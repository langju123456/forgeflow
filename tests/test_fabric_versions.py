# ruff: noqa: F811  (the db fixture is imported from test_fabric_store and used as an argument)
"""A code upgrade must not strand history or pending work (F1).

The adapter hash is the sha256 of fabric_store.py, so any change to that file changes it. Committed
receipts stay readable and replayable; an unconsumed proposal that no longer matches the current code,
review or time gets an audited new version that must be approved again. Nothing is deleted, and a new
effect is still checked against the current parameters, sources and adapter.
"""

from datetime import timedelta

from sqlalchemy import select
from test_fabric_store import (  # noqa: F401
    approve,
    completed,
    count,
    db,
    execute,
    project,
    propose,
    rejects,
    review,
)

from forgeflow import contracts
from forgeflow import fabric_store as fabric_store_module
from forgeflow.contracts import Principal
from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_store import FabricApproval, FabricReceipt, seal
from forgeflow.store import Event, filters

NEW_ADAPTER = "e" * 64


def upgrade(monkeypatch):
    monkeypatch.setattr(fabric_store_module, "adapter_hash", lambda: NEW_ADAPTER)


def read(db, approval):
    return db.service.get_approval(db.reviewer, db.scope, approval["id"])


def decide_new(db, approval, decision="APPROVED"):
    return db.service.decide(db.reviewer, db.scope, approval["id"], approval["proposal_hash"],
                             approval["revision"], decision)


def kinds(db):
    with db.source._tx() as session:
        return [row.kind for row in session.scalars(select(Event).where(
            *filters(Event, db.scope), Event.run_id == db.run_id).order_by(Event.sequence))]


def test_committed_receipt_replays_read_only_after_an_adapter_upgrade(db, monkeypatch):
    db = db()
    report = review(db)
    receipt = completed(db, report, "ISSUE")
    approval = propose(db, report, "ISSUE")
    upgrade(monkeypatch)
    assert db.service.execute(db.executor, db.scope, approval["id"]) == {"receipt": receipt, "replayed": True}
    assert count(db, FabricReceipt) == 1


