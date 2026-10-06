"""SRE SOP over recorded execution receipts, independent of technical approval."""

import hashlib
from pathlib import Path
from typing import Literal

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .analysis_context import _mcp_projection
from .contracts import DomainError, Principal, Scope, StrictModel, authorize, digest
from .skills import SkillRegistry
from .store import SCOPE, Base, Event, Run, Scoped, filters, fk, uid, values, view

REVISION = "sre-recorded-operations-v1"
ROOT = Path(__file__).resolve().parents[2]
ROLE_TOOLS = {"sre": frozenset({"execution.receipts.read", "execution.events.read"})}
PROFILE = {"network_mode": "none", "readonly_rootfs": True, "privileged": False,
           "user": "10001:10001", "nano_cpus": 500000000, "memory": 268435456, "pids_limit": 32}


class OperationalCheck(StrictModel):
    id: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    reason: str
    source_pointer: str


class OperationalReport(StrictModel):
    revision: str = REVISION
    role: Literal["sre"] = "sre"
    authority: Literal["DETERMINISTIC_SOP"] = "DETERMINISTIC_SOP"
    source_observation: Literal["RECORDED_RECEIPTS_NOT_LIVE_PROBE"] = "RECORDED_RECEIPTS_NOT_LIVE_PROBE"
    scope: Scope
    run_id: str
    source_hash: str
    skill_hash: str
    operational_readiness: Literal["READY", "NEEDS_REVIEW", "UNKNOWN"]
    technical_gate: Literal["PASS", "BLOCKED", "UNKNOWN"]
    checks: tuple[OperationalCheck, ...]
    supervisor_handoff: Literal["READY_FOR_HUMAN_REVIEW", "NEEDS_TECHNICAL_EVIDENCE", "NEEDS_OPERATIONAL_REVIEW",
                                "ALREADY_DECIDED_OR_EXECUTED"]
    human_approval_required: Literal[True] = True
    human_approval_state: Literal["PENDING", "DECIDED", "NOT_REQUESTED"]
    business_receipt_count: int
    business_actions_executed_by_sop: Literal[False] = False


class SREReview(Scoped, Base):
    __tablename__ = "sre_reviews"
    __table_args__ = (fk("runs", local="run_id"), UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    report_json: Mapped[dict] = mapped_column(JSON)
    audit_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))


def source_binding(run, events):
    return digest({"scope": {key: run[key] for key in SCOPE}, "id": run["id"], "revision": run["revision"],
                   "state": run["state"], "cleanup_required": run["cleanup_required"],
                   "snapshot": run["snapshot_json"], "snapshot_hash": run["snapshot_hash"],
                   "result": run["result_json"], "assessment": run["assessment_json"],
                   "steps": run["steps"], "artifacts": run["artifacts"], "events": events,
                   "approvals": run["approvals"], "receipts": run["receipts"], "project": run["project"]})


def event_chain_valid(scope, run_id, events):
    previous = "0" * 64
    for sequence, item in enumerate(events, 1):
        material = {"scope": values(scope), "run_id": run_id, **{key: item[key] for key in (
            "step_id", "sequence", "kind", "actor", "payload_hash", "prev_hash", "classification", "created_at")}}
        if (item["sequence"] != sequence or item["prev_hash"] != previous
                or digest(item["payload"]) != item["payload_hash"] or digest(material) != item["event_hash"]):
            return False
        previous = item["event_hash"]
    return bool(events)


