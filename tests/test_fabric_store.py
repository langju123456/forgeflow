"""SQLite OFFLINE_DERIVED receipts; no new execution or PostgreSQL audit claim.

The sealed M01 archive remains read-only. Its scope and document references are
explicitly rebound for this temporary synthetic project and every derived event
hash is rebuilt. Approvals use the current database clock, not archive time.
"""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, func, select
from test_fabric_contract import offline_parent, refresh_bindings

from forgeflow import contracts
from forgeflow.contracts import DomainError, Principal, Scope
from forgeflow.fabric_api import create_fabric_app
from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_runtime import FabricStore
from forgeflow.fabric_store import FabricApproval, FabricItem, FabricOperations, FabricReceipt
from forgeflow.sre_ops import event_chain_valid
from forgeflow.store import (
    Action,
    Approval,
    Artifact,
    Document,
    Event,
    Evidence,
    LocalIssue,
    Memory,
    MemorySource,
    Project,
    Run,
    StageTransition,
    Step,
    aware,
    filters,
    view,
)

ORIGIN = "OFFLINE_DERIVED_SCOPE_AND_DOCUMENT_REBINDING"
DOC_ROLES = {"admin", "supervisor", "reviewer", "fabric_executor", "memory_writer", "hardware"}
DOC_CONTENT = "OFFLINE_DERIVED synthetic Aurora fixture, not a new checker execution or live PostgreSQL audit."


def rejects(code, operation):
    with pytest.raises(DomainError) as error:
        operation()
    assert error.value.code == code


@pytest.fixture
def db(tmp_path):
    def make(*, hardware_status="PASS"):
        source = FabricStore("sqlite:///" + str(tmp_path / ("fabric-" + hardware_status.lower() + ".db")))
        source.create_schema()
        owner = Principal(subject_id="fabric-offline-owner", kind="user", roles=frozenset({"admin", "supervisor"}),
                          scopes=(FABRIC_SCOPE,))
        reviewer = Principal(subject_id="fabric-offline-reviewer", kind="user", roles=frozenset({"reviewer"}),
                             scopes=(FABRIC_SCOPE,))
        executor = Principal(subject_id="fabric-offline-executor", kind="service",
                             roles=frozenset({"fabric_executor", "memory_writer", "hardware"}), scopes=(FABRIC_SCOPE,))
        source.ensure_project(FABRIC_SCOPE)
        run, events = offline_parent()
        validity = (contracts.utcnow() + timedelta(days=1)).isoformat()
        run["snapshot_json"]["valid_until"] = validity
        run["assessment_json"]["status"] = hardware_status
        for reference in run["snapshot_json"]["documents"]:
            document = source.put_document(owner, FABRIC_SCOPE, reference["id"], reference["version"],
                                           DOC_CONTENT, DOC_ROLES, 0, valid_until=validity)
            reference["source_hash"] = document["source_hash"]
        for approval in run["approvals"]:
            approval["proposal"]["snapshot"] = copy.deepcopy(run["snapshot_json"])
        for event in events:
            event["created_at"] = aware(event["created_at"]).isoformat()
        refresh_bindings(run, events)
        # Preserve the two original PENDING proposals as imported history.
        with source._tx() as session:
            for model, items in ((Run, [run]), (Step, run["steps"]), (Artifact, run["artifacts"]),
                                 (Evidence, run["evidence"]), (Approval, run["approvals"]),
                                 (Action, run["actions"]), (Event, events)):
                for item in items:
                    data = {column.name: item[column.name] for column in model.__table__.columns
                            if column.name in item}
                    for column in model.__table__.columns:
                        if isinstance(column.type, DateTime) and data.get(column.name):
                            data[column.name] = aware(data[column.name])
                    session.add(model(**data))
                session.flush()
        service = FabricOperations(source)
        service.create_schema()
        assert event_chain_valid(FABRIC_SCOPE, run["id"], source.events(owner, FABRIC_SCOPE, run["id"]))
        return SimpleNamespace(source=source, service=service, owner=owner, reviewer=reviewer, executor=executor,
                               scope=FABRIC_SCOPE, run_id=run["id"], origin=ORIGIN)
    return make


