"""Legacy hardware-only approvals are blocked at the Store, not only at one HTTP route.

Every production entry point obtains its store from ``forgeflow.local.load_store``.
These tests drive the real CLI entry (``local.main``) and ``load_store()`` and never
construct the gated store class themselves, so they prove the CLI really got it.
"""

import ast
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeflow import local
from forgeflow.contracts import DomainError, Principal, RunRequest
from forgeflow.store import Action, Approval, Event, Receipt, filters

ROOT = Path(__file__).resolve().parents[1]
SCOPE = local.SCOPE
CODE = "EVT_BOUND_APPROVAL_REQUIRED"
OWNER = Principal(subject_id="local-owner", kind="user", scopes=(SCOPE,),
                  roles=frozenset({"submitter", "admin", "reviewer", "hardware", "pm"}))
WORKER = Principal(subject_id="forgeflow-local-worker", kind="service", scopes=(SCOPE,),
                   roles=frozenset({"worker", "memory_writer", "hardware"}))
# Evaluation harnesses that need APPROVED on a private throwaway SQLite database.
HARNESS_STORE_CONSTRUCTORS = {"scripts/s2b_evaluate.py", "preparation/s2/evaluation/evaluator.py"}
LIVE_DATABASE_MARKERS = ("database_url", "FORGEFLOW_DATABASE_URL", ".runtime", "load_store", "dev.env", "postgres")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_DATABASE_URL", f"sqlite:///{tmp_path / 'legacy-gate.sqlite'}")
    monkeypatch.setattr(local, "load_identities", lambda: {"synthetic-test-token": OWNER})
    return local.load_store()


def checked_run(store):
    run = store.submit(OWNER, RunRequest(scope=SCOPE, fixture_id="aurora-pass"), "legacy-gate")
    claim = store.claim(WORKER, SCOPE, run["id"], "worker-a")
    snapshot = {"policy_version": "1", "skill_version": "1", "rule_version": "1", "classification": 0,
                "input_hash": "a" * 64, "documents": []}
    result = {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": [], "stdout": "synthetic"}
    return store.complete_check(WORKER, SCOPE, run["id"], claim["token"], claim["epoch"], result,
                                {"status": "PASS", "evidence": []}, snapshot)


def row(session, model, identifier):
    return session.scalar(select(model).where(*filters(model, SCOPE), model.id == identifier))


def cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["forgeflow.local", *argv])
    local.main()
    return json.loads(capsys.readouterr().out)


def decide_args(store, run, decision):
    approval = run["approvals"][0]
    revision = store.get_run(OWNER, SCOPE, run["id"])["revision"]
    return ["decide", approval["id"], approval["proposal_hash"], str(revision), "--decision", decision]


def test_cli_approved_decision_is_blocked_and_leaves_everything_pending(store, monkeypatch, capsys):
    run = checked_run(store)
    with pytest.raises(DomainError) as raised:
        cli(monkeypatch, capsys, *decide_args(store, run, "APPROVED"))
    assert (raised.value.code, raised.value.status) == (CODE, 409)
    state = store.get_run(OWNER, SCOPE, run["id"])
    assert {row["decision"] for row in state["approvals"]} == {"PENDING"}
    assert {row["status"] for row in state["actions"]} == {"WAITING_APPROVAL"}
    assert state["revision"] == run["revision"]


def test_cli_rejection_remains_available(store, monkeypatch, capsys):
    run = checked_run(store)
    output = cli(monkeypatch, capsys, *decide_args(store, run, "REJECTED"))
    assert output["approval"]["decision"] == "REJECTED"


def test_blocked_attempt_writes_one_audit_event_and_keeps_the_chain_valid(store, monkeypatch, capsys):
    run = checked_run(store)
    with pytest.raises(DomainError):
        cli(monkeypatch, capsys, *decide_args(store, run, "APPROVED"))
    events = [row for row in store.events(OWNER, SCOPE, run["id"]) if row["kind"] == "LEGACY_APPROVAL_BLOCKED"]
    assert len(events) == 1
    assert events[0]["actor"] == OWNER.subject_id
    assert events[0]["payload"]["approval_id"] == run["approvals"][0]["id"]
    assert events[0]["payload"]["decision"] == "APPROVED"
    assert store.verify_events(OWNER, SCOPE, run["id"]) is True


def test_row_approved_before_the_gate_cannot_be_dispatched(store):
    run = checked_run(store)
    action = next(row for row in run["actions"] if row["tool"] == "issues.create.local")
    with Session(store.engine) as session, session.begin():
        approval = row(session, Approval, action["approval_id"])
        approval.decision, approval.reviewer = "APPROVED", "historical-reviewer"
        row(session, Action, action["id"]).status = "APPROVED"
    claim = store.claim(WORKER, SCOPE, run["id"], "local-dispatch")
    with pytest.raises(DomainError) as raised:
        store.dispatch_local(WORKER, SCOPE, run["id"], action["id"], "local-dispatch",
                             lease_token=claim["token"], epoch=claim["epoch"])
    assert raised.value.code == CODE
    with Session(store.engine) as session:
        assert session.scalars(select(Receipt).where(*filters(Receipt, SCOPE))).all() == []
        assert row(session, Approval, action["approval_id"]).consumed_by_action is None
        assert row(session, Action, action["id"]).status == "APPROVED"
        kinds = [row.kind for row in session.scalars(select(Event).where(*filters(Event, SCOPE)))]
    assert "LEGACY_APPROVAL_BLOCKED" in kinds


