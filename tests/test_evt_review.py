"""Offline actual M01 receipts; human acceptance never creates a stage action."""

import copy
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime
from test_sre_ops import archive

from forgeflow.contracts import DomainError, Principal, digest
from forgeflow.domain_store import DomainOperations
from forgeflow.evt_api import create_evt_app
from forgeflow.evt_contract import DEFAULT_POLICY, SOURCE_POLICY, UNRESOLVED_POLICY, EVTPolicies
from forgeflow.evt_review import EVTApproval, EVTReview, EVTReviews, approval_hash, technical_source
from forgeflow.evt_store import EVTBoundStore
from forgeflow.sre_ops import SREOperations
from forgeflow.store import Artifact, Document, Event, Run, Step, aware, values

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def evt_db(tmp_path):
    scope, run, events = archive("M01")
    source = EVTBoundStore("sqlite:///" + str(tmp_path / "evt.db"))
    source.create_schema()
    owner = Principal(subject_id="evt-offline-owner", kind="user", roles=frozenset({"admin", "reviewer"}), scopes=(scope,))
    source.ensure_project(scope)
    for ref in run["snapshot_json"]["documents"]:
        source.put_document(owner, scope, ref["id"], ref["version"], "Offline EVT receipt source.",
                            roles={"admin", "reviewer"}, classification=0)
    with source._tx() as session:
        for model, items in ((Run, [run]), (Step, run["steps"]), (Artifact, run["artifacts"]), (Event, events)):
            for item in items:
                data = {column.name: item[column.name] for column in model.__table__.columns if column.name in item}
                for column in model.__table__.columns:
                    if isinstance(column.type, DateTime) and data.get(column.name):
                        data[column.name] = aware(data[column.name])
                session.add(model(**data))
            session.flush()
    domain, sre = DomainOperations(source), SREOperations(source)
    domain.create_schema()
    sre.create_schema()
    dreview = domain.review(owner, scope, run["id"], "offline-domain")
    sreview = sre.review(owner, scope, run["id"], "offline-sre")
    service = EVTReviews(source)
    service.create_schema()
    return source, service, owner, scope, run["id"], dreview["id"], sreview["id"]


def create(data, policy=DEFAULT_POLICY, key="evt-one"):
    _, service, owner, scope, run_id, domain_id, sre_id = data
    return service.create(owner, scope, run_id, key, domain_id, sre_id, policy)


def rejects(code, operation):
    with pytest.raises(DomainError) as error:
        operation()
    assert error.value.code == code


def test_default_gate_unknown_denies_approval_and_replays(evt_db):
    source, service, owner, scope, run_id, domain_id, sre_id = evt_db
    before = source.get_run(owner, scope, run_id)
    review = create(evt_db)
    assert review["report_json"]["status"] == "UNKNOWN"
    assert service.create(owner, scope, run_id, "evt-one", domain_id, sre_id) == review
    assert EVTReviews(source).get(owner, scope, review["id"]) == review
    rejects("EVT_GATE_NOT_PASS", lambda: service.request_approval(owner, scope, review["id"], review["report_hash"], "no"))
    assert source.get_run(owner, scope, run_id) == before


def test_source_policy_acceptance_is_bound_and_cannot_change_stage(evt_db):
    source, service, owner, scope, run_id, _, _ = evt_db
    before, events = source.get_run(owner, scope, run_id), source.events(owner, scope, run_id)
    review = create(evt_db, SOURCE_POLICY)
    assert review["report_json"]["status"] == "PASS"
    approval = service.request_approval(owner, scope, review["id"], review["report_hash"], "acceptance")
    assert service.request_approval(owner, scope, review["id"], review["report_hash"], "acceptance") == approval
    assert approval["proposal"]["stage_transition_allowed"] is False
    rejects("PROPOSAL_HASH_MISMATCH", lambda: service.decide(owner, scope, approval["id"], "0" * 64, 0, "APPROVED"))
    rejects("REVISION_CONFLICT", lambda: service.decide(owner, scope, approval["id"], approval["proposal_hash"], 1, "APPROVED"))
    decision = service.decide(owner, scope, approval["id"], approval["proposal_hash"], 0, "APPROVED")
    assert decision["business_action"] == "NONE" and decision["approval"]["revision"] == 1
    assert service.decide(owner, scope, approval["id"], approval["proposal_hash"], 0, "APPROVED")["replayed"]
    assert EVTReviews(source).get(owner, scope, review["id"]) == review
    assert EVTReviews(source).get_approval(owner, scope, approval["id"]) == decision["approval"]
    assert before == source.get_run(owner, scope, run_id) and events == source.events(owner, scope, run_id)


def test_policy_and_reference_changes_conflict_with_key(evt_db):
    _, service, owner, scope, run_id, domain_id, sre_id = evt_db
    create(evt_db)
    rejects("IDEMPOTENCY_CONFLICT", lambda: service.create(owner, scope, run_id, "evt-one", domain_id, sre_id, SOURCE_POLICY))
    other = create(evt_db, UNRESOLVED_POLICY, "unresolved")
    assert other["report_json"]["status"] == "UNKNOWN"
    rejects("EVT_REVIEW_HASH_MISMATCH", lambda: service.request_approval(owner, scope, other["id"], "0" * 64, "fake"))


def test_cross_scope_wrong_run_and_actor_denied(evt_db):
    _, service, owner, scope, run_id, domain_id, sre_id = evt_db
    review = create(evt_db)
    rejects("NOT_FOUND", lambda: service.get(owner, scope.model_copy(update={"tenant_id": "foreign"}), review["id"]))
    rejects("NOT_FOUND", lambda: service.create(owner, scope, "wrong-run", "wrong", domain_id, sre_id))
    rejects("HUMAN_SOP_REQUEST_REQUIRED", lambda: service.create(owner.model_copy(update={"kind": "agent"}),
            scope, run_id, "agent", domain_id, sre_id))


