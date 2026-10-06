"""Offline actual-receipt fixtures and SQLite persistence; no live execution."""

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, select

from forgeflow.contracts import DomainError, Principal, Scope, digest
from forgeflow.sre_api import create_sre_app
from forgeflow.sre_ops import SREOperations, SREReview, evaluate_operations
from forgeflow.store import Artifact, Event, Run, Step, Store, aware, values

ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01/raw-chains"


def archive(scenario="M03"):
    data = json.loads((ARCHIVES / (scenario + ".json")).read_text())
    run = data["parent_after"]
    return Scope(**{key: run[key] for key in ("tenant_id", "workspace_id", "project_id")}), run, data["parent_events"]


def rebind(scope, run, events):
    for step in run["steps"]:
        step["output_hash"] = digest(run["result_json"])
    previous = "0" * 64
    for sequence, event in enumerate(events, 1):
        if event["kind"] == "CHECK_COMPLETED":
            event["payload"]["result_hash"] = digest(run["result_json"])
        event["sequence"], event["prev_hash"] = sequence, previous
        event["payload_hash"] = digest(event["payload"])
        event["event_hash"] = digest({"scope": values(scope), "run_id": run["id"], **{key: event[key] for key in (
            "step_id", "sequence", "kind", "actor", "payload_hash", "prev_hash", "classification", "created_at")}})
        previous = event["event_hash"]


@pytest.mark.parametrize("scenario,gate,handoff", [
    ("M01", "PASS", "READY_FOR_HUMAN_REVIEW"), ("M01-blocked", "BLOCKED", "NEEDS_TECHNICAL_EVIDENCE"),
    ("M02", "UNKNOWN", "NEEDS_TECHNICAL_EVIDENCE"), ("M03", "UNKNOWN", "NEEDS_TECHNICAL_EVIDENCE"),
    ("M04", "PASS", "READY_FOR_HUMAN_REVIEW"),
])
def test_actual_receipts_separate_operations_gate_and_approval(scenario, gate, handoff):
    scope, run, events = archive(scenario)
    before = digest(run)
    report = evaluate_operations(scope, run, events, "offline-skill-hash")
    assert report.operational_readiness == "READY"
    assert report.technical_gate == gate and report.supervisor_handoff == handoff
    assert report.human_approval_state == "PENDING" and report.business_receipt_count == 0
    assert all(row.status == "PASS" for row in report.checks)
    assert run["result_json"]["inspect"]["state"]["Running"] is True  # Export snapshot, not live state.
    assert digest(run) == before


@pytest.mark.parametrize("failure", ["cleanup", "active_lease", "epoch", "profile_type", "artifact", "audit"])
def test_observed_adverse_receipts_require_review(failure):
    scope, run, events = archive()
    if failure == "cleanup":
        run["result_json"]["cleanup"] = "FAILED"
    elif failure == "active_lease":
        run["steps"][0]["lease_expires_at"] = "2026-11-05T00:00:00Z"
    elif failure == "epoch":
        run["artifacts"][0]["provenance"]["epoch"] += 1
    elif failure == "profile_type":
        run["result_json"]["inspect"]["readonly_rootfs"] = 1
    elif failure == "artifact":
        run["artifacts"][0]["content"] += "tampered"
    elif failure == "audit":
        events[0]["payload"]["request_hash"] = "changed"
    if failure != "audit":
        rebind(scope, run, events)
    report = evaluate_operations(scope, run, events, "offline-skill-hash")
    assert report.operational_readiness == "NEEDS_REVIEW"
    assert report.supervisor_handoff == "NEEDS_OPERATIONAL_REVIEW"
    assert report.business_actions_executed_by_sop is False


def test_test_driver_protocol_is_unknown_not_actual_mcp():
    scope, run, events = archive()
    run["result_json"]["mcp"] = {"status": "TEST_DRIVER"}
    rebind(scope, run, events)
    report = evaluate_operations(scope, run, events, "offline-skill-hash")
    assert report.operational_readiness == "UNKNOWN"
    assert report.checks[-1].status == "UNKNOWN"