def evaluate_operations(scope, run, events, skill_hash):
    """Use only receipts. Frozen inspector state is not current container state."""
    result, snapshot = run.get("result_json") or {}, run.get("snapshot_json") or {}
    inspection, scan = result.get("inspect", {}), result.get("scan", {})
    completed = [item for item in events if item["kind"] == "CHECK_COMPLETED"]
    checks = []

    def add(name, ok, reason, pointer, *, present=True):
        checks.append(OperationalCheck(id=name, status="UNKNOWN" if not present else "PASS" if ok else "FAIL",
                                       reason=reason, source_pointer=pointer))

    completion = completed[-1] if completed else {}
    payload = completion.get("payload", {})
    add("audit_binding", event_chain_valid(scope, run["id"], events)
        and run.get("snapshot_hash") == digest(snapshot)
        and payload.get("result_hash") == digest(result)
        and payload.get("assessment_hash") == digest(run.get("assessment_json")),
        "CHECK_COMPLETED_BINDING", "/events/CHECK_COMPLETED", present=bool(completed))
    steps = [row for row in run["steps"] if row["id"] == completion.get("step_id")]
    step = steps[0] if len(steps) == 1 else {}
    claims = [item for item in events if item["kind"] == "STEP_CLAIMED" and item["step_id"] == step.get("id")]
    claim = claims[-1]["payload"] if claims else {}
    artifacts = run.get("artifacts", [])
    add("lease_closure", step.get("state") == "SUCCEEDED" and step.get("lease_expires_at") is None
        and step.get("attempt") == claim.get("attempt") and step.get("fencing_epoch") == claim.get("epoch")
        and step.get("sandbox_handle") == claim.get("sandbox_handle") == result.get("handle")
        and step.get("output_hash") == digest(result)
        and bool(artifacts) and all(row["step_id"] == step.get("id")
            and row["provenance"].get("epoch") == step.get("fencing_epoch")
            and row["provenance"].get("input_hash") == snapshot.get("input_hash")
            and hashlib.sha256(row["content"].encode()).hexdigest() == row["content_sha256"]
            and len(row["content"].encode()) == row["size"] for row in artifacts)
        and digest(sorted([{"path": row["path"], "sha256": row["content_sha256"], "size": row["size"],
                            "content": row["content"]} for row in artifacts], key=digest))
        == digest(sorted(result.get("artifacts", []), key=digest)),
        "COMPLETED_FENCED_ATTEMPT", "/steps/readiness", present=bool(step and claims))
    add("execution_cleanup", result.get("status") == "SUCCEEDED" and result.get("exit_code") == 0
        and result.get("cleanup") == "REMOVED" and inspection.get("cleanup_verified") is True
        and run.get("cleanup_required") is False,
        "TERMINAL_REMOVAL_RECEIPT", "/result_json/cleanup", present=bool(result))
    observed_profile = {key: inspection.get(key) for key in PROFILE}
    identity = scan.get("identity", {})
    add("resource_and_scan", digest(observed_profile) == digest(PROFILE)
        and scan.get("status") == "PASS" and scan.get("engine_executed") is True and scan.get("cleanup") == "REMOVED"
        and scan.get("source_hash") == inspection.get("code_hash") == snapshot.get("code_hash")
        and identity.get("scope") == scope.model_dump(mode="json") and identity.get("run_id") == run["id"]
        and identity.get("step_id") == step.get("id") and identity.get("attempt") == step.get("attempt"),
        "RECORDED_PROFILE_AND_EXACT_SCAN", "/result_json/inspect", present=bool(inspection and scan))
    protocol = _mcp_projection(scope, run["id"], run)
    add("mcp_receipt", protocol == "RECORDED", "RECORDED_MCP_PROTOCOL_REQUIRED", "/result_json/mcp",
        present=protocol not in {"UNKNOWN", "TEST_DRIVER"})
    verdict = ("NEEDS_REVIEW" if any(row.status == "FAIL" for row in checks) else
               "UNKNOWN" if any(row.status == "UNKNOWN" for row in checks) else "READY")
    gate = (run.get("assessment_json") or {}).get("status", "UNKNOWN")
    gate = gate if gate in {"PASS", "BLOCKED", "UNKNOWN"} else "UNKNOWN"
    handoff = ("NEEDS_OPERATIONAL_REVIEW" if verdict != "READY" else
               "READY_FOR_HUMAN_REVIEW" if gate == "PASS" else "NEEDS_TECHNICAL_EVIDENCE")
    approval_state = ("PENDING" if any(row["decision"] == "PENDING" for row in run["approvals"])
                      else "DECIDED" if run["approvals"] else "NOT_REQUESTED")
    if handoff == "READY_FOR_HUMAN_REVIEW" and (approval_state == "DECIDED" or run["receipts"]):
        handoff = "ALREADY_DECIDED_OR_EXECUTED"
    return OperationalReport(scope=scope, run_id=run["id"], source_hash=source_binding(run, events), skill_hash=skill_hash,
                             checks=tuple(checks), operational_readiness=verdict, technical_gate=gate,
                             supervisor_handoff=handoff,
                             human_approval_state=approval_state,
                             business_receipt_count=len(run["receipts"]))


