"""Bound synthetic EVT reviews and human acceptance, with no stage/action adapter."""

from datetime import datetime, timedelta

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import DomainError, authorize, digest
from .domain_store import DomainOperations, DomainReview
from .evt_contract import DEFAULT_POLICY, EVTPolicies, evaluate_evt
from .sre_ops import SREOperations, SREReview
from .store import SCOPE, Base, Event, Run, Scoped, aware, filters, fk, uid, values, view

REVISION = "evt-bound-review-v1"


class EVTReview(Scoped, Base):
    __tablename__ = "evt_reviews"
    __table_args__ = (fk("runs", local="run_id"), fk("domain_reviews", local="domain_review_id"),
                      fk("sre_reviews", local="sre_review_id"),
                      UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    domain_review_id: Mapped[str] = mapped_column(String(36))
    sre_review_id: Mapped[str] = mapped_column(String(36))
    policy_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    technical_source_hash: Mapped[str] = mapped_column(String(64))
    report_json: Mapped[dict] = mapped_column(JSON)
    report_hash: Mapped[str] = mapped_column(String(64))
    audit_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))


class EVTApproval(Scoped, Base):
    __tablename__ = "evt_review_approvals"
    __table_args__ = (fk("runs", local="run_id"), fk("evt_reviews", local="review_id"),
                      UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    review_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    proposal: Mapped[dict] = mapped_column(JSON)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16), default="PENDING")
    reviewer: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    audit_json: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))


def technical_source(run, events):
    completed = [event for event in events if event["kind"] == "CHECK_COMPLETED"]
    completion = completed[-1] if completed else None
    step_id = completion["step_id"] if completion else None
    return digest({"scope": {key: run[key] for key in SCOPE}, "run_id": run["id"],
                   "snapshot": run["snapshot_json"], "snapshot_hash": run["snapshot_hash"],
                   "result": run["result_json"], "assessment": run["assessment_json"],
                   "cleanup_required": run["cleanup_required"],
                   "steps": [row for row in run["steps"] if row["id"] == step_id],
                   "artifacts": [row for row in run["artifacts"] if row["step_id"] == step_id],
                   "events": [event for event in events if completion and event["sequence"] <= completion["sequence"]]})


def key_check(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)


def approval_hash(data):
    return digest({key: aware(value).isoformat() if isinstance(value, datetime) else value
                   for key, value in data.items() if key != "payload_hash"})


class _ReadableSRE(SREOperations):
    """Reviewers may validate existing reports; creation remains separately guarded."""

    def _source(self, session, principal, scope, run_id):
        authorize(principal, scope, {"admin", "supervisor", "reviewer"})
        if principal.kind != "user":
            raise DomainError("HUMAN_SOP_REQUEST_REQUIRED", status=403)
        run = self.source._run_view(session, scope, self.source._get(session, Run, scope, run_id, lock=True), principal)
        events = [view(row) for row in session.scalars(select(Event).where(
            *filters(Event, scope), Event.run_id == run_id).order_by(Event.sequence)).all()]
        for row in events:
            authorize(principal, scope, classification=row["classification"])
        return run, events