def review(db, fixture="aligned"):
    return db.service.create(db.owner, db.scope, db.run_id, "offline-review", fixture)


def propose(db, report, kind, *, key=None, target=None):
    return db.service.propose(db.owner, db.scope, report["id"], report["report_hash"],
                             key or "propose-" + kind, kind, target_receipt_id=target)


def approve(db, approval):
    return db.service.decide(db.reviewer, db.scope, approval["id"], approval["proposal_hash"], 0, "APPROVED")


def execute(db, approval):
    return db.service.execute(db.executor, db.scope, approval["id"])["receipt"]


def completed(db, report, kind):
    approval = propose(db, report, kind)
    approve(db, approval)
    return execute(db, approval)


def project(db):
    with db.source._tx() as session:
        return view(session.scalar(select(Project).where(*filters(Project, db.scope))))


def count(db, model):
    with db.source._tx() as session:
        return session.scalar(select(func.count()).select_from(model).where(*filters(model, db.scope)))


def item(db, item_id):
    with db.source._tx() as session:
        return view(db.source._get(session, FabricItem, db.scope, item_id))


def advance_document(db):
    db.source.put_document(db.owner, db.scope, "aurora-evt-sop", "v2", DOC_CONTENT + " New version.",
                           DOC_ROLES, 0, valid_until=(contracts.utcnow() + timedelta(days=1)).isoformat())


def test_review_and_core_prepare_do_not_execute_or_mint_hardware_only_approvals(db):
    db = db()
    before = project(db)
    report = review(db)
    assert report["report_json"]["flow_gate"] == "PASS" and report["report_json"]["evt_gate"] == "UNKNOWN"
    assert report["report_json"]["full_evt_eligibility"] is False and db.origin == ORIGIN
    assert project(db) == before and count(db, FabricApproval) == count(db, FabricReceipt) == count(db, FabricItem) == 0
    assert count(db, Approval) == 2 and count(db, Action) == 2
    assert count(db, LocalIssue) == count(db, StageTransition) == 0
    with db.source._tx() as session:
        run = db.source._get(session, Run, db.scope, db.run_id)
        current = db.source._project(session, db.scope)
        assert db.source._prepare(session, db.owner, db.scope, run, "STAGE", {}, current) is None
        assert db.source._prepare(session, db.owner, db.scope, run, "ISSUE", {}, current) is None
    assert count(db, Approval) == 2


def test_stage_approval_and_execution_are_separate_parameter_bound_steps(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "TEST_STAGE")
    assert approval["decision"] == "PENDING"
    assert approval["proposal"]["parameters"] == {"from_stage": "PROTOTYPE", "to_stage": "TEST_STAGE",
                                                    "expected_revision": 0}
    rejects("APPROVAL_REQUIRED", lambda: execute(db, approval))
    decision = approve(db, approval)
    assert decision["executed"] is False and project(db)["current_stage"] == "PROTOTYPE"
    assert count(db, FabricReceipt) == 0
    receipt = execute(db, approval)
    assert project(db)["current_stage"] == "TEST_STAGE" and project(db)["stage_revision"] == 1
    assert receipt["effect_json"]["simulation"] is True
    assert receipt["effect_json"]["before"] == {"stage": "PROTOTYPE", "revision": 0}
    assert receipt["effect_json"]["after"] == {"stage": "TEST_STAGE", "revision": 1}
    assert db.service.get(db.owner, db.scope, report["id"]) == report


def test_local_issue_effect_creates_one_bound_item_without_remote_tracker(db):
    db = db()
    report = review(db)
    receipt = completed(db, report, "ISSUE")
    issue = item(db, receipt["effect_json"]["item_id"])
    assert issue["kind"] == "ISSUE" and issue["status"] == "OPEN" and issue["revision"] == 0
    assert issue["body"]["parameters"]["remote_tracker"] is False
    assert issue["body"]["review_id"] == report["id"] and issue["body"]["review_hash"] == report["report_hash"]
    assert count(db, FabricItem) == count(db, FabricReceipt) == 1 and count(db, LocalIssue) == 0
    assert project(db)["current_stage"] == "PROTOTYPE"


