# ruff: noqa: F811  (the db fixture is imported from test_fabric_store and used as an argument)
"""The representative chain driver: order, refusal, and a guard that keeps it off the live database.

The real Docker check, MCP handshake and model are passed in as callbacks; these tests use the offline Fabric
fixtures, so they show the orchestration invariants without running any of them.
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select
from test_fabric_store import db  # noqa: F401

from forgeflow import chain
from forgeflow.contracts import DomainError
from forgeflow.fabric_store import FabricApproval, FabricReceipt
from forgeflow.store import Event, filters


def count(db, model):
    with db.source._tx() as session:
        return session.scalar(select(func.count()).select_from(model).where(*filters(model, db.scope)))


@pytest.mark.parametrize("url", ["postgresql+psycopg://u:p@127.0.0.1:55436/forgeflow_chain_rehearsal",
                                 "sqlite:////tmp/forgeflow_chain_x.sqlite"])
def test_the_driver_accepts_only_a_database_named_as_a_chain_copy(url):
    chain.require_copy_database(url)


@pytest.mark.parametrize("url", ["postgresql+psycopg://u:p@127.0.0.1:55436/forgeflow",
                                 "postgresql+psycopg://u:p@127.0.0.1:55436/postgres",
                                 "postgresql+psycopg://u:p@127.0.0.1:55436/", "sqlite:////tmp/live.sqlite",
                                 "sqlite://"])
def test_the_driver_refuses_the_live_database_and_anything_unnamed(url):
    with pytest.raises(DomainError) as refused:
        chain.require_copy_database(url)
    assert refused.value.code == "CHAIN_NEEDS_A_DATABASE_COPY"
    assert "p@" not in refused.value.message and "u:p" not in refused.value.message  # no connection string leaks


def start(db, **options):
    return asyncio.run(chain.start_chain(db.source, db.service, db.owner, key="k1", run_id=db.run_id, **options))


def test_a_passing_run_ends_with_review_and_two_pending_proposals_and_nothing_executed(db):
    db = db()
    state = start(db)
    assert [step["step"] for step in state["steps"]] == ["review", "propose:TEST_STAGE", "propose:ISSUE"]
    assert state["flow_gate"] == "PASS" and state["evt_gate"] == "UNKNOWN"
    assert set(state["proposals"]) == {"TEST_STAGE", "ISSUE"} and state["refused"] == {}
    with db.source._tx() as session:
        decisions = {row.kind: row.decision for row in session.scalars(select(FabricApproval).where(
            *filters(FabricApproval, db.scope)))}
    assert decisions == {"TEST_STAGE": "PENDING", "ISSUE": "PENDING"} and count(db, FabricReceipt) == 0
    json.dumps(state)  # the state is plain data


def test_the_same_key_gives_the_same_review_and_proposals(db):
    db = db()
    first, second = start(db), start(db)
    assert first["review_id"] == second["review_id"] and first["proposals"] == second["proposals"]
    assert count(db, FabricApproval) == 2


def test_an_unknown_run_is_refused_for_every_action_and_creates_no_approval(db):
    db = db(hardware_status="UNKNOWN")
    state = start(db)
    assert state["flow_gate"] == "UNKNOWN" and state["proposals"] == {}
    assert state["refused"] == {"TEST_STAGE": "FABRIC_GATE_NOT_PASS", "ISSUE": "FABRIC_GATE_NOT_PASS"}
    assert count(db, FabricApproval) == 0


def test_the_analysis_runs_after_the_check_and_before_the_review_when_asked(db):
    db = db()
    seen = []

    async def analyze(owner, scope, run_id, key):
        seen.append(("analyze", key))
        return {"state": "COMPLETE", "calls": [{"phase": "PLAN"}] * 4, "id": "analysis-1"}

    state = start(db, analyze=analyze)
    assert [step["step"] for step in state["steps"]][:2] == ["analysis", "review"]
    assert state["steps"][0]["state"] == "COMPLETE" and state["steps"][0]["model_calls"] == 4 and seen


def test_the_verification_report_checks_the_audit_chain_and_counts_what_happened(db):
    db = db()
    state = start(db)
    report = chain.verify_chain(db.source, db.service, db.owner, state)
    assert report["audit_chain_valid"] is True and report["receipts"] == 0 and report["approvals"] == 2
    assert report["event_kinds"].count("FABRIC_ACTION_PROPOSED") == 2 and report["review_matches"] is True
    with db.source._tx() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(*filters(Event, db.scope))) > 0
