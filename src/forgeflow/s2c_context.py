"""S2C source authority and explicit server projection of raw selections.

The model never supplies completion status. The existing internal report shape
is used only after strict raw-selection validation and derives its status from
the authorized catalog. M03 references a separately completed derived-input
check; the historical S2B analysis-only overlay remains unchanged.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .analysis_context import AnalysisContextBuilder
from .analysis_contracts import parse_output, strict_json_object
from .contracts import DomainError, digest
from .gate import RULES
from .grounded_context import GroundedContextBuilder, catalog
from .grounded_contracts import GroundedReport
from .s2c_contracts import derive_status, project, semantic_check, validate_output

CONTEXT_REVISION = "s2c-authoritative-checks-v1"
STATUS_AUTHORITY = "SERVER_DERIVED_AUTHORIZED_CATALOG"
DERIVED_FIXTURE_ID = "aurora-frozen-derived"


class DerivedAnalysisContextBuilder(AnalysisContextBuilder):
    """Reuse the source provider's complete, transactional check verification."""

    def __init__(self, store, input_provider, grant=None, fixture_root=None):
        super().__init__(store, grant, fixture_root)
        self.input_provider = input_provider

    def _checked(self, principal, scope, run_id, session=None):
        run = self._run(principal, scope, run_id, session)
        if run["request_json"].get("fixture_id") != DERIVED_FIXTURE_ID:
            return super()._checked(principal, scope, run_id, session)
        checked = self.input_provider.checked(principal, scope, run_id, session=session)
        checked_run, _, completed, _, now = checked
        if checked_run["id"] != run_id or checked_run["request_json"] != run["request_json"]:
            raise DomainError("SOURCE_CHANGED")
        # Apply the unchanged model-export allowlist after the provider verifies
        # the actual event chain, artifact, manifest, rules and source snapshot.
        for reference in checked_run["snapshot_json"].get("documents", []):
            source = self._document_exact(principal, scope, reference, session)
            self._export_source(source, "document", run_id, completed["id"], now)
        return checked


