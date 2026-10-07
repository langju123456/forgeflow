"""Versioned independent approvals and atomic local effects for a synthetic flow.

All effects share PostgreSQL transactions with their receipts and audit events.
There is no remote IssueTracker call here. Compensation preserves evidence.
"""

import hashlib
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import DomainError, authorize, digest
from .evt_review import key_check
from .evt_store import EVTBoundStore
from .fabric_contract import FABRIC_SCOPE, REVISION, evaluate_fabric
from .sre_ops import event_chain_valid
from .store import SCOPE, Base, Event, Run, Scoped, aware, filters, fk, uid, values, view

FORWARD = {"TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE"}
REVERSE = {"STAGE_REVERT": "TEST_STAGE", "ISSUE_RETRACT": "ISSUE", "MEMORY_REVOKE": "MEMORY_ACTIVATE"}


class FabricReview(Scoped, Base):
    __tablename__ = "fabric_reviews"
    __table_args__ = (fk("runs", local="run_id"), fk("events", local="event_id"),
                      UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    fixture_id: Mapped[str] = mapped_column(String(32))
    report_json: Mapped[dict] = mapped_column(JSON)
    report_hash: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(36))
    payload_hash: Mapped[str] = mapped_column(String(64))


class FabricApproval(Scoped, Base):
    __tablename__ = "fabric_approvals"
    __table_args__ = (fk("runs", local="run_id"), fk("fabric_reviews", local="review_id"),
                      UniqueConstraint(*SCOPE, "created_by", "idempotency_key"),
                      UniqueConstraint(*SCOPE, "run_id", "kind", "target_receipt_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    review_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    target_receipt_id: Mapped[str] = mapped_column(String(36), default="")
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    proposal: Mapped[dict] = mapped_column(JSON)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16), default="PENDING")
    reviewer: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    decision_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    payload_hash: Mapped[str] = mapped_column(String(64))