@pytest.fixture
def db(tmp_path):
    scope, run, events = archive()
    source = Store("sqlite:///" + str(tmp_path / "sre.db"))
    source.create_schema()
    owner = Principal(subject_id="sre-test-owner", kind="user", roles=frozenset({"admin"}), scopes=(scope,))
    source.ensure_project(scope)
    for ref in run["snapshot_json"]["documents"]:
        source.put_document(owner, scope, ref["id"], ref["version"], "Offline receipt review source.",
                            roles={"admin"}, classification=0)
    with source._tx() as session:
        for model, items in ((Run, [run]), (Step, run["steps"]), (Artifact, run["artifacts"]), (Event, events)):
            for item in items:
                data = {column.name: item[column.name] for column in model.__table__.columns if column.name in item}
                for column in model.__table__.columns:
                    if isinstance(column.type, DateTime) and data.get(column.name):
                        data[column.name] = aware(data[column.name])
                session.add(model(**data))
            session.flush()
    service = SREOperations(source)
    service.create_schema()
    return source, service, owner, scope, run["id"]


def rejects(code, operation):
    with pytest.raises(DomainError) as caught:
        operation()
    assert caught.value.code == code


def test_persistent_review_replay_and_parent_unchanged(db):
    source, service, owner, scope, run_id = db
    before, events = source.get_run(owner, scope, run_id), source.events(owner, scope, run_id)
    first = service.review(owner, scope, run_id, "sre-one")
    assert first["report_json"]["operational_readiness"] == "READY"
    fresh = SREOperations(source)
    assert fresh.review(owner, scope, run_id, "sre-one") == first
    assert fresh.get(owner, scope, first["id"]) == first
    assert source.get_run(owner, scope, run_id) == before and source.events(owner, scope, run_id) == events


def test_scope_roles_and_source_reauthorization(db):
    source, service, owner, scope, run_id = db
    result = service.review(owner, scope, run_id, "auth")
    foreign = Scope(tenant_id="foreign", workspace_id=scope.workspace_id, project_id=scope.project_id)
    rejects("NOT_FOUND", lambda: service.get(owner, foreign, result["id"]))
    observer = owner.model_copy(update={"roles": frozenset({"submitter"})})
    rejects("NOT_FOUND", lambda: service.review(observer, scope, run_id, "denied"))
    agent = owner.model_copy(update={"kind": "agent"})
    rejects("HUMAN_SOP_REQUEST_REQUIRED", lambda: service.review(agent, scope, run_id, "agent"))
    with source._tx() as session:
        source._get(session, Run, scope, run_id).revision += 1
    rejects("SRE_SOURCE_CHANGED", lambda: service.get(owner, scope, result["id"]))
    rejects("IDEMPOTENCY_CONFLICT", lambda: service.review(owner, scope, run_id, "auth"))


def test_recomputed_report_hash_cannot_publish_invented_gate(db):
    source, service, owner, scope, run_id = db
    result = service.review(owner, scope, run_id, "tamper")
    with source._tx() as session:
        row = session.scalar(select(SREReview).where(SREReview.id == result["id"]))
        invented = copy.deepcopy(row.report_json)
        invented["technical_gate"] = "PASS"
        row.report_json = invented
        row.payload_hash = digest({"scope": values(scope), "report": invented, "audit": row.audit_json})
    rejects("SRE_REPORT_CHANGED", lambda: service.get(owner, scope, result["id"]))


def test_api_scope_body_no_identity_grant(db):
    source, _, owner, scope, run_id = db
    client = TestClient(create_sre_app(source, {"offline-test-token": owner}))
    body, path = scope.model_dump(mode="json"), f"/runs/{run_id}/sre-review"
    headers = {"Authorization": "Bearer offline-test-token", "Idempotency-Key": "http"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json={**body, "role": "admin"}, headers=headers).status_code == 422
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert client.post(path, json=body, headers=headers).json()["id"] == response.json()["id"]
    assert client.get(f"/sre-reviews/{response.json()['id']}", params=body, headers=headers).status_code == 200
