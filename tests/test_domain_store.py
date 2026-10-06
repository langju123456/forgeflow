"""Supplemental review persistence and unified API over recorded receipts."""

import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_sre_ops import db as recorded_db  # noqa: F401

from forgeflow.contracts import DomainError, digest
from forgeflow.domain_store import DomainOperations, DomainReview
from forgeflow.prototype_api import create_prototype_app
from forgeflow.store import Run, values


@pytest.fixture
def domain(recorded_db):  # noqa: F811 - pytest injects the imported fixture.
    source, _, owner, scope, run_id = recorded_db
    service = DomainOperations(source)
    service.create_schema()
    return source, service, owner, scope, run_id


def rejects(code, operation):
    with pytest.raises(DomainError) as caught:
        operation()
    assert caught.value.code == code


def test_review_persists_replays_and_does_not_change_parent(domain):
    source, service, owner, scope, run_id = domain
    before, events = source.get_run(owner, scope, run_id), source.events(owner, scope, run_id)
    first = service.review(owner, scope, run_id, "domain-one")
    report = first["report_json"]
    assert report["architect"]["readiness"] == "READY"
    assert report["android"]["static_status"] == "PASS" and report["android"]["readiness"] == "UNKNOWN"
    assert report["technical_gate"] == "UNKNOWN"
    assert report["supervisor_handoff"] == "NEEDS_ANDROID_BUILD_EVIDENCE"
    fresh = DomainOperations(source)
    assert fresh.review(owner, scope, run_id, "domain-one") == fresh.get(owner, scope, first["id"]) == first
    assert source.get_run(owner, scope, run_id) == before and source.events(owner, scope, run_id) == events


def test_fixture_selector_is_part_of_idempotency(domain):
    _, service, owner, scope, run_id = domain
    service.review(owner, scope, run_id, "fixture-key")
    rejects("IDEMPOTENCY_CONFLICT", lambda: service.review(owner, scope, run_id, "fixture-key", "bom-mismatch"))
    bad = service.review(owner, scope, run_id, "different-key", "bom-mismatch")
    assert bad["report_json"]["supervisor_handoff"] == "DOMAIN_CONFLICT"


def test_scopes_roles_and_kind_are_reauthorized(domain):
    _, service, owner, scope, run_id = domain
    first = service.review(owner, scope, run_id, "auth")
    foreign = scope.model_copy(update={"tenant_id": "foreign"})
    rejects("NOT_FOUND", lambda: service.get(owner, foreign, first["id"]))
    observer = owner.model_copy(update={"roles": frozenset({"submitter"})})
    rejects("NOT_FOUND", lambda: service.review(observer, scope, run_id, "denied"))
    agent = owner.model_copy(update={"kind": "agent"})
    rejects("HUMAN_SOP_REQUEST_REQUIRED", lambda: service.review(agent, scope, run_id, "agent"))


def test_revised_source_cannot_replay_old_report(domain):
    source, service, owner, scope, run_id = domain
    first = service.review(owner, scope, run_id, "stale")
    with source._tx() as session:
        source._get(session, Run, scope, run_id).revision += 1
    rejects("DOMAIN_SOURCE_CHANGED", lambda: service.get(owner, scope, first["id"]))
    rejects("IDEMPOTENCY_CONFLICT", lambda: service.review(owner, scope, run_id, "stale"))


def test_hash_rewrite_cannot_invent_build_success(domain):
    source, service, owner, scope, run_id = domain
    first = service.review(owner, scope, run_id, "tamper")
    with source._tx() as session:
        row = session.scalar(select(DomainReview).where(DomainReview.id == first["id"]))
        report = copy.deepcopy(row.report_json)
        report["android"]["readiness"] = "READY"
        row.report_json = report
        row.payload_hash = digest({"scope": values(scope), "report": report, "audit": row.audit_json})
    rejects("DOMAIN_REPORT_CHANGED", lambda: service.get(owner, scope, first["id"]))


def test_unified_api_keeps_existing_routes_and_checks_request(domain):
    source, _, owner, scope, run_id = domain
    client = TestClient(create_prototype_app(source, {"offline-test-token": owner}, None, test_harness=True))
    routes = client.get("/openapi.json").json()["paths"]
    assert all(path in routes for path in ["/runs", "/s2c/parents", "/runs/{run_id}/grounded-analysis",
                                         "/runs/{run_id}/sre-review", "/runs/{run_id}/domain-review"])
    body = {"scope": values(scope), "fixture_id": "aligned"}
    path = f"/runs/{run_id}/domain-review"
    headers = {"Authorization": "Bearer offline-test-token", "Idempotency-Key": "http"}
    assert client.post(path, json=body).status_code == 401
    assert client.post(path, json={**body, "role": "android"}, headers=headers).status_code == 422
    first = client.post(path, json=body, headers=headers)
    assert first.status_code == 200
    assert client.post(path, json=body, headers=headers).json() == first.json()
    assert client.get(f"/domain-reviews/{first.json()['id']}", params=values(scope), headers=headers).json() == first.json()