def test_changed_policy_invalidates_old_report(evt_db, tmp_path):
    source, _, owner, scope, run_id, domain_id, sre_id = evt_db
    root = tmp_path / "policies"
    shutil.copytree(ROOT / "fixtures/aurora-evt-review", root)
    service = EVTReviews(source, policies=EVTPolicies(root))
    review = service.create(owner, scope, run_id, "policy-change", domain_id, sre_id)
    path = root / "policies.json"
    data = json.loads(path.read_text())
    data["policies"][0]["limitations"] += " policy revision clarified"
    path.write_text(json.dumps(data))
    rejects("EVT_SOURCE_CHANGED", lambda: service.get(owner, scope, review["id"]))


def test_hash_rewrite_cannot_publish_unknown_as_pass(evt_db):
    source, service, owner, scope, _, _, _ = evt_db
    review = create(evt_db)
    with source._tx() as session:
        row = source._get(session, EVTReview, scope, review["id"])
        changed = copy.deepcopy(row.report_json)
        changed["status"] = "PASS"
        row.report_json, row.report_hash = changed, digest(changed)
        row.payload_hash = digest({"scope": values(scope), "report": changed, "audit": row.audit_json})
    rejects("EVT_SOURCE_CHANGED", lambda: service.get(owner, scope, review["id"]))


@pytest.mark.parametrize("failure", ["parent_revision", "expired", "audit"])
def test_stale_or_tampered_approval_rejected(evt_db, failure):
    source, service, owner, scope, run_id, _, _ = evt_db
    review = create(evt_db, SOURCE_POLICY)
    approval = service.request_approval(owner, scope, review["id"], review["report_hash"], "stale")
    with source._tx() as session:
        row = source._get(session, EVTApproval, scope, approval["id"])
        if failure == "parent_revision":
            source._get(session, Run, scope, run_id).revision += 1
        elif failure == "expired":
            row.expires_at = source._now(session) - timedelta(seconds=1)
            from forgeflow.store import view
            row.payload_hash = approval_hash(view(row))
        else:
            row.audit_json = {"scope": "foreign"}
    code = {"parent_revision": "DOMAIN_SOURCE_CHANGED", "expired": "APPROVAL_EXPIRED", "audit": "EVT_APPROVAL_CHANGED"}[failure]
    rejects(code, lambda: service.decide(owner, scope, approval["id"], approval["proposal_hash"], 0, "APPROVED"))


def test_technical_hash_ignores_human_state_but_tracks_check_material():
    scope, run, events = archive("M01")
    original = technical_source(run, events)
    run["revision"] += 1
    run["approvals"], run["receipts"] = [], []
    assert technical_source(run, events) == original
    run["result_json"]["cleanup"] = "FAILED"
    assert technical_source(run, events) != original


def test_reviewer_can_read_and_decide_but_cannot_create(evt_db):
    _, service, owner, scope, run_id, domain_id, sre_id = evt_db
    review = create(evt_db, SOURCE_POLICY)
    approval = service.request_approval(owner, scope, review["id"], review["report_hash"], "reviewer")
    reviewer = owner.model_copy(update={"subject_id": "independent-reviewer", "roles": frozenset({"reviewer"})})
    assert service.get(reviewer, scope, review["id"]) == review
    assert service.get_approval(reviewer, scope, approval["id"]) == approval
    rejects("NOT_FOUND", lambda: service.create(reviewer, scope, run_id, "not-create", domain_id, sre_id))
    rejects("NOT_FOUND", lambda: service.request_approval(reviewer, scope, review["id"], review["report_hash"], "not-request"))
    result = service.decide(reviewer, scope, approval["id"], approval["proposal_hash"], 0, "APPROVED")
    assert result["approval"]["reviewer"] == "independent-reviewer" and result["business_action"] == "NONE"


def test_reviewer_still_needs_parent_document_acl(evt_db):
    source, service, owner, scope, run_id, _, _ = evt_db
    review = create(evt_db, SOURCE_POLICY)
    reviewer = owner.model_copy(update={"roles": frozenset({"reviewer"})})
    assert service.get(reviewer, scope, review["id"]) == review
    with source._tx() as session:
        run = source._get(session, Run, scope, run_id)
        source._get(session, Document, scope, run.snapshot_json["documents"][0]["id"]).roles = ["admin"]
    rejects("NOT_FOUND", lambda: service.get(reviewer, scope, review["id"]))


def test_current_api_blocks_legacy_hardware_only_approval(evt_db):
    source, _, owner, scope, run_id, domain_id, sre_id = evt_db
    client = TestClient(create_evt_app(source, {"offline-token": owner}, None, test_harness=True))
    headers = {"Authorization": "Bearer offline-token", "Idempotency-Key": "http-evt"}
    body = {"scope": values(scope), "domain_review_id": domain_id, "sre_review_id": sre_id}
    response = client.post(f"/runs/{run_id}/evt-review", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["report_json"]["status"] == "UNKNOWN"
    assert client.post(f"/runs/{run_id}/evt-review", json={**body, "applicability": {}}, headers=headers).status_code == 422
    review = response.json()
    assert client.post(f"/evt-reviews/{review['id']}/approval", json={"scope": values(scope),
                      "review_hash": review["report_hash"]}, headers=headers).status_code == 403
    assert client.post("/approvals/historical-id/decision", json={"scope": values(scope), "proposal_hash": "0" * 64,
                       "expected_run_revision": 0, "decision": "APPROVED"}, headers=headers).status_code == 409