class SREOperations:
    def __init__(self, source, *, skills=None):
        self.source = source
        self.skills = skills or SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS)

    def create_schema(self):
        Base.metadata.create_all(self.source.engine, tables=[SREReview.__table__])

    def _source(self, session, principal, scope, run_id):
        authorize(principal, scope, {"admin", "supervisor"})
        if principal.kind != "user":
            raise DomainError("HUMAN_SOP_REQUEST_REQUIRED", status=403)
        # Full caller source ACL is checked before a bounded SRE projection is produced.
        run = self.source._run_view(session, scope, self.source._get(session, Run, scope, run_id, lock=True), principal)
        events = [view(row) for row in session.scalars(select(Event).where(
            *filters(Event, scope), Event.run_id == run_id).order_by(Event.sequence)).all()]
        for row in events:
            authorize(principal, scope, classification=row["classification"])
        return run, events

    def _view(self, session, principal, scope, row):
        run, events = self._source(session, principal, scope, row.run_id)
        skill = self.skills.load("sre-operations", "sre")
        report = OperationalReport.model_validate(row.report_json)
        if row.source_hash != source_binding(run, events) or report.skill_hash != skill.sha256:
            raise DomainError("SRE_SOURCE_CHANGED")
        if digest(row.report_json) != digest(evaluate_operations(scope, run, events, skill.sha256)):
            raise DomainError("SRE_REPORT_CHANGED")
        if row.payload_hash != digest({"scope": values(scope), "report": row.report_json, "audit": row.audit_json}):
            raise DomainError("SRE_REPORT_CHANGED")
        return view(row)

    def review(self, principal, scope, run_id, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)
        with self.source._tx() as session:
            run, events = self._source(session, principal, scope, run_id)
            skill = self.skills.load("sre-operations", "sre")
            report = evaluate_operations(scope, run, events, skill.sha256).model_dump(mode="json")
            request_hash = digest({"scope": values(scope), "run_id": run_id, "revision": REVISION,
                                   "source_hash": report["source_hash"], "skill_hash": skill.sha256})
            audit = {"kind": "SRE_SOP_COMPLETED", "actor": principal.subject_id, "role": "sre", "scope": values(scope),
                     "run_id": run_id, "source_event_head": events[-1]["event_hash"] if events else None,
                     "report_hash": digest(report), "transport": "IN_PROCESS", "allowed_tools": list(skill.allowed_tools)}
            self.source._insert_ignore(session, SREReview, {
                **values(scope), "id": uid(), "run_id": run_id, "created_by": principal.subject_id,
                "idempotency_key": key, "request_hash": request_hash, "source_hash": report["source_hash"],
                "report_json": report, "audit_json": audit,
                "payload_hash": digest({"scope": values(scope), "report": report, "audit": audit}),
                "created_at": self.source._now(session),
            }, SCOPE + ["created_by", "idempotency_key"])
            row = session.scalar(select(SREReview).where(*filters(SREReview, scope),
                SREReview.created_by == principal.subject_id, SREReview.idempotency_key == key).with_for_update())
            if row.request_hash != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT")
            return self._view(session, principal, scope, row)

    def get(self, principal: Principal, scope: Scope, review_id: str):
        authorize(principal, scope, {"admin", "supervisor"})
        with self.source._tx() as session:
            return self._view(session, principal, scope, self.source._get(session, SREReview, scope, review_id))