class EVTReviews:
    def __init__(self, source, *, policies=None):
        self.source = source
        self.domain = DomainOperations(source)
        self.sre = _ReadableSRE(source)
        self.domain.receipts = self.sre
        self.policies = policies or EVTPolicies()

    def create_schema(self):
        Base.metadata.create_all(self.source.engine, tables=[EVTReview.__table__, EVTApproval.__table__])

    def _inputs(self, session, principal, scope, run_id, domain_id, sre_id, policy_id):
        run, events = self.sre._source(session, principal, scope, run_id)
        domain = self.source._get(session, DomainReview, scope, domain_id)
        sre = self.source._get(session, SREReview, scope, sre_id)
        if domain.run_id != run_id or sre.run_id != run_id:
            raise DomainError("EVT_ROLE_RUN_MISMATCH", status=404)
        domain_view = self.domain._view(session, principal, scope, domain)
        sre_view = self.sre._view(session, principal, scope, sre)
        policy = self.policies.load(scope, policy_id)
        report = {**evaluate_evt(scope, run, domain.report_json, sre.report_json, policy),
                  "control_plane_revision": REVISION, "technical_source_hash": technical_source(run, events),
                  "role_refs": {"domain": {"id": domain_id, "report_hash": digest(domain_view["report_json"])},
                                "sre": {"id": sre_id, "report_hash": digest(sre_view["report_json"])}}}
        return report, run, events

    def _view(self, session, principal, scope, row):
        report, _, _ = self._inputs(session, principal, scope, row.run_id, row.domain_review_id,
                                    row.sre_review_id, row.policy_id)
        if row.technical_source_hash != report["technical_source_hash"] or digest(report) != row.report_hash:
            raise DomainError("EVT_SOURCE_CHANGED")
        if digest(row.report_json) != row.report_hash or row.payload_hash != digest({
                "scope": values(scope), "report": row.report_json, "audit": row.audit_json}):
            raise DomainError("EVT_REPORT_CHANGED")
        return view(row)

    def create(self, principal, scope, run_id, key, domain_review_id, sre_review_id, policy_id=DEFAULT_POLICY):
        authorize(principal, scope, {"admin", "supervisor"})
        key_check(key)
        with self.source._tx() as session:
            report, _, events = self._inputs(session, principal, scope, run_id, domain_review_id, sre_review_id, policy_id)
            report_hash = digest(report)
            audit = {"kind": "EVT_REVIEW_COMPLETED", "actor": principal.subject_id, "scope": values(scope),
                     "run_id": run_id, "observed_event_head": events[-1]["event_hash"] if events else None,
                     "report_hash": report_hash, "transport": "IN_PROCESS", "model_calls": 0}
            self.source._insert_ignore(session, EVTReview, {
                **values(scope), "id": uid(), "run_id": run_id, "created_by": principal.subject_id,
                "idempotency_key": key, "domain_review_id": domain_review_id, "sre_review_id": sre_review_id,
                "policy_id": policy_id, "request_hash": report_hash,
                "technical_source_hash": report["technical_source_hash"], "report_json": report,
                "report_hash": report_hash, "audit_json": audit,
                "payload_hash": digest({"scope": values(scope), "report": report, "audit": audit}),
                "created_at": self.source._now(session),
            }, SCOPE + ["created_by", "idempotency_key"])
            row = session.scalar(select(EVTReview).where(*filters(EVTReview, scope),
                EVTReview.created_by == principal.subject_id, EVTReview.idempotency_key == key).with_for_update())
            if row.request_hash != report_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT")
            return self._view(session, principal, scope, row)

    def get(self, principal, scope, review_id):
        authorize(principal, scope, {"admin", "supervisor", "reviewer"})
        with self.source._tx() as session:
            return self._view(session, principal, scope, self.source._get(session, EVTReview, scope, review_id))

    def request_approval(self, principal, scope, review_id, review_hash, key):
        authorize(principal, scope, {"admin", "supervisor"})
        key_check(key)
        with self.source._tx() as session:
            row = self.source._get(session, EVTReview, scope, review_id)
            reviewed = self._view(session, principal, scope, row)
            if row.report_hash != review_hash:
                raise DomainError("EVT_REVIEW_HASH_MISMATCH")
            if reviewed["report_json"]["status"] != "PASS":
                raise DomainError("EVT_GATE_NOT_PASS", status=403)
            run = self.source._get(session, Run, scope, row.run_id, lock=True)
            parent = self.source._run_view(session, scope, run, principal)
            if run.cancel_requested_at:
                raise DomainError("CANCEL_REQUESTED")
            proposal = {"scope": values(scope), "run_id": run.id, "review_id": row.id,
                        "review_hash": review_hash, "technical_source_hash": row.technical_source_hash,
                        "policy_id": row.policy_id, "policy_hash": row.report_json["policy_hash"],
                        "purpose": row.report_json["purpose"], "stage_transition_allowed": False,
                        "expected_parent_revision": run.revision,
                        "expected_stage_revision": parent["project"]["stage_revision"]}
            now = self.source._now(session)
            audit = {"kind": "EVT_ACCEPTANCE_REQUESTED", "actor": principal.subject_id,
                     "scope": values(scope), "proposal_hash": digest(proposal), "business_action": "NONE"}
            approval_data = {
                **values(scope), "id": uid(), "run_id": run.id, "review_id": row.id,
                "created_by": principal.subject_id, "idempotency_key": key,
                "proposal": proposal, "proposal_hash": digest(proposal), "decision": "PENDING", "revision": 0,
                "reviewer": None,
                "audit_json": audit, "created_at": now, "expires_at": now + timedelta(hours=1),
            }
            approval_data["payload_hash"] = approval_hash(approval_data)
            self.source._insert_ignore(session, EVTApproval, approval_data, SCOPE + ["created_by", "idempotency_key"])
            approval = session.scalar(select(EVTApproval).where(*filters(EVTApproval, scope),
                EVTApproval.created_by == principal.subject_id, EVTApproval.idempotency_key == key).with_for_update())
            if approval.proposal_hash != digest(proposal):
                raise DomainError("IDEMPOTENCY_CONFLICT")
            if approval.payload_hash != approval_hash(view(approval)):
                raise DomainError("EVT_APPROVAL_CHANGED")
            return view(approval)

    def get_approval(self, principal, scope, approval_id):
        authorize(principal, scope, {"admin", "supervisor", "reviewer"})
        with self.source._tx() as session:
            approval = self.source._get(session, EVTApproval, scope, approval_id)
            review = self.source._get(session, EVTReview, scope, approval.review_id)
            if review.run_id != approval.run_id:
                raise DomainError("EVT_APPROVAL_CHANGED")
            self._view(session, principal, scope, review)
            if approval.payload_hash != approval_hash(view(approval)):
                raise DomainError("EVT_APPROVAL_CHANGED")
            return view(approval)

    def decide(self, principal, scope, approval_id, proposal_hash, expected_revision, decision):
        authorize(principal, scope, {"reviewer"})
        if principal.kind != "user":
            raise DomainError("HUMAN_REVIEW_REQUIRED", status=403)
        if decision not in {"APPROVED", "REJECTED"}:
            raise DomainError("INVALID_DECISION", status=422)
        with self.source._tx() as session:
            approval = self.source._get(session, EVTApproval, scope, approval_id)
            run = self.source._get(session, Run, scope, approval.run_id, lock=True)
            approval = self.source._get(session, EVTApproval, scope, approval_id, lock=True)
            review = self.source._get(session, EVTReview, scope, approval.review_id)
            if review.run_id != run.id or approval.payload_hash != approval_hash(view(approval)):
                raise DomainError("EVT_APPROVAL_CHANGED")
            report = self._view(session, principal, scope, review)["report_json"]
            parent = self.source._run_view(session, scope, run, principal)
            proposal = approval.proposal
            expected = {"scope": values(scope), "run_id": run.id, "review_id": review.id,
                        "review_hash": review.report_hash, "technical_source_hash": review.technical_source_hash,
                        "policy_id": review.policy_id, "policy_hash": report["policy_hash"], "purpose": report["purpose"],
                        "stage_transition_allowed": False, "expected_parent_revision": run.revision,
                        "expected_stage_revision": parent["project"]["stage_revision"]}
            if digest(proposal) != approval.proposal_hash or digest(expected) != approval.proposal_hash:
                raise DomainError("EVT_PROPOSAL_CHANGED")
            if proposal_hash != approval.proposal_hash:
                raise DomainError("PROPOSAL_HASH_MISMATCH")
            if report["status"] != "PASS" or run.cancel_requested_at:
                raise DomainError("EVT_GATE_NOT_PASS", status=403)
            if approval.decision == decision and approval.reviewer == principal.subject_id:
                return {"approval": view(approval), "replayed": True, "business_action": "NONE"}
            if approval.revision != expected_revision:
                raise DomainError("REVISION_CONFLICT")
            if approval.decision != "PENDING":
                raise DomainError("APPROVAL_ALREADY_DECIDED")
            if aware(approval.expires_at) <= self.source._now(session):
                raise DomainError("APPROVAL_EXPIRED", status=410)
            approval.decision, approval.reviewer, approval.revision = decision, principal.subject_id, approval.revision + 1
            approval.audit_json = {"request": approval.audit_json, "decision": {
                "kind": "EVT_ACCEPTANCE_DECIDED", "actor": principal.subject_id, "scope": values(scope),
                "proposal_hash": approval.proposal_hash, "review_hash": review.report_hash,
                "decision": decision, "business_action": "NONE"}}
            approval.payload_hash = approval_hash(view(approval))
            return {"approval": view(approval), "replayed": False, "business_action": "NONE"}
