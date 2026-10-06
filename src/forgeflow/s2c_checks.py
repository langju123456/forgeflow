"""Pinned M03 inputs executed through the existing synthetic S1 check boundary.

Only this new provider understands aurora-frozen-derived. A Run, its request,
and the scoped immutable input row are committed together. The manifest is data,
never executable code. The frozen clock evaluates synthetic evidence; it does
not replace the database clock used for leases, expiry or authorization.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .analysis_context import (
    ANALYSIS_POLICY_HASH,
    ROOT,
    SYNTHETIC_SCOPE,
    AnalysisContextBuilder,
    _fresh,
    _mcp_projection,
)
from .analysis_contracts import strict_json_object
from .contracts import (
    DomainError,
    ExecutionResult,
    ExecutionSpec,
    Principal,
    Scope,
    StrictModel,
    authorize,
    digest,
)
from .evt_store import EVTBoundStore
from .gate import RULE_HASH, evaluate
from .grounded_context import FROZEN_CASES_SHA256, FROZEN_SCOPE, _read_cases, _scenario
from .policy import POLICY_HASH, execution_policy
from .skills import SkillRegistry
from .store import SCOPE, Base, Event, Run, Scoped, Step, Store, aware, filters, fk, uid, values, view
from .templates import template_hash
from .workflow import Workflow, verify_checker_artifact

CHECK_REVISION = "s2c-derived-check-v1"
PROVIDER_ID = "frozen-synthetic-m03"
DERIVED_FIXTURE_ID = "aurora-frozen-derived"
EVALUATION_CLOCK = "2026-10-05T12:00:00Z"
DERIVED_SOURCE_ID = "c-aurora-conflict-stale-overlay-v1"
DERIVED_SOURCE_HASH = "224646d913e387e65deae3c98f9e724bcf6d56c066881f59dfb4629290700a06"
DERIVED_MANIFEST_HASH = "bad90c2296879e7d53c152a9fca7f05d73c9df49294cd2bb3e850f99fb180ade"
SOURCE_VALID_UNTIL = "2026-11-05T00:00:00Z"
READ_ROLES = {"hardware", "pm", "supervisor", "admin", "submitter"}


class S2CRequest(StrictModel):
    """Public selection contract: callers cannot supply input or authority."""

    scope: Scope
    scenario_id: Literal["M03"] = "M03"


class S2CTrustedInput(Scoped, Base):
    __tablename__ = "s2c_trusted_inputs"
    __table_args__ = (fk("runs", local="run_id"), UniqueConstraint(*SCOPE, "run_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_hash: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[dict] = mapped_column(JSON)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    witness_json: Mapped[dict] = mapped_column(JSON)
    witness_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))


class S2CInputProvider:
    def __init__(self, store: Store, *, cases_path: Path | None = None, fixture_root: Path | None = None,
                 runtime_scope: Scope = SYNTHETIC_SCOPE):
        if runtime_scope != SYNTHETIC_SCOPE:
            raise DomainError("DERIVED_IMPORT_DENIED", status=403)
        self.store, self.runtime_scope = store, runtime_scope
        self.cases_path = cases_path or ROOT / "preparation/s2/model-evaluation/cases.json"
        self.fixture_root = fixture_root or ROOT / "fixtures"

    def create_schema(self):
        # The sole new table is additive; existing tables are never rebuilt.
        S2CTrustedInput.__table__.create(self.store.engine, checkfirst=True)

    def _source(self, scope: Scope):
        if scope != self.runtime_scope:
            raise DomainError("DERIVED_IMPORT_DENIED", status=403)
        try:
            case = _scenario(_read_cases(self.cases_path), "M03")
            inputs = case["inputs"]
            reference = inputs["fixture_ref"]
            sources = inputs["context"]
            if (
                reference["path"] != "fixtures/aurora-pass.json" or reference["version"] != "v1"
                or inputs["evaluation_clock"] != EVALUATION_CLOCK
                or inputs["execution_allowlist"] != ["evt-check"]
                or inputs["execution_allowlist_owner"] != "EXISTING_DETERMINISTIC_HARDWARE_CONTROL_PLANE_ONLY"
                or inputs["resource_profile"] != "checker-v1" or len(sources) != 1
            ):
                raise DomainError("DERIVED_SOURCE_CHANGED")
            source = sources[0]
            expected_overlays = [
                {"metric": "thermal_peak_c", "updates": {"conflict": True}},
                {"metric": "ci_smoke", "updates": {"valid_until": "2026-10-04T12:00:00Z"}},
            ]
            if (
                source["source_id"] != DERIVED_SOURCE_ID or source["version"] != "v1"
                or source["source_hash"] != DERIVED_SOURCE_HASH
                or source["scope"] != FROZEN_SCOPE.model_dump(mode="json")
                or source["classification"] != 0 or type(source["classification"]) is not int
                or source["trust"] != "UNTRUSTED_DATA" or source["status"] != "CONFLICT"
                or source["origin"] != "DERIVED_SYNTHETIC_EVAL_OVERLAY"
                or inputs["evidence_overlays"] != expected_overlays
                or hashlib.sha256(source["content"].encode()).hexdigest() != DERIVED_SOURCE_HASH
            ):
                raise DomainError("DERIVED_SOURCE_CHANGED")
            raw = (self.fixture_root / "aurora-pass.json").read_bytes()
            if len(raw) > 64 * 1024 or hashlib.sha256(raw).hexdigest() != reference["file_sha256"]:
                raise DomainError("DERIVED_SOURCE_CHANGED")
            base = strict_json_object(raw.decode())
            if digest(base) != reference["semantic_sha256"]:
                raise DomainError("DERIVED_SOURCE_CHANGED")
            manifest = copy.deepcopy(base)
            for overlay in expected_overlays:
                manifest["metrics"][overlay["metric"]].update(overlay["updates"])
            manifest["source_ref"] = "fixture:aurora-conflict-stale-overlay-v1"
            if (
                manifest != strict_json_object(source["content"])
                or digest(manifest) != DERIVED_MANIFEST_HASH
                or manifest.get("synthetic") is not True or manifest.get("version") != "v1"
                or source["provenance"] != {
                    "base_fixture_ref": reference, "evidence_overlays": expected_overlays,
                    "derived_source_ref": manifest["source_ref"],
                }
            ):
                raise DomainError("DERIVED_SOURCE_CHANGED")
            witness = {
                "provider_id": PROVIDER_ID, "provider_revision": CHECK_REVISION, "scenario_id": "M03",
                "dataset_sha256": FROZEN_CASES_SHA256, "case_hash": digest(case),
                "base_fixture": copy.deepcopy(reference), "overlays": expected_overlays,
                "source_id": DERIVED_SOURCE_ID, "source_version": "v1",
                "source_content_hash": DERIVED_SOURCE_HASH, "manifest_hash": DERIVED_MANIFEST_HASH,
                "source_ref": manifest["source_ref"], "synthetic": True, "classification": 0,
                "evaluation_clock": EVALUATION_CLOCK, "evaluation_clock_kind": "FROZEN_SYNTHETIC",
                "scope_mapping": {
                    "from": FROZEN_SCOPE.model_dump(mode="json"), "to": scope.model_dump(mode="json"),
                    "purpose": "EXACT_FROZEN_SYNTHETIC_CHECK_IMPORT",
                },
                "template_id": "evt-check", "resource_profile": "checker-v1",
                "code_hash": template_hash("evt-check"), "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH,
                "skill_hash": SkillRegistry(ROOT / "skills").load("hardware-evt", "hardware").sha256,
                "valid_until": SOURCE_VALID_UNTIL,
            }
            return manifest, witness
        except DomainError:
            raise
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            raise DomainError("DERIVED_SOURCE_CHANGED") from error

    @staticmethod
    def _request(scope, witness):
        return {
            "scope": scope.model_dump(mode="json"), "stage": "EVT", "fixture_id": DERIVED_FIXTURE_ID,
            "fixture_version": "v1", "scenario_id": "M03",
            "objective": "Execute the pinned synthetic M03 manifest; preserve separate human approval",
            "trusted_input": copy.deepcopy(witness),
        }

    @staticmethod
    def _binding(scope, run_id, request_hash, manifest, witness):
        return {
            "scope": scope.model_dump(mode="json"), "run_id": run_id, "request_hash": request_hash,
            "manifest": manifest, "witness": witness,
        }

    def submit(self, principal: Principal, request: S2CRequest, key: str):
        if type(request) is not S2CRequest or principal.kind != "user":
            raise DomainError("DERIVED_SUBMISSION_DENIED", status=403)
        authorize(principal, request.scope, {"submitter", "supervisor"})
        if type(key) is not str or not 1 <= len(key) <= 128:
            raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)
        scope = request.scope
        with self.store._tx() as session:
            # Re-read pinned sources inside the same transaction as Run creation.
            manifest, witness = self._source(scope)
            _fresh(witness["valid_until"], self.store._now(session))
            bound_request = self._request(scope, witness)
            request_hash = digest(bound_request)
            self.store._ensure_project(session, scope)
            identifier = uid()
            self.store._insert_ignore(session, Run, {
                **values(scope), "id": identifier, "created_by": principal.subject_id,
                "idempotency_key": key, "request_hash": request_hash, "request_json": bound_request,
                "state": "QUEUED", "revision": 0, "current_step": "readiness",
                "cleanup_required": False, "created_at": self.store._now(session),
            }, SCOPE + ["created_by", "idempotency_key"])
            run = session.scalar(select(Run).where(
                *filters(Run, scope), Run.created_by == principal.subject_id, Run.idempotency_key == key,
            ).with_for_update())
            if run.request_hash != request_hash or run.request_json != bound_request:
                raise DomainError("IDEMPOTENCY_CONFLICT")
            if run.id == identifier:
                session.add(Step(**values(scope), id=uid(), run_id=run.id, step_key="readiness",
                                 state="PENDING", attempt=0, fencing_epoch=0))
                session.flush()
                session.add(S2CTrustedInput(
                    **values(scope), id=uid(), run_id=run.id, created_by=principal.subject_id,
                    created_at=self.store._now(session), request_hash=request_hash,
                    manifest_json=manifest, manifest_hash=digest(manifest), witness_json=witness,
                    witness_hash=digest(witness),
                    payload_hash=digest(self._binding(scope, run.id, request_hash, manifest, witness)),
                ))
                session.flush()
                self.store._event(session, principal, scope, run, "RUN_SUBMITTED", {"request_hash": request_hash})
                self.store._event(session, principal, scope, run, "TRUSTED_INPUT_BOUND", {
                    "provider_revision": CHECK_REVISION, "manifest_hash": digest(manifest),
                    "witness_hash": digest(witness), "dataset_sha256": FROZEN_CASES_SHA256,
                })
            self.read_in_session(session, principal, scope, run.id)
            return self.store._run_view(session, scope, run, principal)

    def read_in_session(self, session, principal, scope, run_id):
        authorize(principal, scope, READ_ROLES, 0)
        run = self.store._get(session, Run, scope, run_id)
        self.store._run_acl(session, principal, scope, run)
        row = session.scalar(select(S2CTrustedInput).where(
            *filters(S2CTrustedInput, scope), S2CTrustedInput.run_id == run_id,
        ).with_for_update())
        if row is None:
            raise DomainError("DERIVED_INPUT_NOT_BOUND", status=404)
        manifest, witness = self._source(scope)
        request = self._request(scope, witness)
        _fresh(witness["valid_until"], self.store._now(session))
        if (
            run.request_json != request or run.request_hash != digest(request)
            or row.request_hash != run.request_hash or row.created_by != run.created_by
            or row.manifest_json != manifest or row.manifest_hash != digest(manifest)
            or row.witness_json != witness or row.witness_hash != digest(witness)
            or row.payload_hash != digest(self._binding(scope, run_id, run.request_hash, manifest, witness))
        ):
            raise DomainError("DERIVED_INPUT_CHANGED")
        return {"run": self.store._run_view(session, scope, run, principal),
                "manifest": copy.deepcopy(manifest), "witness": copy.deepcopy(witness)}

    def load_run(self, principal, scope, run_id):
        with self.store._tx() as session:
            return self.read_in_session(session, principal, scope, run_id)

    def finish(self, principal, scope, run_id, token, epoch, result, assessment, snapshot):
        # Reuse the sealed complete_check transaction and artifacts/approval logic.
        # Its _lease hook runs after the Run lock and before any completion write.
        bound = _BoundCompletionStore(self, principal, result, assessment, snapshot)
        return bound.complete_check(principal, scope, run_id, token, epoch, result, assessment, snapshot)

    def checked(self, principal, scope, run_id, session=None):
        if session is None:
            with self.store._tx() as current:
                return self.checked(principal, scope, run_id, session=current)
        loaded = self.read_in_session(session, principal, scope, run_id)
        run, manifest, witness = loaded["run"], loaded["manifest"], loaded["witness"]
        events = [view(event) for event in session.scalars(select(Event).where(
            *filters(Event, scope), Event.run_id == run_id,
        ).order_by(Event.sequence))]
        previous, sequence = "0" * 64, 1
        for event in events:
            authorize(principal, scope, classification=event["classification"])
            material = {"scope": scope.model_dump(mode="json"), "run_id": run_id, **{
                key: event[key] for key in (
                    "step_id", "sequence", "kind", "actor", "payload_hash", "prev_hash", "classification", "created_at"
                )}}
            if (event["sequence"] != sequence or event["prev_hash"] != previous
                or digest(event["payload"]) != event["payload_hash"] or digest(material) != event["event_hash"]):
                raise DomainError("INVALID_SOURCE_AUDIT")
            previous, sequence = event["event_hash"], sequence + 1
        completed = [event for event in events if event["kind"] == "CHECK_COMPLETED"]
        bound_events = [event for event in events if event["kind"] == "TRUSTED_INPUT_BOUND"]
        if len(completed) != 1 or len(bound_events) != 1 or not run.get("snapshot_json") or not run.get("result_json"):
            raise DomainError("CHECK_NOT_COMPLETED")
        result, assessment, snapshot = run["result_json"], run["assessment_json"], run["snapshot_json"]
        self._validate_completion(manifest, witness, result, assessment, snapshot)
        if (
            run.get("cancel_requested_at") or run["snapshot_hash"] != digest(snapshot)
            or completed[0]["payload"].get("result_hash") != digest(result)
            or completed[0]["payload"].get("assessment_hash") != digest(assessment)
            or bound_events[0]["payload"] != {
                "provider_revision": CHECK_REVISION, "manifest_hash": digest(manifest),
                "witness_hash": digest(witness), "dataset_sha256": FROZEN_CASES_SHA256,
            }
        ):
            raise DomainError("DERIVED_CHECK_CHANGED")
        if result.get("status") != "SUCCEEDED" or result.get("cleanup") != "REMOVED":
            raise DomainError("DERIVED_CHECK_NOT_CONFIRMED")
        expected_artifacts = [{
            "path": artifact["path"], "content_sha256": artifact["sha256"],
            "size": artifact["size"], "content": artifact["content"],
        } for artifact in result.get("artifacts", [])]
        actual_artifacts = [{key: artifact[key] for key in ("path", "content_sha256", "size", "content")}
                            for artifact in run["artifacts"]]
        if (
            sorted(expected_artifacts, key=digest) != sorted(actual_artifacts, key=digest)
            or any(artifact["step_id"] != completed[0]["step_id"]
                   or artifact["classification"] != 0
                   or artifact["provenance"].get("input_hash") != digest(manifest)
                   for artifact in run["artifacts"])
            or sorted([row["data"] for row in run["evidence"]], key=digest)
            != sorted(assessment.get("evidence", []), key=digest)
            or any(row["content_hash"] != row["data"]["content_hash"]
                   or row["source_ref"] != manifest["source_ref"] or row["source_version"] != "v1"
                   or row["status"] != row["data"]["status"] for row in run["evidence"])
        ):
            raise DomainError("DERIVED_CHECK_ARTIFACT_INVALID")
        if _mcp_projection(scope, run_id, run) not in {"RECORDED", "TEST_DRIVER"}:
            raise DomainError("DERIVED_CHECK_PROTOCOL_UNCONFIRMED")
        now = self.store._now(session)
        _fresh(snapshot.get("valid_until"), now)
        if not snapshot.get("documents"):
            raise DomainError("SYNTHETIC_SOURCE_REQUIRED")
        document_verifier = AnalysisContextBuilder(self.store)
        for reference in snapshot["documents"]:
            source = document_verifier._document_exact(principal, scope, reference, session)
            document_verifier._export_source(source, "document", run_id, completed[0]["id"], now)
        material_hash = digest({
            "result": result, "assessment": assessment, "snapshot": snapshot, "revision": run["revision"],
            "project": run["project"], "completed_event": completed[0]["event_hash"],
            "fixture": digest(manifest), "rules": RULE_HASH, "policy": ANALYSIS_POLICY_HASH,
            "context_revision": CHECK_REVISION, "input_binding": digest(witness),
        })
        return run, manifest, completed[0], material_hash, now

    @staticmethod
    def _validate_completion(manifest, witness, result, assessment, snapshot):
        if (
            snapshot.get("trusted_input") != witness or result.get("trusted_input") != witness
            or snapshot.get("evaluation_clock") != EVALUATION_CLOCK
            or result.get("evaluation_clock") != EVALUATION_CLOCK
            or snapshot.get("input_hash") != digest(manifest)
            or snapshot.get("input_manifest_hash") != digest(manifest)
            or snapshot.get("fixture_version") != "v1" or snapshot.get("rule_hash") != RULE_HASH
            or snapshot.get("policy_hash") != POLICY_HASH or snapshot.get("code_hash") != template_hash("evt-check")
            or snapshot.get("skill_hash") != witness["skill_hash"]
            or result.get("skill", {}).get("sha256") != witness["skill_hash"]
            or snapshot.get("classification") != 0 or type(snapshot.get("classification")) is not int
            or result.get("synthetic") is not True
            or digest(evaluate(manifest, result, now=aware(EVALUATION_CLOCK))) != digest(assessment)
        ):
            raise DomainError("DERIVED_CHECK_CHANGED")
        if result.get("status") == "SUCCEEDED":
            raw = {key: result[key] for key in ExecutionResult.model_fields if key in result}
            try:
                execution = ExecutionResult.model_validate(raw)
            except ValueError as error:
                raise DomainError("DERIVED_CHECK_ARTIFACT_INVALID") from error
            if not verify_checker_artifact(execution, manifest):
                raise DomainError("DERIVED_CHECK_ARTIFACT_INVALID")
            scan = result.get("scan", {})
            if scan.get("status") != "PASS" or scan.get("source_hash", scan.get("code_hash")) != template_hash("evt-check"):
                raise DomainError("DERIVED_CHECK_SCAN_UNCONFIRMED")


class _BoundCompletionStore(EVTBoundStore):
    def __init__(self, provider, principal, result, assessment, snapshot):
        # Share the existing engine and transaction lock, never create a second DB.
        self.__dict__.update(provider.store.__dict__)
        self.provider, self.principal = provider, principal
        self._completion = result, assessment, snapshot

    def _lease(self, session, scope, run, token, epoch, allow_cancel=False):
        step = super()._lease(session, scope, run, token, epoch, allow_cancel)
        loaded = self.provider.read_in_session(session, self.principal, scope, run.id)
        self.provider._validate_completion(loaded["manifest"], loaded["witness"], *self._completion)
        return step


class S2CCheckWorkflow(Workflow):
    def __init__(self, *args, input_provider: S2CInputProvider, **kwargs):
        super().__init__(*args, **kwargs)
        if input_provider.store is not self.store:
            raise DomainError("DERIVED_PROVIDER_STORE_MISMATCH")
        self.input_provider = input_provider

    async def check(self, scope, run_id):
        service, role = self.service(scope), self.role(scope)
        loaded = self.input_provider.load_run(service, scope, run_id)
        run, manifest, witness = loaded["run"], loaded["manifest"], loaded["witness"]
        if run.get("assessment_json") is not None:
            return self.consolidate(scope, run_id)
        claim = self.store.claim(service, scope, run_id, "s2c-local-worker", ttl=90)
        token, epoch = claim["token"], claim["epoch"]
        try:
            # Validate again after lease acquisition; no caller-selected template.
            current = self.input_provider.load_run(service, scope, run_id)
            if current["manifest"] != manifest or current["witness"] != witness:
                raise DomainError("DERIVED_INPUT_CHANGED")
            skill = self.skills.load("hardware-evt", "hardware")
            if "sandbox.execute" not in skill.allowed_tools:
                raise DomainError("SKILL_TOOL_DENIED", status=403)
            hits = self.knowledge.search(role, scope, "Aurora EVT", top_k=5)
            prior = self.memory.search(role, scope, "Aurora EVT", top_k=5)
            source_hash = self.sandbox.template_hash("evt-check")
            if source_hash != witness["code_hash"]:
                raise DomainError("DERIVED_CODE_CHANGED")
            identity = {"scope": scope.model_dump(mode="json"), "run_id": run_id,
                        "step_id": claim["step_id"], "attempt": claim["attempt"]}
            scanner_handle = self.scanner.handle(scope, run_id, claim["step_id"], claim["attempt"])
            self.store.set_scanner_handle(service, scope, run_id, token, epoch, scanner_handle)
            lease_deadline = aware(claim["lease_expires_at"]).timestamp()
        except Exception as error:
            if self.store.get_run(service, scope, run_id).get("cancel_requested_at"):
                return self.store.acknowledge_cancel(service, scope, run_id, token, epoch, "NOT_STARTED")
            return self.store.fail_check(service, scope, run_id, token, epoch, {
                "status": "DENIED" if isinstance(error, DomainError) else "FAILED", "cleanup": "NOT_STARTED",
                "reason": getattr(error, "code", type(error).__name__), "trusted_input": witness,
            })
        try:
            scan = await asyncio.to_thread(self.scanner.scan, "evt-check", handle=scanner_handle,
                                           identity=identity, lease_deadline=lease_deadline)
        except Exception as error:
            scan = {"status": "ERROR", "source_hash": source_hash, "cleanup": "FAILED",
                    "reason": "Scanner controller raised " + type(error).__name__}
        snapshot = {
            "input_manifest_hash": digest(manifest), "input_hash": digest(manifest),
            "policy_hash": POLICY_HASH, "skill_hash": skill.sha256, "rule_hash": RULE_HASH,
            "code_hash": source_hash, "fixture_version": "v1", "classification": 0,
            "documents": [{"id": hit.id, "version": hit.version, "source_hash": hit.source_hash}
                          for hit in hits if hit.status == "CURRENT"],
            "valid_until": SOURCE_VALID_UNTIL, "trusted_input": witness,
            "evaluation_clock": EVALUATION_CLOCK,
        }
        transcript, handle = {"status": "NOT_STARTED"}, ""
        try:
            self.input_provider.load_run(service, scope, run_id)
            spec = ExecutionSpec(
                scope=scope, run_id=run_id, step_id=claim["step_id"], attempt=claim["attempt"],
                template_id="evt-check", input_manifest=manifest, input_hash=digest(manifest),
                code_hash=source_hash, image=self.sandbox.resolve_image(), timeout_seconds=20,
            )
            execution_policy(role, spec, scan.get("source_hash", scan.get("code_hash", "")), scan.get("status", "UNAVAILABLE"))
            handle = self.sandbox.handle(spec)
            self.store.set_sandbox_handle(service, scope, run_id, token, epoch, handle)
            result, transcript = await self.bridge.execute(
                role, spec, scan_receipt=scan, lease_deadline=lease_deadline,
                cancel_requested=lambda: bool(self.store.get_run(service, scope, run_id).get("cancel_requested_at")),
            )
        except Exception as error:
            cleanup = "NOT_STARTED" if not handle else (
                "REMOVED" if await asyncio.to_thread(self.sandbox.cleanup, handle) else "FAILED"
            )
            result = ExecutionResult(handle=handle, status="UNAVAILABLE", cleanup=cleanup,
                                     reason=f"{type(error).__name__}: {str(error)[:600]}")
        result_dict = result.model_dump(mode="json")
        if scan.get("cleanup") == "FAILED":
            result_dict["cleanup"] = "FAILED"
        if self.store.get_run(service, scope, run_id).get("cancel_requested_at"):
            return self.store.acknowledge_cancel(service, scope, run_id, token, epoch, result_dict["cleanup"])
        if result.status == "SUCCEEDED" and not verify_checker_artifact(result, manifest):
            result_dict.update(status="FAILED", reason="Independent artifact verification failed")
        assessment = evaluate(manifest, result_dict, now=aware(EVALUATION_CLOCK))
        report = {
            **result_dict, "scan": scan, "mcp": transcript, "orchestration": "DETERMINISTIC_NO_LLM",
            "skill": skill.model_dump(mode="json"), "knowledge_count": len(hits), "prior_memory_count": len(prior),
            "synthetic": True, "trusted_input": witness, "evaluation_clock": EVALUATION_CLOCK,
            "supervisor_plan": {"planner": "DETERMINISTIC", "role": "hardware", "skill": skill.name,
                                "tools": list(skill.allowed_tools), "maximum_check_seconds": 20,
                                "reason": "Pinned synthetic M03 input requires a scoped manifest checker"},
        }
        self.input_provider.finish(service, scope, run_id, token, epoch, report, assessment, snapshot)
        return self.consolidate(scope, run_id)

    def dispatch(self, scope, run_id, action_id):
        raise DomainError("S2C_BUSINESS_DISPATCH_DENIED", status=403)