def test_an_old_approval_stays_readable_and_says_it_is_not_current(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    approve(db, approval)
    upgrade(monkeypatch)
    shown = read(db, approval)
    assert shown["decision"] == "APPROVED" and shown["proposal_hash"] == approval["proposal_hash"]
    assert shown["adapter_current"] is False and shown["history"] == []


def test_same_key_request_replaces_a_stale_pending_proposal_with_an_audited_new_version(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    upgrade(monkeypatch)
    renewed = propose(db, report, "ISSUE")
    assert renewed["id"] == approval["id"] and renewed["proposal"]["adapter_hash"] == NEW_ADAPTER
    assert renewed["proposal_hash"] != approval["proposal_hash"]
    assert renewed["decision"] == "PENDING" and renewed["revision"] == approval["revision"] + 1
    shown = read(db, renewed)
    assert shown["adapter_current"] is True and len(shown["history"]) == 1
    assert shown["history"][0]["proposal_hash"] == approval["proposal_hash"]
    assert shown["history"][0]["reasons"] == ["ADAPTER_CHANGED"] and shown["history"][0]["version"] == 1
    assert kinds(db).count("FABRIC_APPROVAL_SUPERSEDED") == 1


def test_new_key_request_supersedes_once_and_repeats_idempotently(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    upgrade(monkeypatch)
    renewed = propose(db, report, "ISSUE", key="renew-1")
    assert renewed["id"] == approval["id"] and renewed["proposal"]["adapter_hash"] == NEW_ADAPTER
    assert propose(db, report, "ISSUE", key="renew-1") == renewed
    assert len(read(db, renewed)["history"]) == 1 and kinds(db).count("FABRIC_APPROVAL_SUPERSEDED") == 1
    rejects("FABRIC_ACTION_ALREADY_PROPOSED", lambda: propose(db, report, "ISSUE", key="renew-2"))


def test_an_old_decision_cannot_authorize_the_new_version(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    approve(db, approval)
    upgrade(monkeypatch)
    rejects("FABRIC_PROPOSAL_CHANGED", lambda: db.service.execute(db.executor, db.scope, approval["id"]))
    renewed = propose(db, report, "ISSUE", key="renew-1")
    assert renewed["decision"] == "PENDING"
    rejects("APPROVAL_REQUIRED", lambda: db.service.execute(db.executor, db.scope, approval["id"]))
    rejects("PROPOSAL_HASH_MISMATCH", lambda: db.service.decide(
        db.reviewer, db.scope, approval["id"], approval["proposal_hash"], renewed["revision"], "APPROVED"))
    archived = read(db, renewed)["history"][0]
    assert archived["decision"] == "APPROVED" and archived["reviewer"] == db.reviewer.subject_id


def test_the_new_version_is_approved_and_executed_against_current_parameters(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    upgrade(monkeypatch)
    renewed = propose(db, report, "ISSUE", key="renew-1")
    decide_new(db, renewed)
    result = db.service.execute(db.executor, db.scope, approval["id"])
    assert result["replayed"] is False and result["receipt"]["kind"] == "ISSUE"
    assert db.service.execute(db.executor, db.scope, approval["id"])["receipt"] == result["receipt"]
    assert count(db, FabricReceipt) == 1


def test_a_pending_compensation_can_be_renewed_approved_and_executed(db, monkeypatch):
    db = db()
    report = review(db)
    original = completed(db, report, "TEST_STAGE")
    pending = propose(db, report, "STAGE_REVERT", target=original["id"])
    upgrade(monkeypatch)
    rejects("FABRIC_PROPOSAL_CHANGED", lambda: db.service.decide(
        db.reviewer, db.scope, pending["id"], pending["proposal_hash"], 0, "APPROVED"))
    renewed = propose(db, report, "STAGE_REVERT", key="renew-1", target=original["id"])
    assert renewed["id"] == pending["id"] and renewed["proposal"]["adapter_hash"] == NEW_ADAPTER
    decide_new(db, renewed)
    receipt = db.service.execute(db.executor, db.scope, pending["id"])["receipt"]
    assert receipt["effect_json"]["target_receipt_id"] == original["id"]
    assert project(db)["current_stage"] == "PROTOTYPE" and project(db)["stage_revision"] == 2


def test_a_current_pending_proposal_is_not_replaced(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    assert propose(db, report, "ISSUE") == approval
    rejects("FABRIC_ACTION_ALREADY_PROPOSED", lambda: propose(db, report, "ISSUE", key="other"))
    assert read(db, approval)["history"] == []


def test_a_consumed_proposal_is_never_replaced(db, monkeypatch):
    db = db()
    report = review(db)
    completed(db, report, "ISSUE")
    upgrade(monkeypatch)
    rejects("FABRIC_ACTION_ALREADY_PROPOSED", lambda: propose(db, report, "ISSUE", key="renew-1"))
    assert count(db, FabricReceipt) == 1 and "FABRIC_APPROVAL_SUPERSEDED" not in kinds(db)


def test_a_rejection_is_final(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    db.service.decide(db.reviewer, db.scope, approval["id"], approval["proposal_hash"], 0, "REJECTED")
    upgrade(monkeypatch)
    rejects("FABRIC_ACTION_ALREADY_PROPOSED", lambda: propose(db, report, "ISSUE", key="renew-1"))


def test_a_tampered_proposal_cannot_be_superseded(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    with db.source._tx() as session:
        row = db.source._get(session, FabricApproval, db.scope, approval["id"])
        row.proposal = {**row.proposal, "parameters": {"title": "edited"}}
    upgrade(monkeypatch)
    rejects("FABRIC_RECORD_CHANGED", lambda: propose(db, report, "ISSUE"))
    rejects("FABRIC_RECORD_CHANGED", lambda: propose(db, report, "ISSUE", key="renew-1"))
    assert "FABRIC_APPROVAL_SUPERSEDED" not in kinds(db)


def test_an_expired_undecided_proposal_can_be_renewed(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    with db.source._tx() as session:
        row = db.source._get(session, FabricApproval, db.scope, approval["id"])
        row.expires_at = contracts.utcnow() - timedelta(minutes=1)
        seal(row)
    renewed = propose(db, report, "ISSUE", key="renew-1")
    assert renewed["id"] == approval["id"] and renewed["decision"] == "PENDING"
    assert read(db, renewed)["history"][0]["reasons"] == ["EXPIRED"]


def test_a_review_that_no_longer_verifies_is_replaced_by_a_new_review(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    original = fabric_store_module.evaluate_fabric
    monkeypatch.setattr(fabric_store_module, "evaluate_fabric",
                        lambda *args, **kwargs: {**original(*args, **kwargs), "audit_chain_note": "upgraded"})
    rejects("FABRIC_SOURCE_CHANGED", lambda: db.service.get(db.owner, db.scope, report["id"]))
    newer = db.service.create(db.owner, db.scope, db.run_id, "offline-review-2")
    renewed = db.service.propose(db.owner, db.scope, newer["id"], newer["report_hash"], "renew-1", "ISSUE")
    assert renewed["id"] == approval["id"] and renewed["review_id"] == newer["id"]
    assert read(db, renewed)["history"][0]["reasons"] == ["REVIEW_STALE"]


def test_the_superseding_proposer_cannot_also_decide(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    both = Principal(subject_id="second-owner", kind="user", roles=frozenset({"admin", "supervisor", "reviewer"}),
                     scopes=(FABRIC_SCOPE,))
    upgrade(monkeypatch)
    renewed = db.service.propose(both, db.scope, report["id"], report["report_hash"], "renew-1", "ISSUE")
    rejects("INDEPENDENT_REVIEW_REQUIRED", lambda: db.service.decide(
        both, db.scope, approval["id"], renewed["proposal_hash"], renewed["revision"], "APPROVED"))
    rejects("INDEPENDENT_REVIEW_REQUIRED", lambda: db.service.decide(
        Principal(subject_id=db.owner.subject_id, kind="user", roles=frozenset({"reviewer"}), scopes=(FABRIC_SCOPE,)),
        db.scope, approval["id"], renewed["proposal_hash"], renewed["revision"], "APPROVED"))


def test_another_tenant_cannot_read_or_supersede(db, monkeypatch):
    from forgeflow.contracts import Scope

    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    other_scope = Scope(tenant_id="other-tenant", workspace_id="engineering", project_id="aurora-fabric-e2e")
    outsider = Principal(subject_id="outsider", kind="user", roles=frozenset({"admin", "supervisor", "reviewer"}),
                         scopes=(other_scope,))
    upgrade(monkeypatch)
    rejects("NOT_FOUND", lambda: db.service.get_approval(outsider, db.scope, approval["id"]))
    rejects("NOT_FOUND", lambda: db.service.get_approval(outsider, other_scope, approval["id"]))
    rejects("NOT_FOUND", lambda: db.service.propose(outsider, db.scope, report["id"], report["report_hash"],
                                                    "renew-1", "ISSUE"))
    rejects("NOT_FOUND", lambda: db.service.execute(
        Principal(subject_id="x", kind="service", roles=frozenset({"fabric_executor"}), scopes=(other_scope,)),
        other_scope, approval["id"]))
    assert "FABRIC_APPROVAL_SUPERSEDED" not in kinds(db)


def test_no_earlier_proposer_of_any_version_can_approve_the_current_one(db, monkeypatch):
    """A creates v1, B renews it as v2, C renews it as v3: only someone outside A, B and C may approve v3."""
    db = db()
    report = review(db)
    first = propose(db, report, "ISSUE")
    renewer = {name: Principal(subject_id=name, kind="user", roles=frozenset({"admin", "supervisor", "reviewer"}),
                               scopes=(FABRIC_SCOPE,)) for name in ("second-proposer", "third-proposer")}
    monkeypatch.setattr(fabric_store_module, "adapter_hash", lambda: "1" * 64)
    db.service.propose(renewer["second-proposer"], db.scope, report["id"], report["report_hash"], "renew-b", "ISSUE")
    monkeypatch.setattr(fabric_store_module, "adapter_hash", lambda: "2" * 64)
    third = db.service.propose(renewer["third-proposer"], db.scope, report["id"], report["report_hash"],
                               "renew-c", "ISSUE")
    assert third["id"] == first["id"] and len(read(db, third)["history"]) == 2
    for who in (db.owner, renewer["second-proposer"], renewer["third-proposer"]):
        reviewer = Principal(subject_id=who.subject_id, kind="user", roles=frozenset({"reviewer"}),
                             scopes=(FABRIC_SCOPE,))
        rejects("INDEPENDENT_REVIEW_REQUIRED", lambda reviewer=reviewer: db.service.decide(
            reviewer, db.scope, third["id"], third["proposal_hash"], third["revision"], "APPROVED"))
    decided = decide_new(db, third)
    assert decided["approval"]["decision"] == "APPROVED" and decided["approval"]["reviewer"] == db.reviewer.subject_id


def expire_now(db, approval):
    with db.source._tx() as session:
        row = db.source._get(session, FabricApproval, db.scope, approval["id"])
        row.expires_at = contracts.utcnow() - timedelta(minutes=1)
        seal(row)


def test_a_version_that_expires_again_can_be_renewed_again_with_the_same_key(db):
    """Found in the rehearsal: the second renewal with the same key returned the expired version unchanged."""
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    expire_now(db, approval)
    second = propose(db, report, "ISSUE")  # same key as the first proposal
    assert second["id"] == approval["id"] and second["revision"] == 1
    expire_now(db, approval)
    third = propose(db, report, "ISSUE")  # same key once more
    assert third["revision"] == 2 and third["proposal_hash"] != second["proposal_hash"]
    shown = read(db, third)
    assert [item["reasons"] for item in shown["history"]] == [["EXPIRED"], ["EXPIRED"]]
    assert shown["history"][1]["proposal_hash"] == second["proposal_hash"]
    assert explain_ok(db, third)


def explain_ok(db, approval):
    return db.service.explain(db.reviewer, db.scope, approval["id"])["can_be_decided"]["now"] is True


def test_a_current_version_requested_again_with_the_key_that_made_it_is_returned_unchanged(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    expire_now(db, approval)
    renewed = propose(db, report, "ISSUE", key="renew-1")
    assert propose(db, report, "ISSUE", key="renew-1") == renewed
    assert propose(db, report, "ISSUE") == renewed
    assert len(read(db, renewed)["history"]) == 1