class S2CContextBuilder(GroundedContextBuilder):
    def __init__(self, store, scenario_id="M01", *, input_provider=None, **kwargs):
        super().__init__(store, scenario_id, **kwargs)
        if scenario_id == "M03" and input_provider is None:
            from .s2c_checks import S2CInputProvider

            input_provider = S2CInputProvider(
                store, cases_path=self.cases_path, fixture_root=self.fixture_root,
                runtime_scope=self.frozen_grant.runtime_scope,
            )
        self.input_provider = input_provider
        if input_provider is not None:
            self._base = DerivedAnalysisContextBuilder(store, input_provider, self.grant, self.fixture_root)

    def _checked(self, principal, scope, run_id, session=None):
        # The sealed search path invokes self._checked directly. Share the
        # exact same verifier used by build/revalidation so derived runs cannot
        # fall back to the canonical fixture alias. _base is a separate builder
        # and never delegates back to this context instance.
        return self._base._checked(principal, scope, run_id, session)

    def _witness(self, case, run_id):
        return {
            **super()._witness(case, run_id),
            "s2c_context_revision": CONTEXT_REVISION,
            "report_status_authority": STATUS_AUTHORITY,
            "input_contract": (
                "CONFIRMED_DERIVED_INPUT_CHECK" if self.frozen_grant.scenario_id == "M03"
                else "UNCHANGED_CANONICAL_FIXTURE_CHECK"
            ),
        }

    def _check_derivation(self, case, manifest):
        if self.frozen_grant.scenario_id != "M03":
            return super()._check_derivation(case, manifest)
        fixture = case["inputs"]["fixture_ref"]
        try:
            canonical_manifest = strict_json_object(
                (self.fixture_root / Path(fixture["path"]).name).read_text()
            )
        except (OSError, UnicodeError) as error:
            raise DomainError("GROUNDING_FIXTURE_MISMATCH") from error
        # This checks the original fixture's byte/semantic hashes and recreates
        # the exact frozen overlay, without changing either file or its time.
        super()._check_derivation(case, canonical_manifest)
        derived = next(
            source for source in case["inputs"]["context"]
            if source["origin"] == "DERIVED_SYNTHETIC_EVAL_OVERLAY"
        )
        if digest(strict_json_object(derived["content"])) != digest(manifest):
            raise DomainError("DERIVED_CHECK_REQUIRED")

    def _derive_catalog(self, scope, run_id, hardware, case, manifest):
        if self.frozen_grant.scenario_id != "M03":
            return super()._derive_catalog(scope, run_id, hardware, case, manifest)
        if hardware["checker"] != "SUCCEEDED" or hardware["cleanup"] != "REMOVED":
            raise DomainError("DERIVED_CHECK_NOT_CONFIRMED")
        instant = datetime.fromisoformat(case["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
        observations, concerns = [], []
        for metric in RULES["required"]:
            item = manifest["metrics"].get(metric)
            state = hardware["checks"][metric]
            if item is None:
                evidence_state = "MISSING"
            elif item.get("conflict"):
                evidence_state = "CONFLICT"
            elif datetime.fromisoformat(item["valid_until"].replace("Z", "+00:00")) <= instant:
                evidence_state = "EXPIRED"
            elif item.get("status") != "MEASURED":
                evidence_state = "UNCONFIRMED"
            else:
                evidence_state = "MEASURED" if state in {"SATISFIED", "VIOLATED"} else "UNCONFIRMED"
            observation = {
                "source_ref": hardware["ref"], "metric": metric,
                "value": item.get("value") if item else None,
                "check_state": state, "evidence_state": evidence_state,
                "authority": "HARDWARE_CHECK",
            }
            observations.append({"id": "c_" + digest(observation)[:16], **observation})
            if state != "SATISFIED":
                issue = "VIOLATED" if state == "VIOLATED" else evidence_state
                concern = {"source_ref": hardware["ref"], "kind": f"{metric}:{issue}"}
                concerns.append({"id": "c_" + digest(concern)[:16], **concern})
        return {"observations": observations, "concerns": concerns}

    def revalidate(self, bundle, recipient_role=None):
        super().revalidate(bundle, recipient_role)
        if "reports" not in bundle.model_context:
            return
        report = bundle.model_context["reports"][0]
        projected = {key: value for key, value in report.items() if key not in {"ref", "role"}}
        selection = {key: value for key, value in projected.items() if key != "status"}
        # The legacy shape is strictly an internal projection. Re-derive rather
        # than accepting a pre-filled status as if it came from the model.
        parsed = validate_output(catalog(bundle), "report", selection)
        if projected != project("report", parsed, catalog(bundle)).model_dump(mode="json"):
            raise DomainError("SERVER_PROJECTION_CHANGED")

    def validate_output(self, bundle, phase, raw, report_refs=()):
        self.revalidate(bundle)
        if phase in {"plan", "context"}:
            parsed = parse_output(phase, raw)
            if phase == "plan" and any(ref not in bundle.citations for ref in parsed.source_refs):
                raise DomainError("INVALID_CITATION", status=422)
        parsed = validate_output(catalog(bundle), phase, raw)
        if phase == "summary" and parsed.report_refs != list(report_refs):
            raise DomainError("INVALID_REPORT_REFERENCE", status=422)
        return parsed

    def semantic_check(self, bundle, phase, raw):
        self.revalidate(bundle)
        return semantic_check(catalog(bundle), phase, raw)

    @staticmethod
    def expected_status(bundle):
        return derive_status(catalog(bundle))

    def share_report(self, pm_bundle, supervisor_bundle, report, report_ref="report_pm"):
        if pm_bundle.role != "pm" or supervisor_bundle.role != "supervisor":
            raise DomainError("ANALYSIS_ROLE_DENIED", status=403)
        if (
            pm_bundle.scope != supervisor_bundle.scope or pm_bundle.run_id != supervisor_bundle.run_id
            or pm_bundle.caller != supervisor_bundle.caller
        ):
            raise DomainError("NOT_FOUND", status=404)
        if report_ref != "report_pm":
            raise DomainError("INVALID_REPORT_REFERENCE", status=422)
        raw = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
        if type(report) is GroundedReport:
            # The sealed engine receives this explicit internal compatibility
            # object from S2CWorkflow._call. Its status must be derived again;
            # dictionaries/provider outputs still go through the statusless
            # parser unchanged and cannot take this internal-only path.
            if raw.get("status") != derive_status(catalog(pm_bundle)):
                raise DomainError("SERVER_PROJECTION_CHANGED")
            raw = {key: value for key, value in raw.items() if key != "status"}
        selection = self.validate_output(pm_bundle, "report", raw)
        internal = project("report", selection, catalog(pm_bundle))
        if type(report) is GroundedReport and internal.model_dump(mode="json") != report.model_dump(mode="json"):
            raise DomainError("SERVER_PROJECTION_CHANGED")
        self.revalidate(pm_bundle, recipient_role="supervisor")
        self.revalidate(supervisor_bundle)
        context = copy.deepcopy(supervisor_bundle.model_context)
        context["reports"] = [{"ref": report_ref, "role": "pm", **internal.model_dump(mode="json")}]
        known = {source["ref"] for source in context["sources"]}
        context["sources"].extend(
            copy.deepcopy(source) for source in pm_bundle.model_context["sources"] if source["ref"] not in known
        )
        bundle = replace(
            supervisor_bundle, model_context=context,
            citations={**supervisor_bundle.citations, **pm_bundle.citations}, context_hash=digest(context),
        )
        self.revalidate(bundle)
        return bundle
