"""Persist supplemental Architect/Android SOPs without changing the EVT gate."""

from pathlib import Path

from sqlalchemy import JSON, DateTime, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import DomainError, Principal, Scope, authorize, digest
from .domain_checks import ROLE_TOOLS, DomainArtifacts, evaluate_domain
from .skills import SkillRegistry
from .sre_ops import SREOperations, source_binding
from .store import SCOPE, Base, Scoped, filters, fk, uid, values, view

REVISION = "domain-source-contract-v1"
ROOT = Path(__file__).resolve().parents[2]
SKILLS = {"architect": "architect-contract", "android": "android-manifest"}


class DomainReview(Scoped, Base):
    __tablename__ = "domain_reviews"
    __table_args__ = (fk("runs", local="run_id"), UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    fixture_id: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    report_json: Mapped[dict] = mapped_column(JSON)
    audit_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))


class DomainOperations:
    def __init__(self, source, *, artifacts=None, skills=None):
        self.source = source
        self.receipts = SREOperations(source)
        self.artifacts = artifacts or DomainArtifacts()
        self.skills = skills or SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS)

    def create_schema(self):
        Base.metadata.create_all(self.source.engine, tables=[DomainReview.__table__])

    def _inputs(self, session, principal, scope, run_id, fixture_id):
        # Reuse whole-run ACL and project/run locking before reading domain artifacts.
        run, events = self.receipts._source(session, principal, scope, run_id)
        bundle = self.artifacts.load(scope, fixture_id)
        skills = {role: self.skills.load(name, role) for role, name in SKILLS.items()}
        for role, skill in skills.items():
            if set(skill.allowed_tools) != ROLE_TOOLS[role]:
                raise DomainError("DOMAIN_SKILL_CAPABILITY_MISSING", status=403)
        hashes = {role: skill.sha256 for role, skill in skills.items()}
        binding = digest({"revision": REVISION, "parent": source_binding(run, events),
                          "fixture_id": fixture_id, "artifact_hash": bundle["artifact_hash"], "skills": hashes})
        report = {"control_plane_revision": REVISION, "scope": values(scope), "run_id": run_id, "source_hash": binding,
                  "fixture_id": fixture_id, "synthetic": True,
                  "observation": "SOURCE_XML_AND_RECORDED_HARDWARE_NOT_APK_OR_DEVICE",
                  "sources": bundle["sources"], "skill_hashes": hashes,
                  "technical_gate": (run.get("assessment_json") or {}).get("status", "UNKNOWN"),
                  "gate_integration": "SUPPLEMENTAL_NOT_ENFORCED_BY_EXISTING_EVT_GATE",
                  "human_approval_required": True, "business_actions_executed_by_sop": False,
                  **evaluate_domain(scope, run, events, bundle, hashes)}
        return report, skills, events

    def _view(self, session, principal, scope, row):
        report, _, _ = self._inputs(session, principal, scope, row.run_id, row.fixture_id)
        if row.source_hash != report["source_hash"]:
            raise DomainError("DOMAIN_SOURCE_CHANGED")
        if digest(report) != digest(row.report_json) or row.payload_hash != digest({
                "scope": values(scope), "report": row.report_json, "audit": row.audit_json}):
            raise DomainError("DOMAIN_REPORT_CHANGED")
        return view(row)

    def review(self, principal, scope, run_id, key, fixture_id="aligned"):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)
        with self.source._tx() as session:
            report, skills, events = self._inputs(session, principal, scope, run_id, fixture_id)
            request_hash = digest({"scope": values(scope), "run_id": run_id, "source_hash": report["source_hash"]})
            audit = {"kind": "DOMAIN_SOP_COMPLETED", "actor": principal.subject_id, "scope": values(scope),
                     "run_id": run_id, "source_event_head": events[-1]["event_hash"] if events else None,
                     "report_hash": digest(report), "transport": "IN_PROCESS", "model_calls": 0,
                     "allowed_tools": {role: list(skill.allowed_tools) for role, skill in skills.items()}}
            self.source._insert_ignore(session, DomainReview, {
                **values(scope), "id": uid(), "run_id": run_id, "created_by": principal.subject_id,
                "idempotency_key": key, "fixture_id": fixture_id, "request_hash": request_hash,
                "source_hash": report["source_hash"], "report_json": report, "audit_json": audit,
                "payload_hash": digest({"scope": values(scope), "report": report, "audit": audit}),
                "created_at": self.source._now(session),
            }, SCOPE + ["created_by", "idempotency_key"])
            row = session.scalar(select(DomainReview).where(*filters(DomainReview, scope),
                DomainReview.created_by == principal.subject_id, DomainReview.idempotency_key == key).with_for_update())
            if row.request_hash != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT")
            return self._view(session, principal, scope, row)

    def get(self, principal: Principal, scope: Scope, review_id: str):
        authorize(principal, scope, {"admin", "supervisor"})
        with self.source._tx() as session:
            return self._view(session, principal, scope, self.source._get(session, DomainReview, scope, review_id))