def test_propose_decide_execute_and_new_store_replay_do_not_duplicate_effects(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "TEST_STAGE")
    assert propose(db, report, "TEST_STAGE") == approval
    rejects("FABRIC_ACTION_ALREADY_PROPOSED", lambda: propose(db, report, "TEST_STAGE", key="another-key"))
    rejects("IDEMPOTENCY_CONFLICT", lambda: propose(db, report, "ISSUE", key="propose-TEST_STAGE"))
    first_decision = approve(db, approval)
    first = db.service.execute(db.executor, db.scope, approval["id"])
    fresh_source = FabricStore(db.source.url)
    fresh = FabricOperations(fresh_source)
    assert fresh.create(db.owner, db.scope, db.run_id, "offline-review") == report
    replay = fresh.decide(db.reviewer, db.scope, approval["id"], approval["proposal_hash"], 0, "APPROVED")
    assert first_decision["replayed"] is False and replay["replayed"] is True and replay["executed"] is False
    assert fresh.execute(db.executor, db.scope, approval["id"]) == {"receipt": first["receipt"], "replayed": True}
    assert count(db, FabricApproval) == count(db, FabricReceipt) == 1 and project(db)["stage_revision"] == 1


def test_scope_roles_and_document_acl_reauthorize_existing_reports(db):
    db = db()
    report = review(db)
    foreign = Scope(tenant_id="foreign", workspace_id=db.scope.workspace_id, project_id=db.scope.project_id)
    broad_owner = db.owner.model_copy(update={"scopes": (db.scope, foreign)})
    rejects("NOT_FOUND", lambda: db.service.get(broad_owner, foreign, report["id"]))
    rejects("NOT_FOUND", lambda: db.service.create(db.reviewer, db.scope, db.run_id, "reviewer-create"))
    observer = db.owner.model_copy(update={"roles": frozenset({"submitter"})})
    rejects("NOT_FOUND", lambda: db.service.get(observer, db.scope, report["id"]))
    with db.source._tx() as session:
        document = db.source._get(session, Document, db.scope, "aurora-evt-sop")
        document.roles = ["admin", "supervisor"]
    rejects("NOT_FOUND", lambda: db.service.get(db.reviewer, db.scope, report["id"]))


def test_only_independent_human_can_decide_and_service_can_execute(db):
    db = db()
    report = review(db)
    approval = propose(db, report, "ISSUE")
    self_reviewer = db.owner.model_copy(update={"roles": frozenset({"reviewer"})})
    rejects("INDEPENDENT_REVIEW_REQUIRED", lambda: db.service.decide(
        self_reviewer, db.scope, approval["id"], approval["proposal_hash"], 0, "APPROVED"))
    robot_reviewer = db.reviewer.model_copy(update={"kind": "service"})
    rejects("HUMAN_REVIEW_REQUIRED", lambda: db.service.decide(
        robot_reviewer, db.scope, approval["id"], approval["proposal_hash"], 0, "APPROVED"))
    robot_owner = db.owner.model_copy(update={"kind": "service"})
    rejects("HUMAN_REQUEST_REQUIRED", lambda: db.service.create(robot_owner, db.scope, db.run_id, "robot-review"))
    approve(db, approval)
    human_executor = db.executor.model_copy(update={"kind": "user"})
    rejects("TRUSTED_EXECUTOR_REQUIRED", lambda: db.service.execute(human_executor, db.scope, approval["id"]))
    assert count(db, FabricReceipt) == 0


@pytest.mark.parametrize("condition", ["unknown", "contradiction"])
def test_unknown_or_blocked_composite_gate_denies_forward_actions(db, condition):
    db = db(hardware_status="UNKNOWN" if condition == "unknown" else "PASS")
    report = review(db, "bom-mismatch" if condition == "contradiction" else "aligned")
    assert report["report_json"]["flow_gate"] == ("UNKNOWN" if condition == "unknown" else "BLOCKED")
    for kind in ("TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE"):
        rejects("FABRIC_GATE_NOT_PASS", lambda kind=kind: propose(db, report, kind))
    assert count(db, FabricApproval) == count(db, FabricReceipt) == 0