def test_gate_condition_lives_in_one_function():
    from forgeflow import evt_store

    assert evt_store.legacy_approval_gate("REJECTED") is None
    with pytest.raises(DomainError) as raised:
        evt_store.legacy_approval_gate("APPROVED")
    assert (raised.value.code, raised.value.status) == (CODE, 409)


def http_client(store):
    from fastapi.testclient import TestClient

    from forgeflow.evt_api import create_evt_app

    app = create_evt_app(store, {"synthetic-test-token": OWNER}, None, test_harness=True)
    return TestClient(app), {"Authorization": "Bearer synthetic-test-token"}


def decision_body(store, run, decision):
    approval = run["approvals"][0]
    return {"scope": SCOPE.model_dump(mode="json"), "proposal_hash": approval["proposal_hash"],
            "expected_run_revision": store.get_run(OWNER, SCOPE, run["id"])["revision"], "decision": decision}


def blocked_events(store, run_id):
    return [row for row in store.events(OWNER, SCOPE, run_id) if row["kind"] == "LEGACY_APPROVAL_BLOCKED"]


def test_http_approved_decision_returns_409_and_is_audited_by_the_store(store):
    run = checked_run(store)
    client, headers = http_client(store)
    approval_id = run["approvals"][0]["id"]
    response = client.post(f"/approvals/{approval_id}/decision", json=decision_body(store, run, "APPROVED"),
                           headers=headers)
    assert response.status_code == 409 and response.json()["code"] == CODE
    events = blocked_events(store, run["id"])
    assert len(events) == 1 and events[0]["payload"]["approval_id"] == approval_id
    assert events[0]["payload"]["entry"] == "decide"
    assert store.verify_events(OWNER, SCOPE, run["id"]) is True
    assert {row["decision"] for row in store.get_run(OWNER, SCOPE, run["id"])["approvals"]} == {"PENDING"}


def test_http_rejection_still_works_and_writes_no_block_event(store):
    run = checked_run(store)
    client, headers = http_client(store)
    response = client.post(f"/approvals/{run['approvals'][0]['id']}/decision",
                           json=decision_body(store, run, "REJECTED"), headers=headers)
    assert response.status_code == 200 and response.json()["approval"]["decision"] == "REJECTED"
    assert blocked_events(store, run["id"]) == []


def test_http_approved_decision_for_an_unknown_approval_is_still_409_without_an_event(store):
    run = checked_run(store)
    client, headers = http_client(store)
    body = {**decision_body(store, run, "APPROVED"), "proposal_hash": "0" * 64}
    assert client.post("/approvals/historical-id/decision", json=body, headers=headers).status_code == 409
    assert blocked_events(store, run["id"]) == []


def test_evt_app_refuses_to_start_on_an_ungated_store(tmp_path):
    from forgeflow.evt_api import create_evt_app
    from forgeflow.store import Store

    ungated = Store(f"sqlite:///{tmp_path / 'ungated.sqlite'}")
    with pytest.raises(RuntimeError, match="EVT_BOUND_STORE_REQUIRED"):
        create_evt_app(ungated, {}, None, test_harness=True)


def test_load_store_returns_the_gated_implementation(store):
    from forgeflow.evt_store import EVTBoundStore
    from forgeflow.fabric_runtime import FabricStore

    assert isinstance(store, EVTBoundStore)
    assert issubclass(FabricStore, EVTBoundStore)


def _store_uses(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))

    def named(node):
        return getattr(node, "id", None) == "Store" or getattr(node, "attr", None) == "Store"

    calls = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and named(node.func)]
    subclasses = [(node.name, node.lineno) for node in ast.walk(tree)
                  if isinstance(node, ast.ClassDef) and any(named(base) for base in node.bases)]
    return calls, subclasses


def test_no_package_module_builds_or_subclasses_the_ungated_base_store():
    offenders = []
    for path in sorted((ROOT / "src/forgeflow").glob("*.py")):
        calls, subclasses = _store_uses(path)
        if path.name == "evt_store.py":
            continue  # the single module that is allowed to extend the base class
        offenders += [f"{path.relative_to(ROOT)}:{line} Store(...)" for line in calls]
        offenders += [f"{path.relative_to(ROOT)}:{line} class {name}(Store)" for name, line in subclasses]
    assert offenders == []


def test_only_the_two_known_evaluation_harnesses_build_the_base_store_outside_the_package():
    found = set()
    for folder in ("scripts", "preparation"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if "__pycache__" not in path.parts and _store_uses(path)[0]:
                found.add(str(path.relative_to(ROOT)))
    assert found == HARNESS_STORE_CONSTRUCTORS


def test_whitelisted_harnesses_never_reach_the_live_database_or_runtime_credentials():
    for name in HARNESS_STORE_CONSTRUCTORS:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert [marker for marker in LIVE_DATABASE_MARKERS if marker in text] == [], name
