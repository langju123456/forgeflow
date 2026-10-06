"""Typed grounding over unchanged S1 checks and frozen synthetic eval sources.

Frozen evaluation documents are exported through an exact local grant and an
explicit source-scope to runtime-scope mapping. They never acquire sandbox,
approval or hardware-check authority. In particular an M03 overlay changes the
analysis catalog, not the persisted technical Gate or its completed-check event.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from .analysis_context import (
    PROFILE_ID,
    ROOT,
    SYNTHETIC_SCOPE,
    AnalysisContextBuilder,
    ContextBundle,
    ExportGrant,
    SourceBinding,
    _mcp_projection,
)
from .analysis_contracts import parse_output, strict_json_object
from .analysis_provider import ProviderInvalid, strict_json
from .contracts import DomainError, Scope, authorize, digest
from .gate import RULES, evaluate

GROUNDING_REVISION = "s2b-frozen-grounding-v1"
FROZEN_CASES_SHA256 = "6c8d42a6c2b7e38542ae9bcacbd34b1a84d3e6ffba60ce973abdd2cb3d216e62"
FROZEN_SCOPE = Scope(
    tenant_id="eval-synthetic", workspace_id="engineering", project_id="aurora-s2-model-eval"
)
_DERIVED_KINDS = frozenset({"eval_fixture", "eval_overlay", "untrusted_document"})
_ROLES = ("supervisor", "pm")


def _read_cases(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != FROZEN_CASES_SHA256:
            raise DomainError("FROZEN_EVALUATION_CHANGED")
        parsed = strict_json(raw)
        if type(parsed) is not dict:
            raise DomainError("FROZEN_EVALUATION_CHANGED")
        return parsed
    except (OSError, UnicodeError, ProviderInvalid) as error:
        raise DomainError("FROZEN_EVALUATION_CHANGED") from error


def _scenario(dataset: dict, scenario_id: str) -> dict:
    for case in dataset["cases"]:
        if case["id"] == scenario_id:
            return copy.deepcopy(case)
        for variant in case.get("variants", []):
            if variant["variant_id"] == scenario_id:
                return {**copy.deepcopy(variant), "id": scenario_id, "parent_case_id": case["id"]}
    raise DomainError("FROZEN_SCENARIO_DENIED", status=403)


@dataclass(frozen=True)
class FrozenSourceGrant:
    source_id: str
    version: str
    source_hash: str
    origin: str
    classification: int
    status: str
    trust: str
    roles: tuple[str, ...] = _ROLES


@dataclass(frozen=True)
class FrozenCaseExportGrant:
    """Trusted config; the model cannot select a case, scope or destination."""

    scenario_id: str
    sources: tuple[FrozenSourceGrant, ...]
    source_scope: Scope = FROZEN_SCOPE
    runtime_scope: Scope = SYNTHETIC_SCOPE
    profile_id: str = PROFILE_ID
    destination: str = "ollama-local-qwen3:8b"
    purpose: str = "frozen-synthetic-grounding-evaluation"
    max_classification: int = 0
    dataset_sha256: str = FROZEN_CASES_SHA256

    @classmethod
    def for_case(cls, scenario_id: str, *, cases_path: Path | None = None):
        dataset = _read_cases(cases_path or ROOT / "preparation/s2/model-evaluation/cases.json")
        case = _scenario(dataset, scenario_id)
        sources = tuple(
            FrozenSourceGrant(
                **{
                    field: source[field]
                    for field in (
                        "source_id", "version", "source_hash", "origin", "classification", "status", "trust"
                    )
                }
            )
            for source in case["inputs"]["context"]
        )
        return cls(scenario_id=scenario_id, sources=sources)

    def payload(self):
        return {
            **asdict(self),
            "source_scope": self.source_scope.model_dump(mode="json"),
            "runtime_scope": self.runtime_scope.model_dump(mode="json"),
        }

    @property
    def revision(self):
        return digest(self.payload())


@dataclass(frozen=True)
class GroundedContextBundle(ContextBundle):
    grounding: dict | None = None

    def to_material(self):
        return {
            **super().to_material(),
            "grounding": copy.deepcopy(self.grounding),
            "catalog_hash": digest(catalog(self)),
        }


def catalog(bundle: ContextBundle) -> dict:
    """Return server-derived claims, never claims inferred from SOP prose."""
    result = bundle.model_context.get("ground_catalog")
    if not isinstance(result, dict) or set(result) != {"observations", "concerns"}:
        raise DomainError("GROUNDING_CONTEXT_REQUIRED")
    return copy.deepcopy(result)


def expected_status(bundle: ContextBundle) -> str:
    return (
        "COMPLETE"
        if all(
            item["check_state"] == "SATISFIED" and item["evidence_state"] == "MEASURED"
            for item in catalog(bundle)["observations"]
        )
        else "INCOMPLETE"
    )


class GroundedContextBuilder(AnalysisContextBuilder):
    def __init__(
        self,
        store,
        scenario_id: str = "M01",
        *,
        export_grant: FrozenCaseExportGrant | None = None,
        cases_path: Path | None = None,
        fixture_root: Path | None = None,
    ):
        self.cases_path = cases_path or ROOT / "preparation/s2/model-evaluation/cases.json"
        self.frozen_grant = export_grant or FrozenCaseExportGrant.for_case(
            scenario_id, cases_path=self.cases_path
        )
        if (
            self.frozen_grant.scenario_id != scenario_id
            or self.frozen_grant.source_scope != FROZEN_SCOPE
            or self.frozen_grant.runtime_scope != SYNTHETIC_SCOPE
            or self.frozen_grant.profile_id != PROFILE_ID
            or self.frozen_grant.destination != "ollama-local-qwen3:8b"
            or self.frozen_grant.purpose != "frozen-synthetic-grounding-evaluation"
            or self.frozen_grant.max_classification != 0
            or self.frozen_grant.dataset_sha256 != FROZEN_CASES_SHA256
        ):
            raise DomainError("GROUNDING_EXPORT_DENIED", status=403)
        super().__init__(store, ExportGrant(scope=self.frozen_grant.runtime_scope), fixture_root)
        # The unchanged builder validates canonical hardware, documents, Memory
        # and both ACLs. This separate instance avoids accepting a stripped S2B
        # bundle during the base builder's own build-time revalidation.
        self._base = AnalysisContextBuilder(store, self.grant, self.fixture_root)
        self._case = self._load_case()

    @property
    def frozen_prompt(self):
        return self._load_case()["inputs"]["model_prompt"]

    def _load_case(self):
        case = _scenario(_read_cases(self.cases_path), self.frozen_grant.scenario_id)
        sources = case["inputs"]["context"]
        grants = {item.source_id: item for item in self.frozen_grant.sources}
        if len(grants) != len(sources) or set(grants) != {source["source_id"] for source in sources}:
            raise DomainError("GROUNDING_EXPORT_DENIED", status=403)
        for source in sources:
            grant = grants[source["source_id"]]
            if (
                source["scope"] != self.frozen_grant.source_scope.model_dump(mode="json")
                or type(source["classification"]) is not int
                or source["classification"] != 0
                or source["trust"] != "UNTRUSTED_DATA"
                or hashlib.sha256(source["content"].encode()).hexdigest() != source["source_hash"]
                or any(getattr(grant, key) != source[key] for key in asdict(grant) if key != "roles")
                or not grant.roles
                or len(set(grant.roles)) != len(grant.roles)
                or not set(grant.roles) <= set(_ROLES)
            ):
                raise DomainError("GROUNDING_EXPORT_DENIED", status=403)
        return case

    def _authorize_frozen(self, caller, scope, role):
        principal = self.role_principal(caller, scope, role)
        for grant in self.frozen_grant.sources:
            for observer in (caller, principal):
                authorize(observer, scope, set(grant.roles), grant.classification)
        return principal

    @staticmethod
    def _kind(source):
        if source["origin"] == "DERIVED_SYNTHETIC_EVAL_OVERLAY":
            return "eval_overlay"
        if source["origin"] == "LOCAL_SYNTHETIC_ADVERSARIAL_DOCUMENT":
            return "untrusted_document"
        if source["origin"].startswith("fixtures/"):
            return "eval_fixture"
        raise DomainError("GROUNDING_EXPORT_DENIED", status=403)

    def _witness(self, case, run_id):
        return {
            "revision": GROUNDING_REVISION,
            "scenario_id": self.frozen_grant.scenario_id,
            "case_hash": digest(case),
            "dataset_sha256": FROZEN_CASES_SHA256,
            "export_grant_revision": self.frozen_grant.revision,
            "scope_mapping": {
                "from": self.frozen_grant.source_scope.model_dump(mode="json"),
                "to": self.frozen_grant.runtime_scope.model_dump(mode="json"),
                "purpose": "EXACT_SYNTHETIC_EVAL_SOURCE_IMPORT_ONLY",
                "run_id": run_id,
            },
            "evaluation_clock": case["inputs"]["evaluation_clock"],
            "source_records": [
                {key: copy.deepcopy(value) for key, value in source.items() if key != "content"}
                for source in case["inputs"]["context"]
            ],
        }

    def _bindings(self, case, scope, run_id):
        witness = self._witness(case, run_id)
        result = {}
        for source in case["inputs"]["context"]:
            kind = self._kind(source)
            ref = self._reference(scope, run_id, kind, source["source_id"], source["source_hash"])
            result[ref] = SourceBinding(
                ref, kind, source["source_id"], source["version"], source["source_hash"],
                source["status"], source["classification"],
                digest({"source": source, "grounding": witness}),
            )
        return result

    def _frozen_projection(self, source, scope, run_id):
        kind = self._kind(source)
        projection = {
            "ref": self._reference(scope, run_id, kind, source["source_id"], source["source_hash"]),
            "kind": kind,
            "version": source["version"],
            "status": source["status"],
            "authority": "UNTRUSTED_DOCUMENT" if kind == "untrusted_document" else "SYNTHETIC_EVAL_DATA",
        }
        if kind == "untrusted_document":
            projection["text"] = source["content"]
        elif kind == "eval_overlay":
            projection["derived_from"] = "AUTHORIZED_CANONICAL_FIXTURE"
        else:
            projection["matches_actual_fixture"] = True
        return projection

    def _check_derivation(self, case, manifest):
        fixture = case["inputs"]["fixture_ref"]
        raw = (self.fixture_root / Path(fixture["path"]).name).read_bytes()
        if (
            hashlib.sha256(raw).hexdigest() != fixture["file_sha256"]
            or digest(strict_json_object(raw.decode())) != fixture["semantic_sha256"]
            or digest(manifest) != fixture["semantic_sha256"]
        ):
            raise DomainError("GROUNDING_FIXTURE_MISMATCH")
        for source in case["inputs"]["context"]:
            kind = self._kind(source)
            if kind == "untrusted_document":
                continue
            content = strict_json_object(source["content"])
            if kind == "eval_fixture":
                if digest(content) != digest(manifest):
                    raise DomainError("GROUNDING_FIXTURE_MISMATCH")
                continue
            expected = copy.deepcopy(manifest)
            provenance = source["provenance"]
            if (
                provenance["base_fixture_ref"] != fixture
                or provenance["evidence_overlays"] != case["inputs"]["evidence_overlays"]
            ):
                raise DomainError("GROUNDING_DERIVATION_INVALID")
            for overlay in provenance["evidence_overlays"]:
                if overlay["metric"] not in RULES["required"]:
                    raise DomainError("GROUNDING_DERIVATION_INVALID")
                expected["metrics"][overlay["metric"]].update(overlay["updates"])
            expected["source_ref"] = provenance["derived_source_ref"]
            if digest(expected) != digest(content):
                raise DomainError("GROUNDING_DERIVATION_INVALID")

    def _hardware_source(self, scope, run_id, run, manifest, material_hash):
        assessment, result = run["assessment_json"], run["result_json"]
        return {
            "ref": self._reference(scope, run_id, "run", run_id, material_hash),
            "kind": "hardware_check",
            "synthetic": True,
            "gate": assessment["status"],
            "checker": result["status"],
            "cleanup": result["cleanup"],
            "stage": run["project"]["current_stage"],
            "transition": assessment["stage_transition"],
            "checks": {item["rule"]: item["status"] for item in assessment["checks"]},
            "metrics": {key: value.get("value") for key, value in manifest["metrics"].items() if key in RULES["required"]},
            "evidence_status": {item["id"]: item["status"] for item in assessment.get("evidence", [])},
            "scan": result.get("scan", {}).get("status", "UNKNOWN"),
            "mcp": _mcp_projection(scope, run_id, run),
        }

    def _catalog(self, bundle, case, manifest):
        hardware = next(source for source in bundle.model_context["sources"] if source["kind"] == "hardware_check")
        return self._derive_catalog(bundle.scope, bundle.run_id, hardware, case, manifest)

    def _derive_catalog(self, scope, run_id, hardware, case, manifest):
        observations, concerns = [], []
        overlay_source = next(
            (source for source in case["inputs"]["context"] if self._kind(source) == "eval_overlay"), None
        )
        overlay_metrics = {
            item["metric"] for item in case["inputs"]["evidence_overlays"]
        }
        derived = None
        if overlay_source:
            derived = strict_json_object(overlay_source["content"])
            # This is an analysis of an explicitly derived synthetic document;
            # it is not recorded as an independently executed hardware check.
            instant = datetime.fromisoformat(case["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
            derived_assessment = evaluate(
                derived,
                {"status": hardware["checker"], "cleanup": hardware["cleanup"]},
                now=instant,
            )
            derived_checks = {item["rule"]: item["status"] for item in derived_assessment["checks"]}
        for metric in RULES["required"]:
            item = manifest["metrics"].get(metric)
            state = hardware["checks"][metric]
            source_ref, authority = hardware["ref"], "HARDWARE_CHECK"
            evidence_state = "MISSING" if item is None else (
                "MEASURED" if state in {"SATISFIED", "VIOLATED"} else "UNCONFIRMED"
            )
            if derived is not None and metric in overlay_metrics:
                item, state = derived["metrics"].get(metric), derived_checks[metric]
                source_ref = self._reference(
                    scope, run_id, "eval_overlay",
                    overlay_source["source_id"], overlay_source["source_hash"],
                )
                authority = "SYNTHETIC_OVERLAY"
                instant = datetime.fromisoformat(case["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
                expiry = datetime.fromisoformat(item["valid_until"].replace("Z", "+00:00"))
                evidence_state = (
                    "CONFLICT" if item.get("conflict") else
                    ("EXPIRED" if expiry <= instant else "MEASURED")
                )
            observation = {
                "source_ref": source_ref, "metric": metric,
                "value": item.get("value") if item else None,
                "check_state": state, "evidence_state": evidence_state, "authority": authority,
            }
            observation = {"id": "c_" + digest(observation)[:16], **observation}
            observations.append(observation)
            if state != "SATISFIED":
                issue = "VIOLATED" if state == "VIOLATED" else evidence_state
                concern = {"source_ref": source_ref, "kind": f"{metric}:{issue}"}
                concerns.append({"id": "c_" + digest(concern)[:16], **concern})
        for source in case["inputs"]["context"]:
            if self._kind(source) != "untrusted_document":
                continue
            concern = {
                "source_ref": self._reference(
                    scope, run_id, "untrusted_document", source["source_id"], source["source_hash"]
                ),
                "kind": "UNTRUSTED_INSTRUCTION",
            }
            concerns.append({"id": "c_" + digest(concern)[:16], **concern})
        return {"observations": observations, "concerns": concerns}

    def build(self, caller, scope, run_id, role="supervisor"):
        self._authorize_frozen(caller, scope, role)
        base = self._base.build(caller, scope, run_id, role)
        case = self._load_case()
        _, manifest, _, _, _ = self._base._checked(base.principal, scope, run_id)
        self._check_derivation(case, manifest)
        additions = self._bindings(case, scope, run_id)
        context = copy.deepcopy(base.model_context)
        for source in context["sources"]:
            if source["kind"] == "document":
                source["authority"] = "GENERIC_RULE"
        for source in case["inputs"]["context"]:
            context["sources"].append(self._frozen_projection(source, scope, run_id))
        bundle = GroundedContextBundle(
            **{field: getattr(base, field) for field in base.__dataclass_fields__},
            grounding=self._witness(case, run_id),
        )
        context["ground_catalog"] = self._catalog(bundle, case, manifest)
        bundle = replace(bundle, model_context=context, citations={**base.citations, **additions}, context_hash=digest(context))
        self.revalidate(bundle)
        return bundle

    def _revalidate_snapshot(self, snapshot, caller, scope, session=None, recipient_role=None):
        role = recipient_role or snapshot["role"]
        self._authorize_frozen(caller, scope, role)
        case = self._load_case()
        if snapshot.get("grounding") != self._witness(case, snapshot["run_id"]):
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        expected = self._bindings(case, scope, snapshot["run_id"])
        derived_sources = [source for source in snapshot["sources"] if source["kind"] in _DERIVED_KINDS]
        actual = {source["ref"]: source for source in derived_sources}
        if len(derived_sources) != len(expected) or actual != {ref: asdict(binding) for ref, binding in expected.items()}:
            raise DomainError("GROUNDING_SOURCE_CHANGED")
        base_snapshot = {
            key: copy.deepcopy(value) for key, value in snapshot.items() if key not in {"grounding", "catalog_hash"}
        }
        base_snapshot["sources"] = [source for source in snapshot["sources"] if source["kind"] not in _DERIVED_KINDS]
        self._base._revalidate_snapshot(base_snapshot, caller, scope, session, recipient_role)
        principal = self._base.role_principal(caller, scope, role)
        run, manifest, _, material_hash, _ = self._base._checked(principal, scope, snapshot["run_id"], session)
        self._check_derivation(case, manifest)
        hardware = self._hardware_source(scope, snapshot["run_id"], run, manifest, material_hash)
        expected_catalog = self._derive_catalog(scope, snapshot["run_id"], hardware, case, manifest)
        if snapshot.get("catalog_hash") != digest(expected_catalog):
            raise DomainError("GROUNDING_CATALOG_CHANGED")

    def revalidate(self, bundle, recipient_role=None):
        if not isinstance(bundle, GroundedContextBundle) or bundle.context_hash != digest(bundle.model_context):
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        self._revalidate_snapshot(bundle.to_material(), bundle.caller, bundle.scope, recipient_role=recipient_role)
        case = self._load_case()
        _, manifest, _, _, _ = self._base._checked(bundle.principal, bundle.scope, bundle.run_id)
        # A self-consistent replacement context must not forge its own catalog.
        expected = self._catalog(bundle, case, manifest)
        if catalog(bundle) != expected:
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        base = self._base.build(bundle.caller, bundle.scope, bundle.run_id, bundle.role)
        actual_hardware = [source for source in bundle.model_context["sources"] if source["kind"] == "hardware_check"]
        expected_hardware = [source for source in base.model_context["sources"] if source["kind"] == "hardware_check"]
        if actual_hardware != expected_hardware:
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        expected_sources = {source["ref"]: copy.deepcopy(source) for source in expected_hardware}
        for binding in bundle.citations.values():
            if binding.kind not in {"document", "memory"}:
                continue
            source = self._base._citation(bundle.principal, bundle.scope, binding.kind, binding.id)
            projection = {
                "ref": binding.ref, "kind": binding.kind, "version": binding.version,
                "status": binding.status,
                "text": source["content"][:700 if binding.kind == "document" else 400],
            }
            if binding.kind == "document":
                projection["authority"] = "GENERIC_RULE"
            expected_sources[binding.ref] = projection
        expected_sources.update({
            projection["ref"]: projection
            for source in case["inputs"]["context"]
            if (projection := self._frozen_projection(source, bundle.scope, bundle.run_id))
        })
        sources = bundle.model_context["sources"]
        if len(sources) != len(expected_sources) or {item["ref"]: item for item in sources} != expected_sources:
            raise DomainError("GROUNDING_SOURCE_CHANGED")
        if (
            set(bundle.model_context) - {"notice", "sources", "ground_catalog", "reports"}
            or bundle.model_context["notice"] != base.model_context["notice"]
        ):
            raise DomainError("GROUNDING_CONTEXT_CHANGED")
        if "reports" in bundle.model_context:
            from .grounded_contracts import validate_output

            reports = bundle.model_context["reports"]
            if len(reports) != 1 or reports[0].get("ref") != "report_pm" or reports[0].get("role") != "pm":
                raise DomainError("GROUNDING_CONTEXT_CHANGED")
            report = {key: value for key, value in reports[0].items() if key not in {"ref", "role"}}
            validate_output(expected, "report", report)

    def frozen_source_refs(self, bundle):
        self.revalidate(bundle)
        return {binding.id: binding.ref for binding in bundle.citations.values() if binding.kind in _DERIVED_KINDS}

    @staticmethod
    def catalog(bundle):
        return catalog(bundle)

    @staticmethod
    def expected_status(bundle):
        return expected_status(bundle)

    def reauthorize_in_session(self, session, principal, scope, material):
        super().reauthorize_in_session(session, principal, scope, material)
        snapshot = material["source_snapshot"]
        snapshots = snapshot.get("bundles", [snapshot])
        if material.get("claim_catalog_hash") != snapshots[-1].get("catalog_hash"):
            raise DomainError("GROUNDING_CATALOG_CHANGED")

    def validate_output(self, bundle, phase, raw, report_refs=()):
        from .grounded_contracts import validate_output

        self.revalidate(bundle)
        if phase in {"plan", "context"}:
            # Reuse the sealed output contracts; base validate_output would
            # reject derived kinds because it deliberately knows only S2A.
            base_parsed = parse_output(phase, raw)
            if phase == "plan" and any(ref not in bundle.citations for ref in base_parsed.source_refs):
                raise DomainError("INVALID_CITATION", status=422)
        parsed = validate_output(catalog(bundle), phase, raw)
        if phase == "summary" and parsed.report_refs != list(report_refs):
            raise DomainError("INVALID_REPORT_REFERENCE", status=422)
        return parsed

    def semantic_check(self, bundle, phase, raw):
        from .grounded_contracts import semantic_check

        self.revalidate(bundle)
        return semantic_check(catalog(bundle), phase, raw)