@pytest.mark.parametrize("phase", ["decision", "execution"])
def test_current_stage_revision_changes_reject_bound_parameters(db, phase):
    db = db()
    approval = propose(db, review(db), "TEST_STAGE")
    if phase == "execution":
        approve(db, approval)
    with db.source._tx() as session:
        db.source._project(session, db.scope).stage_revision += 1
    rejects("FABRIC_PARAMETERS_CHANGED", lambda: approve(db, approval) if phase == "decision" else execute(db, approval))
    assert project(db)["current_stage"] == "PROTOTYPE" and count(db, FabricReceipt) == 0


@pytest.mark.parametrize("phase", ["decision", "execution"])
def test_current_document_version_changes_block_approval_or_execution(db, phase):
    db = db()
    approval = propose(db, review(db), "ISSUE")
    if phase == "execution":
        approve(db, approval)
    advance_document(db)
    rejects("SOURCE_CHANGED", lambda: approve(db, approval) if phase == "decision" else execute(db, approval))
    assert count(db, FabricReceipt) == count(db, FabricItem) == 0


def test_memory_activation_requires_both_approved_local_effect_receipts(db):
    db = db()
    report = review(db)
    rejects("FABRIC_EFFECT_SOURCES_REQUIRED", lambda: propose(db, report, "MEMORY_ACTIVATE"))
    completed(db, report, "TEST_STAGE")
    rejects("FABRIC_EFFECT_SOURCES_REQUIRED", lambda: propose(db, report, "MEMORY_ACTIVATE"))
    assert count(db, Memory) == 0 and db.service.memory(db.owner, db.scope) == []


def test_memory_reference_uses_composite_review_and_receipts_and_revokes_without_confirming_candidate(db):
    db = db()
    report = review(db)
    stage = completed(db, report, "TEST_STAGE")
    issue = completed(db, report, "ISSUE")
    activation = completed(db, report, "MEMORY_ACTIVATE")
    references = db.service.memory(db.owner, db.scope, "Aurora")
    assert len(references) == 1
    reference, citation = references[0]["reference"], references[0]["citation"]
    assert reference["status"] == "ACTIVE" and citation["status"] == "CANDIDATE"
    assert citation["human_confirmed"] is False and references[0]["confirms_evt"] is False
    assert references[0]["approval_provenance"] == "TEST_DRIVER"
    assert sorted(citation["source_event_ids"]) == sorted([report["event_id"], stage["event_id"], issue["event_id"]])
    assert reference["body"]["source_event_hashes"] == {row["id"]: row["event_hash"] for row in citation["sources"]}
    assert reference["body"]["parameters"]["receipt_hashes"] == {stage["id"]: stage["payload_hash"],
                                                                    issue["id"]: issue["payload_hash"]}
    before = db.source.cite(db.owner, db.scope, "memory", activation["effect_json"]["memory_id"])
    revocation = propose(db, report, "MEMORY_REVOKE", target=activation["id"])
    rejects("APPROVAL_REQUIRED", lambda: execute(db, revocation))
    approve(db, revocation)
    reversed_receipt = execute(db, revocation)
    assert reversed_receipt["effect_json"]["target_receipt_id"] == activation["id"]
    assert item(db, reference["id"])["status"] == "REVOKED" and db.service.memory(db.owner, db.scope) == []
    assert db.source.cite(db.owner, db.scope, "memory", activation["effect_json"]["memory_id"]) == before


@pytest.mark.parametrize("condition", ["expired", "future", "revoked"])
def test_inactive_underlying_memory_cannot_be_retrieved_as_an_active_reference(db, condition):
    db = db()
    report = review(db)
    completed(db, report, "TEST_STAGE")
    completed(db, report, "ISSUE")
    activation = completed(db, report, "MEMORY_ACTIVATE")
    assert len(db.service.memory(db.owner, db.scope)) == 1
    with db.source._tx() as session:
        candidate = db.source._get(session, Memory, db.scope, activation["effect_json"]["memory_id"])
        now = db.source._now(session)
        if condition == "expired":
            candidate.valid_until = now - timedelta(minutes=1)
        elif condition == "future":
            candidate.valid_from = now + timedelta(minutes=1)
        else:
            candidate.status = "REVOKED"
    assert item(db, activation["effect_json"]["item_id"])["status"] == "ACTIVE"
    assert db.service.memory(db.owner, db.scope) == []
    assert count(db, FabricReceipt) == 3


