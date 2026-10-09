# ruff: noqa: F811  (the db fixture is imported from test_fabric_store and used as an argument)
"""The manual approval view: what is being approved, which version of the parameters, and what would happen.

`explain` only reads. It exists so that a person decides on the facts, not on a hash.
"""

import json
import sys

from sqlalchemy import func, select
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

from forgeflow import fabric_cli
from forgeflow import fabric_store as fabric_store_module
from forgeflow.contracts import Principal, Scope
from forgeflow.fabric_store import FabricReceipt
from forgeflow.store import Event, filters


def explain(db, approval, who=None):
    return db.service.explain(who or db.reviewer, db.scope, approval["id"])


def events(db):
    with db.source._tx() as session:
        return session.scalar(select(func.count()).select_from(Event).where(*filters(Event, db.scope)))


def test_a_pending_stage_change_shows_what_it_would_do_before_anyone_decides(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "TEST_STAGE")
    before = (events(db), count(db, FabricReceipt), project(db)["stage_revision"])
    shown = explain(db, approval)
    assert (events(db), count(db, FabricReceipt), project(db)["stage_revision"]) == before  # read only
    assert shown["approval"]["decision"] == "PENDING" and shown["approval"]["kind"] == "TEST_STAGE"
    assert shown["parameters"]["version"] == 1 and shown["parameters"]["proposal_hash"] == approval["proposal_hash"]
    assert shown["parameters"]["values"] == approval["proposal"]["parameters"]
    effect = shown["if_executed"]
    assert effect["stage"] == {"from": "PROTOTYPE", "to": "TEST_STAGE", "stage_revision": [0, 1]}
    assert effect["external_writes"] is False and effect["simulation"] is True
    assert shown["after_decision"] == "RECORDS_THE_DECISION_ONLY_NOTHING_EXECUTES_UNTIL_THE_EXECUTOR_RUNS"


def test_an_issue_shows_the_local_record_it_would_create(db):
    db = db()
    report = review(db)
    shown = explain(db, propose(db, report, "ISSUE"))
    assert shown["if_executed"]["creates"] == {
        "kind": "ISSUE", "status": "OPEN", "title": "Synthetic Aurora local workflow follow-up",
        "remote_tracker": False}


def test_a_memory_activation_says_it_stays_a_candidate_and_confirms_nothing(db):
    db = db()
    report = review(db)
    completed(db, report, "TEST_STAGE")
    completed(db, report, "ISSUE")
    shown = explain(db, propose(db, report, "MEMORY_ACTIVATE"))
    creates = shown["if_executed"]["creates"]
    assert creates["kind"] == "MEMORY" and creates["status"] == "ACTIVE" and creates["underlying_status"] == "CANDIDATE"
    assert creates["confirms_evt"] is False and len(creates["source_event_ids"]) == 3


def test_a_compensation_shows_the_receipt_it_reverses(db):
    db = db()
    report = review(db)
    original = completed(db, report, "TEST_STAGE")
    shown = explain(db, propose(db, report, "STAGE_REVERT", target=original["id"]))
    assert shown["if_executed"]["reverses_receipt"] == original["id"]
    assert shown["if_executed"]["stage"] == {"from": "TEST_STAGE", "to": "PROTOTYPE", "stage_revision": [1, 2]}


def test_it_names_everyone_who_may_not_decide_and_the_gate_it_rests_on(db):
    db = db()
    report = review(db)
    shown = explain(db, propose(db, report, "ISSUE"))
    assert shown["independence"]["may_not_decide"] == [db.owner.subject_id]
    assert shown["gate"] == {"flow_gate": "PASS", "evt_gate": "UNKNOWN", "full_evt_eligibility": False}
    assert shown["provenance"] == "TEST_DRIVER"


def test_a_stale_approval_says_it_cannot_be_decided_and_how_to_renew_it(db, monkeypatch):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    monkeypatch.setattr(fabric_store_module, "adapter_hash", lambda: "e" * 64)
    shown = explain(db, approval)
    assert shown["approval"]["adapter_current"] is False
    assert shown["can_be_decided"] == {"now": False, "reasons": ["ADAPTER_CHANGED"], "renew_with": "propose"}


def test_a_current_approval_can_be_decided_and_an_executed_one_shows_its_receipt(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    assert explain(db, approval)["can_be_decided"] == {"now": True, "reasons": [], "renew_with": None}
    approve(db, approval)
    receipt = execute(db, approval)
    shown = explain(db, approval)
    assert shown["approval"]["decision"] == "APPROVED" and shown["executed"] == {"receipt_id": receipt["id"]}
    assert shown["can_be_decided"]["now"] is False


def test_another_tenant_cannot_look(db):
    db = db()
    approval = propose(db, review(db), "ISSUE")
    other = Scope(tenant_id="other-tenant", workspace_id="engineering", project_id="aurora-fabric-e2e")
    outsider = Principal(subject_id="outsider", kind="user", roles=frozenset({"admin", "reviewer"}), scopes=(other,))
    rejects("NOT_FOUND", lambda: db.service.explain(outsider, db.scope, approval["id"]))
    rejects("NOT_FOUND", lambda: db.service.explain(outsider, other, approval["id"]))


def test_the_cli_prints_the_same_view(db, monkeypatch, capsys):
    db = db()
    approval = propose(db, review(db), "TEST_STAGE")
    identities = {"owner": db.owner, "reviewer": db.reviewer, "executor": db.executor}
    monkeypatch.setattr(fabric_cli, "load_fabric_store", lambda: db.source)
    monkeypatch.setattr(fabric_cli, "load_fabric_identities", lambda: identities)
    monkeypatch.setattr(sys, "argv", ["fabric_cli", "explain", approval["id"]])
    fabric_cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["approval_provenance"] == "TEST_DRIVER"
    assert printed["output"] == json.loads(json.dumps(explain(db, approval), default=str))