class FabricApprovalVersion(Scoped, Base):
    """A superseded version of an approval: its proposal, decision and reviewer, kept unchanged.

    The live approval row keeps its id and always holds the current version; earlier versions live here.
    """
    __tablename__ = "fabric_approval_versions"
    __table_args__ = (fk("runs", local="run_id"), fk("fabric_approvals", local="approval_id"),
                      fk("events", local="event_id"), UniqueConstraint(*SCOPE, "approval_id", "version"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    review_id: Mapped[str] = mapped_column(String(36))
    proposal: Mapped[dict] = mapped_column(JSON)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    reviewer: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(Integer)
    decision_event_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str] = mapped_column(String(128))
    request_key: Mapped[str] = mapped_column(String(128))
    reasons: Mapped[list] = mapped_column(JSON)
    event_id: Mapped[str] = mapped_column(String(36))
    payload_hash: Mapped[str] = mapped_column(String(64))


class FabricReceipt(Scoped, Base):
    __tablename__ = "fabric_receipts"
    __table_args__ = (fk("runs", local="run_id"), fk("fabric_approvals", local="approval_id"),
                      fk("events", local="event_id"), UniqueConstraint(*SCOPE, "approval_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    effect_json: Mapped[dict] = mapped_column(JSON)
    event_id: Mapped[str] = mapped_column(String(36))
    executed_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    payload_hash: Mapped[str] = mapped_column(String(64))


class FabricItem(Scoped, Base):
    """Actual local tracker entries and approved Memory references, not remote tickets."""
    __tablename__ = "fabric_items"
    __table_args__ = (fk("runs", local="run_id"), fk("fabric_approvals", local="approval_id"),
                      fk("memories", local="memory_id"), UniqueConstraint(*SCOPE, "approval_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(String(36))
    memory_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))


def row_hash(row):
    return digest({key: val for key, val in view(row).items() if key != "payload_hash"})


def seal(row):
    row.payload_hash = row_hash(row)


def checked(row):
    if row.payload_hash != row_hash(row):
        raise DomainError("FABRIC_RECORD_CHANGED")
    return row


def adapter_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class _TransactionStore(EVTBoundStore):
    def __init__(self, source, session):
        self.__dict__.update(source.__dict__)
        self.session = session

    @contextmanager
    def _tx(self):
        yield self.session


class FabricOperations:
    def __init__(self, source):
        self.source = source

    def create_schema(self):
        Base.metadata.create_all(self.source.engine, tables=[FabricReview.__table__, FabricApproval.__table__,
                                                          FabricApprovalVersion.__table__,
                                                          FabricReceipt.__table__, FabricItem.__table__])

    def _auth(self, p, scope, roles=None):
        authorize(p, scope, roles if roles is not None else {"admin", "supervisor", "reviewer", "fabric_executor"})
        if scope != FABRIC_SCOPE:
            raise DomainError("NOT_FOUND", status=404)

    def _source(self, session, p, scope, run_id, *, fresh=True):
        run = self.source._get(session, Run, scope, run_id, lock=True)
        material = self.source._run_view(session, scope, run, p)
        events = [view(row) for row in session.scalars(select(Event).where(
            *filters(Event, scope), Event.run_id == run_id).order_by(Event.sequence))]
        for event in events:
            authorize(p, scope, classification=event["classification"])
        if not event_chain_valid(scope, run_id, events):
            raise DomainError("AUDIT_CHAIN_CHANGED")
        if fresh:
            if not run.snapshot_json:
                raise DomainError("FABRIC_EVIDENCE_REQUIRED")
            if run.cancel_requested_at or run.cleanup_required:
                raise DomainError("FABRIC_RUN_NOT_EXECUTABLE")
            self.source._fresh(session, p, scope, run)
        return run, material, events

    def _review(self, session, p, scope, row, *, fresh=True):
        checked(row)
        run, material, events = self._source(session, p, scope, row.run_id, fresh=fresh)
        if digest(row.report_json) != row.report_hash:
            raise DomainError("FABRIC_REPORT_CHANGED")
        if fresh and digest(evaluate_fabric(scope, material, events, row.fixture_id)) != row.report_hash:
            raise DomainError("FABRIC_SOURCE_CHANGED")
        return run

    def create(self, p, scope, run_id, key, fixture_id="aligned"):
        self._auth(p, scope, {"admin", "supervisor"})
        if p.kind != "user":
            raise DomainError("HUMAN_REQUEST_REQUIRED", status=403)
        key_check(key)
        with self.source._tx() as session:
            run, material, events = self._source(session, p, scope, run_id)
            report = evaluate_fabric(scope, material, events, fixture_id)
            old = session.scalar(select(FabricReview).where(*filters(FabricReview, scope),
                FabricReview.created_by == p.subject_id, FabricReview.idempotency_key == key))
            if old:
                self._review(session, p, scope, old)
                if old.run_id != run_id or old.fixture_id != fixture_id or old.report_hash != digest(report):
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                return view(old)
            identifier = uid()
            event = self.source._event(session, p, scope, run, "FABRIC_REVIEW_COMPLETED", {
                "review_id": identifier, "report_hash": digest(report), "technical_hash": report["technical_hash"],
                "flow_gate": report["flow_gate"], "evt_gate": report["evt_gate"], "transport": "IN_PROCESS"})
            row = FabricReview(**values(scope), id=identifier, run_id=run_id, created_by=p.subject_id,
                idempotency_key=key, fixture_id=fixture_id, report_json=report, report_hash=digest(report),
                event_id=event.id, payload_hash="")
            seal(row)
            session.add(row)
            session.flush()
            return view(row)

    def get(self, p, scope, review_id):
        self._auth(p, scope)
        with self.source._tx() as session:
            row = self.source._get(session, FabricReview, scope, review_id)
            self._review(session, p, scope, row)
            return view(row)

    def _receipt(self, session, p, scope, receipt_id, run_id):
        row = checked(self.source._get(session, FabricReceipt, scope, receipt_id))
        if row.run_id != run_id:
            raise DomainError("NOT_FOUND", status=404)
        event = self.source._get(session, Event, scope, row.event_id)
        authorize(p, scope, classification=event.classification)
        if event.run_id != run_id or event.payload.get("receipt_id") != row.id or (
                event.payload.get("effect_hash") != digest(row.effect_json)):
            raise DomainError("FABRIC_RECEIPT_CHANGED")
        return row

    def _parameters(self, session, p, scope, run, review, kind, target):
        project = self.source._project(session, scope, lock=True)
        if kind == "TEST_STAGE":
            if project.current_stage != "PROTOTYPE":
                raise DomainError("STAGE_CONFLICT")
            return {"from_stage": "PROTOTYPE", "to_stage": "TEST_STAGE", "expected_revision": project.stage_revision}
        if kind == "ISSUE":
            return {"title": "Synthetic Aurora local workflow follow-up", "review_hash": review.report_hash,
                    "evt_gate": review.report_json["evt_gate"], "remote_tracker": False}
        if kind == "MEMORY_ACTIVATE":
            receipts = session.scalars(select(FabricReceipt).where(*filters(FabricReceipt, scope),
                FabricReceipt.run_id == run.id, FabricReceipt.kind.in_(["TEST_STAGE", "ISSUE"]))).all()
            if {row.kind for row in receipts} != {"TEST_STAGE", "ISSUE"}:
                raise DomainError("FABRIC_EFFECT_SOURCES_REQUIRED")
            for row in receipts:
                self._receipt(session, p, scope, row.id, run.id)
            return {"subject": "Aurora synthetic local workflow", "status": "APPROVED_REFERENCE",
                    "source_event_ids": sorted([review.event_id] + [row.event_id for row in receipts]),
                    "receipt_hashes": {row.id: row.payload_hash for row in receipts}}
        receipt = self._receipt(session, p, scope, target, run.id)
        if receipt.kind != REVERSE[kind]:
            raise DomainError("FABRIC_COMPENSATION_MISMATCH")
        if kind == "STAGE_REVERT":
            after = receipt.effect_json["after"]
            if project.current_stage != after["stage"] or project.stage_revision != after["revision"]:
                raise DomainError("STAGE_CONFLICT")
            return {"from_stage": after["stage"], "to_stage": receipt.effect_json["before"]["stage"],
                    "expected_revision": project.stage_revision, "receipt_hash": receipt.payload_hash}
        item = checked(self.source._get(session, FabricItem, scope, receipt.effect_json["item_id"], lock=True))
        if item.status != receipt.effect_json["after"]["status"] or item.revision != receipt.effect_json["after"]["revision"]:
            raise DomainError("FABRIC_ITEM_CONFLICT")
        return {"item_id": item.id, "from_status": item.status, "expected_revision": item.revision,
                "to_status": "RETRACTED" if kind == "ISSUE_RETRACT" else "REVOKED", "receipt_hash": receipt.payload_hash}

    def _proposal(self, p, run, review, kind, target, parameters, *, version, supersedes):
        return {"schema": "fabric-action-v1", "revision": REVISION, "scope": values(FABRIC_SCOPE),
                "run_id": run.id, "review_id": review.id, "review_hash": review.report_hash,
                "technical_hash": review.report_json["technical_hash"], "kind": kind,
                "target_receipt_id": target, "parameters": parameters, "adapter_hash": adapter_hash(),
                "approval_provenance": "TEST_DRIVER", "simulation": True, "full_evt_eligibility": False,
                "external_writes": False, "compensation_allows_stale_technical_evidence": kind in REVERSE,
                "proposed_by": p.subject_id, "version": version, "supersedes": supersedes}

    def _stale_reasons(self, session, p, scope, row, now):
        """Why an unconsumed proposal can no longer be approved or executed; empty when it is current."""
        if row.decision == "REJECTED" or session.scalar(select(FabricReceipt.id).where(
                *filters(FabricReceipt, scope), FabricReceipt.approval_id == row.id)):
            return []  # a rejection is final and a consumed approval is history
        reasons = []
        if row.proposal.get("adapter_hash") != adapter_hash():
            reasons.append("ADAPTER_CHANGED")
        if now >= aware(row.expires_at):
            reasons.append("EXPIRED")
        review = self.source._get(session, FabricReview, scope, row.review_id)
        try:
            self._review(session, p, scope, review, fresh=row.kind in FORWARD)
            if row.proposal.get("review_hash") != review.report_hash:
                reasons.append("REVIEW_STALE")
        except DomainError as error:
            if error.code not in {"FABRIC_SOURCE_CHANGED", "EVIDENCE_EXPIRED", "SOURCE_CHANGED",
                                  "FABRIC_EVIDENCE_REQUIRED"}:
                raise
            reasons.append("REVIEW_STALE")
        return reasons

    def _proposers(self, session, scope, row):
        """Everyone who created or renewed any version of this approval; none of them may decide it."""
        people = {row.created_by, row.proposal.get("proposed_by")}
        for version in session.scalars(select(FabricApprovalVersion).where(
                *filters(FabricApprovalVersion, scope), FabricApprovalVersion.approval_id == row.id)):
            checked(version)
            people |= {version.superseded_by, version.proposal.get("proposed_by")}
        return people - {None}

    def _supersede(self, session, p, scope, run, review, row, key, reasons, parameters):
        """Replace the current version of an unconsumed approval; the old one is kept and audited."""
        checked(row)
        if digest(row.proposal) != row.proposal_hash:
            raise DomainError("FABRIC_RECORD_CHANGED")
        number = session.scalar(select(func.count()).select_from(FabricApprovalVersion).where(
            *filters(FabricApprovalVersion, scope), FabricApprovalVersion.approval_id == row.id)) + 1
        proposal = self._proposal(p, run, review, row.kind, row.target_receipt_id, parameters,
                                  version=number + 1, supersedes=row.proposal_hash)
        event = self.source._event(session, p, scope, run, "FABRIC_APPROVAL_SUPERSEDED", {
            "approval_id": row.id, "kind": row.kind, "version": number, "reasons": reasons,
            "old_proposal_hash": row.proposal_hash, "new_proposal_hash": digest(proposal),
            "old_adapter_hash": row.proposal.get("adapter_hash"), "new_adapter_hash": adapter_hash(),
            "old_decision": row.decision, "provenance": "TEST_DRIVER"})
        archive = FabricApprovalVersion(
            **values(scope), id=uid(), run_id=row.run_id, approval_id=row.id, version=number,
            review_id=row.review_id, proposal=row.proposal, proposal_hash=row.proposal_hash,
            decision=row.decision, reviewer=row.reviewer, revision=row.revision,
            decision_event_id=row.decision_event_id, created_at=row.created_at, expires_at=row.expires_at,
            superseded_by=p.subject_id, request_key=key, reasons=reasons, event_id=event.id, payload_hash="")
        seal(archive)
        session.add(archive)
        now = self.source._now(session)
        row.review_id, row.proposal, row.proposal_hash = review.id, proposal, digest(proposal)
        row.decision, row.reviewer, row.decision_event_id = "PENDING", None, None
        row.revision, row.created_at, row.expires_at = row.revision + 1, now, now + timedelta(hours=1)
        seal(row)
        session.flush()

    def propose(self, p, scope, review_id, review_hash, key, kind, target_receipt_id=None):
        self._auth(p, scope, {"admin", "supervisor"})
        if p.kind != "user":
            raise DomainError("HUMAN_REQUEST_REQUIRED", status=403)
        key_check(key)
        if kind not in FORWARD | REVERSE.keys() or bool(target_receipt_id) != (kind in REVERSE):
            raise DomainError("INVALID_FABRIC_ACTION", status=422)
        target = target_receipt_id or ""
        with self.source._tx() as session:
            review = self.source._get(session, FabricReview, scope, review_id)
            run = self._review(session, p, scope, review, fresh=kind in FORWARD)
            if review.report_hash != review_hash:
                raise DomainError("FABRIC_REPORT_HASH_MISMATCH")
            if kind in FORWARD and review.report_json["flow_gate"] != "PASS":
                raise DomainError("FABRIC_GATE_NOT_PASS")
            old = session.scalar(select(FabricApproval).where(*filters(FabricApproval, scope),
                FabricApproval.created_by == p.subject_id, FabricApproval.idempotency_key == key))
            duplicate = session.scalar(select(FabricApproval).where(*filters(FabricApproval, scope),
                FabricApproval.run_id == run.id, FabricApproval.kind == kind,
                FabricApproval.target_receipt_id == target))
            existing = old or duplicate
            if existing is not None:
                checked(existing)
                if existing.run_id != run.id or (existing.kind, existing.target_receipt_id) != (kind, target):
                    raise DomainError("IDEMPOTENCY_CONFLICT")
                renewed = session.scalar(select(FabricApprovalVersion.id).where(
                    *filters(FabricApprovalVersion, scope), FabricApprovalVersion.approval_id == existing.id,
                    FabricApprovalVersion.request_key == key,
                    FabricApprovalVersion.superseded_by == p.subject_id))
                if renewed and existing.review_id == review.id:
                    return view(existing)  # this request already produced the current version
                reasons = self._stale_reasons(session, p, scope, existing, self.source._now(session))
                if not reasons:
                    if old is not None:
                        if old.review_id != review_id:
                            raise DomainError("IDEMPOTENCY_CONFLICT")
                        return view(old)
                    raise DomainError("FABRIC_ACTION_ALREADY_PROPOSED")
                parameters = self._parameters(session, p, scope, run, review, kind, target)
                self._supersede(session, p, scope, run, review, existing, key, reasons, parameters)
                return view(existing)
            parameters = self._parameters(session, p, scope, run, review, kind, target)
            proposal = self._proposal(p, run, review, kind, target, parameters, version=1, supersedes=None)
            now = self.source._now(session)
            row = FabricApproval(**values(scope), id=uid(), run_id=run.id, review_id=review.id, kind=kind,
                target_receipt_id=target, created_by=p.subject_id, idempotency_key=key,
                proposal=proposal, proposal_hash=digest(proposal), decision="PENDING", reviewer=None,
                revision=0, decision_event_id=None, created_at=now, expires_at=now + timedelta(hours=1), payload_hash="")
            seal(row)
            session.add(row)
            self.source._event(session, p, scope, run, "FABRIC_ACTION_PROPOSED", {
                "approval_id": row.id, "kind": kind, "proposal_hash": row.proposal_hash, "provenance": "TEST_DRIVER"})
            session.flush()
            return view(row)

    def _approval(self, session, p, scope, row, *, fresh=True, adapter=True):
        """Verify an approval. adapter=False is for reading history: a code upgrade must not hide it."""
        checked(row)
        if digest(row.proposal) != row.proposal_hash or (adapter and row.proposal["adapter_hash"] != adapter_hash()):
            raise DomainError("FABRIC_PROPOSAL_CHANGED")
        review = self.source._get(session, FabricReview, scope, row.review_id)
        run = self._review(session, p, scope, review, fresh=fresh and row.kind in FORWARD)
        if row.run_id != review.run_id or row.proposal["review_hash"] != review.report_hash:
            raise DomainError("FABRIC_PROPOSAL_CHANGED")
        return run, review

    def get_approval(self, p, scope, approval_id):
        self._auth(p, scope)
        with self.source._tx() as session:
            row = self.source._get(session, FabricApproval, scope, approval_id)
            self._approval(session, p, scope, row, fresh=False, adapter=False)
            history = []
            for version in session.scalars(select(FabricApprovalVersion).where(
                    *filters(FabricApprovalVersion, scope), FabricApprovalVersion.approval_id == row.id
                    ).order_by(FabricApprovalVersion.version)):
                checked(version)
                history.append({"version": version.version, "proposal_hash": version.proposal_hash,
                                "review_id": version.review_id, "decision": version.decision,
                                "reviewer": version.reviewer, "revision": version.revision,
                                "superseded_by": version.superseded_by, "reasons": version.reasons,
                                "event_id": version.event_id})
            return {**view(row), "adapter_current": row.proposal["adapter_hash"] == adapter_hash(),
                    "history": history}

    def decide(self, p, scope, approval_id, proposal_hash, expected_revision, decision):
        self._auth(p, scope, {"reviewer"})
        if p.kind != "user":
            raise DomainError("HUMAN_REVIEW_REQUIRED", status=403)
        if decision not in {"APPROVED", "REJECTED"}:
            raise DomainError("INVALID_DECISION", status=422)
        with self.source._tx() as session:
            initial = self.source._get(session, FabricApproval, scope, approval_id)
            self.source._get(session, Run, scope, initial.run_id, lock=True)
            row = self.source._get(session, FabricApproval, scope, approval_id, lock=True)
            run, review = self._approval(session, p, scope, row, fresh=decision == "APPROVED")
            if p.subject_id in self._proposers(session, scope, row):
                raise DomainError("INDEPENDENT_REVIEW_REQUIRED", status=403)
            if proposal_hash != row.proposal_hash:
                raise DomainError("PROPOSAL_HASH_MISMATCH")
            if row.decision != "PENDING":
                if row.decision == decision and row.reviewer == p.subject_id and expected_revision == row.revision - 1:
                    return {"approval": view(row), "replayed": True, "executed": False}
                raise DomainError("APPROVAL_ALREADY_DECIDED")
            if row.revision != expected_revision:
                raise DomainError("REVISION_CONFLICT")
            if self.source._now(session) >= aware(row.expires_at):
                raise DomainError("APPROVAL_EXPIRED")
            if decision == "APPROVED":
                if row.kind in FORWARD and review.report_json["flow_gate"] != "PASS":
                    raise DomainError("FABRIC_GATE_NOT_PASS")
                if self._parameters(session, p, scope, run, review, row.kind, row.target_receipt_id) != row.proposal["parameters"]:
                    raise DomainError("FABRIC_PARAMETERS_CHANGED")
            row.decision, row.reviewer, row.revision = decision, p.subject_id, row.revision + 1
            event = self.source._event(session, p, scope, run, "FABRIC_APPROVAL_DECIDED", {
                "approval_id": row.id, "proposal_hash": row.proposal_hash, "decision": decision, "reviewer": p.subject_id,
                "provenance": "TEST_DRIVER", "execution": "NOT_EXECUTED"})
            row.decision_event_id = event.id
            seal(row)
            session.flush()
            return {"approval": view(row), "replayed": False, "executed": False}

    def execute(self, p, scope, approval_id, *, _before_commit=None):
        self._auth(p, scope, {"fabric_executor"})
        if p.kind != "service":
            raise DomainError("TRUSTED_EXECUTOR_REQUIRED", status=403)
        with self.source._tx() as session:
            initial = self.source._get(session, FabricApproval, scope, approval_id)
            self.source._get(session, Run, scope, initial.run_id, lock=True)
            row = self.source._get(session, FabricApproval, scope, approval_id, lock=True)
            run, review = self._approval(session, p, scope, row, fresh=False, adapter=False)
            receipt = session.scalar(select(FabricReceipt).where(*filters(FabricReceipt, scope), FabricReceipt.approval_id == row.id))
            if receipt:
                receipt = self._receipt(session, p, scope, receipt.id, run.id)
                return {"receipt": view(receipt), "replayed": True}
            run, review = self._approval(session, p, scope, row)
            if row.decision != "APPROVED" or not row.decision_event_id:
                raise DomainError("APPROVAL_REQUIRED")
            if self.source._now(session) >= aware(row.expires_at):
                raise DomainError("APPROVAL_EXPIRED")
            parameters = self._parameters(session, p, scope, run, review, row.kind, row.target_receipt_id)
            if parameters != row.proposal["parameters"]:
                raise DomainError("FABRIC_PARAMETERS_CHANGED")
            effect = self._effect(session, p, scope, run, review, row, parameters)
            identifier = uid()
            event = self.source._event(session, p, scope, run, "FABRIC_ACTION_EXECUTED", {
                "receipt_id": identifier, "approval_id": row.id, "kind": row.kind,
                "proposal_hash": row.proposal_hash, "effect_hash": digest(effect), "transport": "IN_PROCESS",
                "provenance": "TEST_DRIVER", "external_writes": False})
            receipt = FabricReceipt(**values(scope), id=identifier, run_id=run.id, approval_id=row.id,
                kind=row.kind, effect_json=effect, event_id=event.id, executed_at=self.source._now(session), payload_hash="")
            seal(receipt)
            session.add(receipt)
            run.revision += 1
            session.flush()
            if _before_commit:
                _before_commit()  # Trusted test harness only; never exposed as an HTTP parameter.
            return {"receipt": view(receipt), "replayed": False}

    def _effect(self, session, p, scope, run, review, row, params):
        if row.kind in {"TEST_STAGE", "STAGE_REVERT"}:
            project = self.source._project(session, scope, lock=True)
            before = {"stage": project.current_stage, "revision": project.stage_revision}
            project.current_stage, project.stage_revision = params["to_stage"], project.stage_revision + 1
            return {"before": before, "after": {"stage": project.current_stage, "revision": project.stage_revision},
                    "simulation": True, "target_receipt_id": row.target_receipt_id}
        if row.kind in {"ISSUE_RETRACT", "MEMORY_REVOKE"}:
            item = checked(self.source._get(session, FabricItem, scope, params["item_id"], lock=True))
            before = {"status": item.status, "revision": item.revision}
            item.status, item.revision = params["to_status"], item.revision + 1
            seal(item)
            return {"item_id": item.id, "before": before, "after": {"status": item.status, "revision": item.revision},
                    "target_receipt_id": row.target_receipt_id}
        memory_id = None
        body = {"parameters": params, "review_id": review.id, "review_hash": review.report_hash,
                "technical_hash": review.report_json["technical_hash"], "simulation": True}
        if row.kind == "MEMORY_ACTIVATE":
            candidate = _TransactionStore(self.source, session).write_memory(p, scope, run.id, params["subject"],
                "Synthetic local Stage and Issue effects completed. EVT remains " + review.report_json["evt_gate"] +
                "; APK/device checks NOT_RUN. Review " + review.id + " hash " + review.report_hash,
                params["source_event_ids"], "CANDIDATE", 0)
            memory_id = candidate["id"]
            body["memory_source_hash"] = candidate["source_hash"]
            body["source_event_ids"] = params["source_event_ids"]
            body["source_event_hashes"] = {event_id: self.source._get(session, Event, scope, event_id).event_hash
                                         for event_id in params["source_event_ids"]}
        item = FabricItem(**values(scope), id=uid(), run_id=run.id, approval_id=row.id, memory_id=memory_id,
            kind="ISSUE" if row.kind == "ISSUE" else "MEMORY", status="OPEN" if row.kind == "ISSUE" else "ACTIVE",
            revision=0, body=body, payload_hash="")
        seal(item)
        session.add(item)
        session.flush()
        return {"item_id": item.id, "memory_id": memory_id, "before": None,
                "after": {"status": item.status, "revision": item.revision}, "item_hash": item.payload_hash}

    def memory(self, p, scope, query=""):
        self._auth(p, scope)
        if not isinstance(query, str) or len(query) > 500:
            raise DomainError("INVALID_QUERY", status=422)
        with self.source._tx() as session:
            # Scope and current run ACL checks precede loading/scoring Memory content.
            metadata = session.execute(select(FabricItem.id, FabricItem.run_id).where(
                *filters(FabricItem, scope), FabricItem.kind == "MEMORY", FabricItem.status == "ACTIVE")).all()
            allowed = []
            for item_id, run_id in metadata:
                try:
                    self._source(session, p, scope, run_id)
                    allowed.append(item_id)
                except DomainError:
                    continue
            result = []
            for item in session.scalars(select(FabricItem).where(*filters(FabricItem, scope), FabricItem.id.in_(allowed))):
                checked(item)
                approval = self.source._get(session, FabricApproval, scope, item.approval_id)
                self._approval(session, p, scope, approval, adapter=False)
                candidate = _TransactionStore(self.source, session).cite(p, scope, "memory", item.memory_id)
                now = self.source._now(session)
                if (candidate["status"] not in {"CANDIDATE", "CONFIRMED"}
                        or aware(candidate["valid_from"]) > now
                        or (candidate["valid_until"] and aware(candidate["valid_until"]) <= now)):
                    continue
                if (candidate["source_hash"] != item.body["memory_source_hash"] or
                        hashlib.sha256(candidate["content"].encode()).hexdigest() != candidate["source_hash"] or
                        sorted(candidate["source_event_ids"]) != item.body["source_event_ids"] or
                        {event["id"]: event["event_hash"] for event in candidate["sources"]} != item.body["source_event_hashes"]):
                    raise DomainError("FABRIC_MEMORY_SOURCE_CHANGED")
                if query.casefold() in (candidate["subject"] + " " + candidate["content"]).casefold():
                    result.append({"reference": view(item), "citation": candidate,
                        "reference_status": "ACTIVE", "underlying_status": candidate["status"],
                        "approval_provenance": "TEST_DRIVER", "confirms_evt": False})
            return result