@pytest.mark.parametrize("original_kind,reverse_kind", [("TEST_STAGE", "STAGE_REVERT"), ("ISSUE", "ISSUE_RETRACT")])
def test_compensation_is_separately_approved_receipt_bound_and_can_preserve_stale_sources(db, original_kind, reverse_kind):
    db = db()
    report = review(db)
    original = completed(db, report, original_kind)
    advance_document(db)
    compensation = propose(db, report, reverse_kind, target=original["id"])
    assert compensation["proposal"]["compensation_allows_stale_technical_evidence"] is True
    assert compensation["proposal"]["parameters"]["receipt_hash"] == original["payload_hash"]
    rejects("APPROVAL_REQUIRED", lambda: execute(db, compensation))
    approve(db, compensation)
    result = execute(db, compensation)
    assert result["effect_json"]["target_receipt_id"] == original["id"] and count(db, FabricReceipt) == 2
    if original_kind == "TEST_STAGE":
        assert project(db)["current_stage"] == "PROTOTYPE" and project(db)["stage_revision"] == 2
    else:
        assert item(db, original["effect_json"]["item_id"])["status"] == "RETRACTED"
    assert event_chain_valid(db.scope, db.run_id, db.source.events(db.owner, db.scope, db.run_id))


def test_exception_rolls_back_memory_item_candidate_sources_receipt_and_audit_then_retry_succeeds(db):
    db = db()
    report = review(db)
    completed(db, report, "TEST_STAGE")
    completed(db, report, "ISSUE")
    approval = propose(db, report, "MEMORY_ACTIVATE")
    approve(db, approval)
    tracked = (Memory, MemorySource, FabricItem, FabricReceipt, Event)
    before = {model.__tablename__: count(db, model) for model in tracked}
    run_before = db.source.get_run(db.owner, db.scope, db.run_id)
    def fault():
        raise RuntimeError("OFFLINE_DERIVED_ATOMIC_FAULT")
    with pytest.raises(RuntimeError, match="OFFLINE_DERIVED_ATOMIC_FAULT"):
        db.service.execute(db.executor, db.scope, approval["id"], _before_commit=fault)
    assert {model.__tablename__: count(db, model) for model in tracked} == before
    assert db.source.get_run(db.owner, db.scope, db.run_id) == run_before
    assert db.service.get_approval(db.owner, db.scope, approval["id"])["decision"] == "APPROVED"
    receipt = execute(db, approval)
    assert count(db, Memory) == 1 and count(db, MemorySource) == 3 and count(db, FabricReceipt) == 3
    assert db.service.execute(db.executor, db.scope, approval["id"]) == {"receipt": receipt, "replayed": True}


def test_fabric_api_auth_is_separate_from_body_roles_and_human_execution(db):
    db = db()
    client = TestClient(create_fabric_app(db.source, {"offline-owner": db.owner, "offline-reviewer": db.reviewer,
                                                    "offline-executor": db.executor}, None, test_harness=True))
    path, body = f"/fabric/runs/{db.run_id}/review", {"scope": db.scope.model_dump(mode="json")}
    headers = {"Authorization": "Bearer offline-owner", "Idempotency-Key": "api-review"}
    assert client.post(path, json=body, headers={"Idempotency-Key": "api-review"}).status_code == 401
    assert client.post(path, json={**body, "role": "admin"}, headers=headers).status_code == 422
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    proposal = client.post(f"/fabric/reviews/{response.json()['id']}/proposals",
        json={**body, "review_hash": response.json()["report_hash"], "kind": "ISSUE"},
        headers={**headers, "Idempotency-Key": "api-propose"})
    assert proposal.status_code == 200
    execute_path = f"/fabric/approvals/{proposal.json()['id']}/execute"
    assert client.post(execute_path, json=body["scope"], headers=headers).status_code == 403
    assert count(db, FabricReceipt) == 0
