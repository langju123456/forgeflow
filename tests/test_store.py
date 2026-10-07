"""State/authorization/fault invariants, runnable on SQLite or PostgreSQL without dropping data."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeflow import contracts
from forgeflow.contracts import DomainError, Principal, RunRequest, Scope, digest
from forgeflow.store import Approval, Artifact, Event, Run, Step, Store, filters, values


@pytest.fixture
def env(tmp_path):
    url = os.getenv("FORGEFLOW_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'state.sqlite'}")
    store = Store(url)
    store.create_schema()
    scope = Scope(tenant_id="t" + uuid4().hex[:12], workspace_id="w", project_id="aurora")
    human = Principal(
        subject_id="reviewer",
        kind="user",
        scopes=(scope,),
        clearance=3,
        roles=frozenset({"admin", "reviewer", "submitter", "hardware", "memory_writer"}),
    )
    worker = Principal(
        subject_id="runtime",
        kind="service",
        scopes=(scope,),
        clearance=3,
        roles=frozenset({"worker", "hardware", "memory_writer"}),
    )
    return store, scope, human, worker


def submit(env, key="initial", fixture="aurora-pass"):
    store, scope, human, _ = env
    return store.submit(human, RunRequest(scope=scope, fixture_id=fixture), key)


def checked(env, key="initial", status="PASS", documents=None):
    store, scope, _human, worker = env
    run = submit(env, key)
    claim = store.claim(worker, scope, run["id"], "worker-a")
    result = {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": [], "stdout": "synthetic"}
    snapshot = {
        "policy_version": "1",
        "skill_version": "1",
        "rule_version": "1",
        "classification": 0,
        "input_hash": "a" * 64,
        "documents": documents or [],
    }
    run = store.complete_check(
        worker,
        scope,
        run["id"],
        claim["token"],
        claim["epoch"],
        result,
        {"status": status, "evidence": []},
        snapshot,
    )
    return run, claim


def approve_all(env, run):
    store, scope, human, _ = env
    for approval in run["approvals"]:
        latest = store.get_run(human, scope, run["id"])
        store.decide(human, scope, approval["id"], approval["proposal_hash"], latest["revision"], "APPROVED")
    return store.get_run(human, scope, run["id"])


def expect_error(code, callable):
    with pytest.raises(DomainError) as raised:
        callable()
    assert raised.value.code == code


def expire(store, scope, run_id):
    with Session(store.engine) as s, s.begin():
        row = s.scalar(
            select(Step).where(*filters(Step, scope), Step.run_id == run_id, Step.lease_token.is_not(None))
        )
        row.lease_expires_at = contracts.utcnow() - timedelta(minutes=1)


def test_concurrent_idempotency_and_subject_namespace(env):
    store, scope, human, _ = env

    def create(_):
        return store.submit(human, RunRequest(scope=scope, fixture_id="aurora-pass"), "concurrent")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        identifiers = list(pool.map(create, range(12)))
    assert len(set(identifiers)) == 1
    expect_error(
        "IDEMPOTENCY_CONFLICT",
        lambda: store.submit(human, RunRequest(scope=scope, fixture_id="aurora-unknown"), "concurrent"),
    )
    second = human.model_copy(update={"subject_id": "second"})
    assert (
        store.submit(second, RunRequest(scope=scope, fixture_id="aurora-pass"), "concurrent")["id"]
        != identifiers[0]
    )
    assert len(store.events(human, scope, identifiers[0])) == 1


def test_real_local_chain_and_separate_stage_approval(env):
    store, scope, human, worker = env
    run, _ = checked(env)
    assert run["state"] == "WAITING_APPROVAL"
    assert run["project"]["current_stage"] == "PROTOTYPE"
    assert {a["kind"] for a in run["approvals"]} == {"ISSUE", "STAGE"}
    run = approve_all(env, run)
    lease = store.claim(worker, scope, run["id"], "worker-dispatch")
    responses = []
    for action in run["actions"]:
        responses.append(
            store.dispatch_local(
                worker,
                scope,
                run["id"],
                action["id"],
                "worker-dispatch",
                lease_token=lease["token"],
                epoch=lease["epoch"],
            )
        )
    latest = store.get_run(human, scope, run["id"])
    assert latest["state"] == "COMPLETED"
    assert latest["project"]["current_stage"] == "EVT"
    assert latest["project"]["stage_revision"] == 1
    assert len(latest["receipts"]) == 2
    action = run["actions"][0]
    replay = store.dispatch_local(
        worker,
        scope,
        run["id"],
        action["id"],
        "worker-dispatch",
        lease_token=lease["token"],
        epoch=lease["epoch"],
    )
    assert replay["replayed"] is True
    assert store.verify_events(human, scope, run["id"])


@pytest.mark.parametrize("status", ["BLOCKED", "UNKNOWN"])
def test_corrective_issue_independent_of_gate(env, status):
    store, scope, human, worker = env
    run, _ = checked(env, status=status)
    assert [a["kind"] for a in run["approvals"]] == ["ISSUE"]
    run = approve_all(env, run)
    lease = store.claim(worker, scope, run["id"], "worker")
    store.dispatch_local(
        worker,
        scope,
        run["id"],
        run["actions"][0]["id"],
        "worker",
        lease_token=lease["token"],
        epoch=lease["epoch"],
    )
    latest = store.get_run(human, scope, run["id"])
    assert latest["state"] == "COMPLETED"
    assert latest["assessment_json"]["status"] == status
    assert latest["project"]["current_stage"] == "PROTOTYPE"


def test_expired_lease_fencing_and_cleanup_barrier(env):
    store, scope, _human, worker = env
    run = submit(env)
    first = store.claim(worker, scope, run["id"], "first")
    expire(store, scope, run["id"])
    expect_error(
        "STALE_LEASE", lambda: store.heartbeat(worker, scope, run["id"], first["token"], first["epoch"])
    )
    expect_error("RECOVERY_REQUIRED", lambda: store.claim(worker, scope, run["id"], "second"))
    recovery = store.recover(worker, scope, run["id"])
    assert recovery["sandbox_handle"] == first["sandbox_handle"]
    expect_error("CLEANUP_REQUIRED", lambda: store.claim(worker, scope, run["id"], "second"))
    assert store.acknowledge_recovery(worker, scope, run["id"], "FAILED")["state"] == "NEEDS_REVIEW"
    store.acknowledge_recovery(worker, scope, run["id"], "REMOVED")
    second = store.claim(worker, scope, run["id"], "second")
    assert second["epoch"] > first["epoch"] and second["attempt"] == 2
    expect_error(
        "STALE_LEASE",
        lambda: store.complete_check(
            worker,
            scope,
            run["id"],
            first["token"],
            first["epoch"],
            {"status": "SUCCEEDED", "cleanup": "REMOVED"},
            {"status": "PASS"},
            {},
        ),
    )


def test_stale_worker_cannot_consume_approval(env):
    store, scope, human, worker = env
    run, _ = checked(env)
    run = approve_all(env, run)
    lease = store.claim(worker, scope, run["id"], "old")
    expire(store, scope, run["id"])
    expect_error(
        "STALE_LEASE",
        lambda: store.dispatch_local(
            worker,
            scope,
            run["id"],
            run["actions"][0]["id"],
            "old",
            lease_token=lease["token"],
            epoch=lease["epoch"],
        ),
    )
    latest = store.get_run(human, scope, run["id"])
    assert all(a["consumed_by_action"] is None for a in latest["approvals"])
    assert not latest["receipts"]


def test_cancel_before_dispatch_prevents_effect(env):
    store, scope, human, worker = env
    run, _ = checked(env)
    run = approve_all(env, run)
    lease = store.claim(worker, scope, run["id"], "worker")
    cancelled = store.cancel(human, scope, run["id"])
    assert cancelled["state"] == "RUNNING"
    assert cancelled["cancel_requested_at"]
    expect_error(
        "CANCEL_REQUESTED",
        lambda: store.dispatch_local(
            worker,
            scope,
            run["id"],
            run["actions"][0]["id"],
            "worker",
            lease_token=lease["token"],
            epoch=lease["epoch"],
        ),
    )
    assert not store.get_run(human, scope, run["id"])["receipts"]
    assert (
        store.acknowledge_cancel(worker, scope, run["id"], lease["token"], lease["epoch"], "REMOVED")["state"]
        == "CANCELLED"
    )


def test_cancel_race_has_serialized_result(env):
    store, scope, human, worker = env
    run, _ = checked(env, status="UNKNOWN")
    run = approve_all(env, run)
    lease = store.claim(worker, scope, run["id"], "worker")

    def dispatch():
        try:
            return store.dispatch_local(
                worker,
                scope,
                run["id"],
                run["actions"][0]["id"],
                "worker",
                lease_token=lease["token"],
                epoch=lease["epoch"],
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(dispatch)
        b = pool.submit(store.cancel, human, scope, run["id"])
        dispatch_result, _ = a.result(), b.result()
    latest = store.get_run(human, scope, run["id"])
    assert len(latest["receipts"]) in {0, 1}
    assert (dispatch_result == "CANCEL_REQUESTED") == (not latest["receipts"])
    assert latest["cancel_requested_at"]


def test_approval_hash_revision_expiry_and_agent_denial(env):
    store, scope, human, _worker = env
    run, _ = checked(env)
    approval = run["approvals"][0]
    expect_error(
        "PROPOSAL_HASH_MISMATCH",
        lambda: store.decide(human, scope, approval["id"], "0" * 64, run["revision"], "APPROVED"),
    )
    expect_error(
        "REVISION_CONFLICT",
        lambda: store.decide(
            human, scope, approval["id"], approval["proposal_hash"], run["revision"] - 1, "APPROVED"
        ),
    )
    agent = Principal(subject_id="self", kind="agent", scopes=(scope,), roles=frozenset({"reviewer"}))
    expect_error(
        "HUMAN_REVIEW_REQUIRED",
        lambda: store.decide(
            agent, scope, approval["id"], approval["proposal_hash"], run["revision"], "APPROVED"
        ),
    )
    with Session(store.engine) as s, s.begin():
        row = s.scalar(select(Approval).where(*filters(Approval, scope), Approval.id == approval["id"]))
        row.expires_at = contracts.utcnow() - timedelta(seconds=5)
    expect_error(
        "APPROVAL_EXPIRED",
        lambda: store.decide(
            human, scope, approval["id"], approval["proposal_hash"], run["revision"], "APPROVED"
        ),
    )


def test_material_snapshot_tamper_rejected(env):
    store, scope, human, _worker = env
    run, _ = checked(env)
    approval = run["approvals"][0]
    with Session(store.engine) as s, s.begin():
        row = s.scalar(select(Run).where(*filters(Run, scope), Run.id == run["id"]))
        row.snapshot_json = {**row.snapshot_json, "policy_version": "changed"}
    expect_error(
        "PROPOSAL_CHANGED",
        lambda: store.decide(
            human, scope, approval["id"], approval["proposal_hash"], run["revision"], "APPROVED"
        ),
    )


def test_two_runs_compete_project_stage_cas(env):
    store, scope, human, worker = env
    first, _ = checked(env, key="one")
    second, _ = checked(env, key="two")
    first, second = approve_all(env, first), approve_all(env, second)
    leases = [store.claim(worker, scope, r["id"], str(index)) for index, r in enumerate([first, second])]

    def advance(index):
        run, lease = [first, second][index], leases[index]
        action = next(a for a in run["actions"] if a["tool"] == "stage.transition.local")
        try:
            store.dispatch_local(
                worker,
                scope,
                run["id"],
                action["id"],
                str(index),
                lease_token=lease["token"],
                epoch=lease["epoch"],
            )
            return "SUCCEEDED"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(advance, range(2)))
    assert sorted(outcomes) == ["STAGE_CONFLICT", "SUCCEEDED"]
    assert store.get_run(human, scope, first["id"])["project"]["stage_revision"] == 1


@pytest.mark.parametrize("other_tenant", [True, False])
def test_scope_read_write_citation_and_fk_isolation(env, other_tenant):
    store, scope, human, worker = env
    other = Scope(
        tenant_id=("other" + uuid4().hex[:8]) if other_tenant else scope.tenant_id,
        workspace_id=scope.workspace_id,
        project_id="other-project",
    )
    second = human.model_copy(update={"scopes": (other,)})
    run = submit(env)
    store.submit(second, RunRequest(scope=other, fixture_id="aurora-pass"), "initial")
    expect_error("NOT_FOUND", lambda: store.get_run(second, scope, run["id"]))
    expect_error("NOT_FOUND", lambda: store.get_run(second, other, run["id"]))
    expect_error("NOT_FOUND", lambda: store.events(second, other, run["id"]))
    event = store.events(human, scope, run["id"])[0]
    expect_error("NOT_FOUND", lambda: store.cite(second, other, "event", event["id"]))
    second_worker = worker.model_copy(update={"scopes": (other,)})
    other_run = store.submit(second, RunRequest(scope=other, fixture_id="aurora-pass"), "another")
    expect_error(
        "NOT_FOUND",
        lambda: store.write_memory(
            second_worker, other, other_run["id"], "aurora", "foreign source", [event["id"]]
        ),
    )
    lease = store.claim(worker, scope, run["id"], "worker")
    with pytest.raises(IntegrityError), Session(store.engine) as s, s.begin():
        s.add(
            Artifact(
                **values(other),
                id=str(uuid4()),
                run_id=run["id"],
                step_id=lease["step_id"],
                path="evidence.json",
                content="{}",
                content_sha256=digest({}),
                size=2,
                classification=0,
                provenance={},
            )
        )


def test_knowledge_acl_before_content_and_exact_version_citation(env):
    store, scope, human, _ = env
    old = store.put_document(human, scope, "EVT", "v1", "old manifest", {"hardware"}, 0)
    private = store.put_document(
        human, scope, "private", "v1", "secret manifest strongest exact EVT", {"sre"}, 2
    )
    restricted = Principal(
        subject_id="hardware", kind="agent", scopes=(scope,), roles=frozenset({"hardware"}), clearance=1
    )
    assert [r["id"] for r in store.document_rows(restricted, scope)] == ["EVT"]
    expect_error("NOT_FOUND", lambda: store.cite(restricted, scope, "document", private["citation_id"]))
    store.put_document(human, scope, "EVT", "v2", "new manifest", {"hardware"}, 0)
    citation = store.cite(restricted, scope, "document", old["citation_id"])
    assert citation["content"] == "old manifest" and citation["version"] == "v1"
    assert citation["status"] == "SUPERSEDED"
    assert store.document_rows(restricted, scope)[0]["version"] == "v2"


def test_source_change_prevents_previously_approved_stage(env):
    store, scope, human, worker = env
    doc = store.put_document(human, scope, "EVT", "v1", "manifest", {"hardware"}, 0)
    reference = {"id": doc["id"], "version": doc["version"], "source_hash": doc["source_hash"]}
    run, _ = checked(env, documents=[reference])
    run = approve_all(env, run)
    store.put_document(human, scope, "EVT", "v2", "changed manifest", {"hardware"}, 0)
    lease = store.claim(worker, scope, run["id"], "worker")
    action = next(a for a in run["actions"] if a["tool"] == "stage.transition.local")
    expect_error(
        "SOURCE_CHANGED",
        lambda: store.dispatch_local(
            worker, scope, run["id"], action["id"], "worker", lease_token=lease["token"], epoch=lease["epoch"]
        ),
    )
    assert store.get_run(human, scope, run["id"])["project"]["current_stage"] == "PROTOTYPE"


def test_memory_sources_dedup_confirm_and_cross_session(env):
    store, scope, human, worker = env
    run = submit(env)
    source = store.events(human, scope, run["id"])[0]
    first = store.write_memory(
        worker, scope, run["id"], "aurora", "Synthetic check requested", [source["id"]]
    )
    second = store.write_memory(
        worker, scope, run["id"], "aurora", "Synthetic check requested", [source["id"], source["id"]]
    )
    assert first["id"] == second["id"]
    expect_error(
        "HUMAN_REVIEW_REQUIRED",
        lambda: store.write_memory(
            worker, scope, run["id"], "aurora", "decision", [source["id"]], status="CONFIRMED"
        ),
    )
    reopened = Store(store.url)
    hardware = Principal(subject_id="later", kind="agent", scopes=(scope,), roles=frozenset({"hardware"}))
    rows = reopened.memory_rows(hardware, scope)
    assert len(rows) == 1 and rows[0]["status"] == "CANDIDATE"
    assert rows[0]["source_event_ids"] == [source["id"]]
    assert reopened.cite(hardware, scope, "memory", first["id"])["sources"][0]["id"] == source["id"]


def test_unverified_pass_and_timeout_status(env):
    store, scope, _human, worker = env
    run = submit(env, fixture="aurora-timeout")
    lease = store.claim(worker, scope, run["id"], "worker")
    result = {"status": "TIMED_OUT", "cleanup": "REMOVED", "artifacts": []}
    expect_error(
        "UNVERIFIED_PASS",
        lambda: store.complete_check(
            worker, scope, run["id"], lease["token"], lease["epoch"], result, {"status": "PASS"}, {}
        ),
    )
    failure = store.fail_check(worker, scope, run["id"], lease["token"], lease["epoch"], result)
    assert failure["state"] == "FAILED" and failure["assessment_json"]["status"] == "UNKNOWN"
    assert failure["steps"][0]["state"] == "TIMED_OUT" and not failure["approvals"]


def test_audit_tamper_detected(env):
    store, scope, human, _ = env
    run = submit(env)
    assert store.verify_events(human, scope, run["id"])
    with Session(store.engine) as s, s.begin():
        record = s.scalar(select(Event).where(*filters(Event, scope), Event.run_id == run["id"]))
        record.payload = {"tampered": True}
    assert store.verify_events(human, scope, run["id"]) is False


def test_derived_report_acl_and_memory_no_role_downgrade(env):
    store, scope, human, worker = env
    document = store.put_document(
        human, scope, "private-context", "v1", "restricted hardware notes", {"hardware"}, 0
    )
    reference = {"id": document["id"], "version": document["version"], "source_hash": document["source_hash"]}
    run, _ = checked(env, documents=[reference])
    event = store.events(human, scope, run["id"])[-1]
    memory = store.write_memory(
        worker, scope, run["id"], "private", "restricted context decision", [event["id"]]
    )
    pm = Principal(
        subject_id="pm",
        kind="user",
        scopes=(scope,),
        clearance=3,
        roles=frozenset({"pm", "reviewer", "submitter"}),
    )
    expect_error("NOT_FOUND", lambda: store.get_run(pm, scope, run["id"]))
    expect_error("NOT_FOUND", lambda: store.events(pm, scope, run["id"]))
    expect_error("NOT_FOUND", lambda: store.cite(pm, scope, "event", event["id"]))
    expect_error("NOT_FOUND", lambda: store.cite(pm, scope, "memory", memory["id"]))
    approval = run["approvals"][0]
    expect_error(
        "NOT_FOUND",
        lambda: store.decide(
            pm, scope, approval["id"], approval["proposal_hash"], run["revision"], "APPROVED"
        ),
    )
    assert store.memory_rows(pm, scope) == []
    identity_pm = pm.model_copy(update={"subject_id": human.subject_id})
    expect_error(
        "NOT_FOUND",
        lambda: store.submit(identity_pm, RunRequest(scope=scope, fixture_id="aurora-pass"), "initial"),
    )


def test_memory_foreign_run_source_denied(env):
    store, scope, human, worker = env
    first = submit(env, "first")
    second = submit(env, "second")
    source = store.events(human, scope, first["id"])[0]
    expect_error(
        "NOT_FOUND",
        lambda: store.write_memory(worker, scope, second["id"], "decision", "cross run", [source["id"]]),
    )


def test_consumed_approval_action_fk_has_full_scope(env):
    store, scope, human, worker = env
    first, _ = checked(env)
    other = scope.model_copy(update={"project_id": "other-project"})
    second_human = human.model_copy(update={"scopes": (other,)})
    second_worker = worker.model_copy(update={"scopes": (other,)})
    second, _ = checked((store, other, second_human, second_worker))
    with pytest.raises(IntegrityError), Session(store.engine) as s, s.begin():
        row = s.scalar(
            select(Approval).where(*filters(Approval, scope), Approval.id == first["approvals"][0]["id"])
        )
        row.consumed_by_action = second["actions"][0]["id"]


@pytest.mark.parametrize(
    "artifact",
    [
        {"path": "../escape", "content": "{}"},
        {"path": "check.json", "content": "{}", "sha256": "0" * 64},
        {"path": "check.json", "content": "{}", "size": 999},
    ],
)
def test_untrusted_artifact_manifest_rejected_atomically(env, artifact):
    store, scope, human, worker = env
    run = submit(env)
    lease = store.claim(worker, scope, run["id"], "worker")
    expect_error(
        "ARTIFACT_MANIFEST_INVALID",
        lambda: store.complete_check(
            worker,
            scope,
            run["id"],
            lease["token"],
            lease["epoch"],
            {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": [artifact]},
            {"status": "PASS"},
            {},
        ),
    )
    latest = store.get_run(human, scope, run["id"])
    assert latest["state"] == "RUNNING" and not latest["artifacts"] and not latest["approvals"]
