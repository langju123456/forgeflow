"""S2 durability/ACL/fault invariants. No provider, inference, or business effects."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeflow.analysis_store import AnalysisCall, AnalysisProviderSlot, AnalysisRun, AnalysisStore
from forgeflow.contracts import DomainError, Principal, RunRequest, Scope
from forgeflow.store import Document, Run, Store, filters


def authorized(_session, _principal, _scope, _material):
    """TEST_HARNESS_ONLY: every fixture source is synthetic and export-approved."""


@pytest.fixture
def env(tmp_path):
    import os

    store = Store(os.getenv("FORGEFLOW_TEST_DATABASE_URL", f"sqlite:///{tmp_path / 'analysis.sqlite'}"))
    store.create_schema()
    analysis = AnalysisStore(store)
    analysis.create_schema()
    scope = Scope(tenant_id="a" + uuid4().hex[:12], workspace_id="w", project_id="aurora")
    p = Principal(
        subject_id="caller",
        kind="user",
        scopes=(scope,),
        clearance=3,
        roles=frozenset({"submitter", "pm", "hardware"}),
    )
    worker = p.model_copy(
        update={
            "kind": "service",
            "subject_id": "hardware-worker",
            "roles": frozenset({"hardware", "worker"}),
        }
    )
    run = store.submit(p, RunRequest(scope=scope, fixture_id="aurora-pass"), "hardware")
    claim = store.claim(worker, scope, run["id"], "hw")
    store.complete_check(
        worker,
        scope,
        run["id"],
        claim["token"],
        claim["epoch"],
        {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": []},
        {"status": "PASS", "evidence": []},
        {"classification": 0, "documents": [], "input_hash": "a" * 64},
    )
    material = {
        "profile_id": "test-profile-" + uuid4().hex,
        "profile_revision": "b" * 64,
        "policy_revision": "analysis-v1",
        "source_snapshot": {"references": []},
    }
    return store, analysis, scope, p, run["id"], material


def started(env, key="analysis"):
    _s, a, scope, p, run_id, material = env
    row = a.start(p, scope, run_id, key, material, reauthorize=authorized)
    lease = a.claim(p, scope, row["id"], "orchestrator", reauthorize=authorized)
    return row, lease


def reserve(env, row, lease, phase="PLAN", role="supervisor", key=None, **kwargs):
    _s, a, scope, p, _run_id, _m = env
    return a.reserve_call(
        p,
        scope,
        row["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        key or phase.lower(),
        role,
        phase,
        "a" * 64,
        "b" * 64,
        reauthorize=authorized,
        **kwargs,
    )


def finish(env, row, lease, call, **kwargs):
    _s, a, scope, p, _run_id, _m = env
    return a.finish_call(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE", "source_refs": []},
        {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 20, "cost_microusd": 0},
        reauthorize=authorized,
        **kwargs,
    )


def error(code, callback):
    with pytest.raises(DomainError) as raised:
        callback()
    assert raised.value.code == code


def expire(env, row, *, global_deadline=False):
    s, _a, scope, _p, _run_id, _m = env
    with Session(s.engine) as session, session.begin():
        item = session.scalar(
            select(AnalysisRun).where(*filters(AnalysisRun, scope), AnalysisRun.id == row["id"])
        )
        item.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        if global_deadline:
            item.deadline = item.lease_expires_at


def test_four_real_phase_records_separate_from_gate_and_actions(env):
    s, a, scope, p, run_id, _m = env
    before = s.get_run(p, scope, run_id)
    row, lease = started(env)
    for phase, role in (
        ("PLAN", "supervisor"),
        ("CONTEXT_PROPOSAL", "pm"),
        ("FINAL_REPORT", "pm"),
        ("SUMMARY", "supervisor"),
    ):
        call = reserve(env, row, lease, phase, role)
        assert call["dispatch_allowed"] is True
        assert 0 < call["timeout_seconds"] <= 60
        assert finish(env, row, lease, call)["publication_allowed"] is True
    completed = a.complete(
        p,
        scope,
        row["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE", "summary": "Synthetic evidence reviewed"},
        reauthorize=authorized,
    )
    assert completed["state"] == "COMPLETE"
    assert completed["calls_reserved"] == 4
    assert completed["tokens_reserved"] == 4 * (3500 + 800)
    assert completed["lease_token"] is None
    assert len(completed["calls"]) == 4
    assert all("lease_token" not in c for c in completed["calls"])
    after = s.get_run(p, scope, run_id)
    assert before == after
    events = a.events(p, scope, row["id"], reauthorize=authorized)
    assert len(events) == 11
    assert all(events[i]["prev_hash"] == events[i - 1]["event_hash"] for i in range(1, len(events)))


def test_concurrent_caller_scoped_idempotency(env):
    _s, a, scope, p, run_id, material = env

    def create(_):
        return a.start(p, scope, run_id, "simultaneous", material, reauthorize=authorized)["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(12)))
    assert len(set(ids)) == 1
    changed = {**material, "policy_revision": "new-policy"}
    error(
        "IDEMPOTENCY_CONFLICT",
        lambda: a.start(p, scope, run_id, "simultaneous", changed, reauthorize=authorized),
    )
    other = p.model_copy(update={"subject_id": "second-caller"})
    assert a.start(other, scope, run_id, "simultaneous", material, reauthorize=authorized)["id"] != ids[0]


def test_dispatch_intent_replay_never_redelivers_or_rereserves(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    assert reserve(env, row, lease)["dispatch_allowed"] is False
    assert finish(env, row, lease, call)["state"] == "VALIDATED"
    assert reserve(env, row, lease)["dispatch_allowed"] is False
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["calls_reserved"] == 1
    assert len(latest["calls"]) == 1
    error("ANALYSIS_CALL_CONFLICT", lambda: reserve(env, row, lease, input_tokens=3499))


def test_timeout_unknown_retains_slot_and_budget_across_runs(env):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    result = a.fail_unknown(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        code="PROVIDER_TIMEOUT",
    )
    assert result["state"] == "OUTCOME_UNKNOWN"
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["tokens_reserved"] == 4300
    assert latest["calls"][0]["usage_json"]["input_tokens"] is None
    assert reserve(env, row, lease)["dispatch_allowed"] is False
    second, second_lease = started(env, "second")
    error("PROVIDER_UNAVAILABLE", lambda: reserve(env, second, second_lease))
    with Session(s.engine) as session:
        slot = session.get(AnalysisProviderSlot, row["profile_id"])
        assert slot.call_id == call["id"]


def test_crash_recovery_unknown_blocks_late_response_and_new_dispatch(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    expire(env, row)
    recovered = a.recover(p, scope, row["id"])
    assert recovered["state"] == "OUTCOME_UNKNOWN"
    assert recovered["lease_epoch"] > lease["epoch"]
    assert recovered["calls"][0]["state"] == "OUTCOME_UNKNOWN"
    error("ANALYSIS_CALL_TERMINAL", lambda: finish(env, row, lease, call))
    error("ANALYSIS_TERMINAL", lambda: a.claim(p, scope, row["id"], "new-owner", reauthorize=authorized))


def test_recovery_before_dispatch_can_claim_without_extending_deadline(env):
    _s, a, scope, p, _run_id, _m = env
    row, first = started(env)
    expire(env, row)
    recovered = a.recover(p, scope, row["id"])
    assert recovered["state"] == "QUEUED"
    second = a.claim(p, scope, row["id"], "second-owner", reauthorize=authorized)
    assert second["epoch"] > first["epoch"]
    assert second["deadline"] == first["deadline"]
    error("STALE_ANALYSIS_LEASE", lambda: reserve(env, row, first))
    assert reserve(env, row, second)["dispatch_allowed"]


def test_stale_reply_retains_delivery_usage_and_rejects_publication(env):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    expire(env, row)
    result = finish(env, row, lease, call)
    assert result["publication_allowed"] is False
    assert result["error_code"] == "STALE_ANALYSIS_LEASE"
    stored = a.get(p, scope, row["id"], reauthorize=authorized)["calls"][0]
    assert stored["output_json"] is None
    assert stored["delivery_status"] == "DELIVERED"
    assert stored["usage_json"]["output_tokens"] == 20
    with Session(s.engine) as session:
        assert session.get(AnalysisProviderSlot, row["profile_id"]).call_id is None


def test_cancel_inflight_does_not_prove_stop_or_release_unknown(env):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    assert a.cancel(p, scope, row["id"])["state"] == "CANCELLED"
    with Session(s.engine) as session:
        assert session.get(AnalysisProviderSlot, row["profile_id"]).call_id == call["id"]
    result = finish(env, row, lease, call)
    assert result["state"] == "PUBLICATION_REJECTED"
    assert result["publication_allowed"] is False
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["state"] == "CANCELLED"
    assert latest["calls"][0]["usage_json"]["input_tokens"] == 100


def test_revoked_export_before_dispatch_sends_nothing(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)

    def denied(*_args):
        raise DomainError("EXPORT_DENIED", status=403)

    error(
        "EXPORT_DENIED",
        lambda: a.reserve_call(
            p,
            scope,
            row["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            "plan",
            "supervisor",
            "PLAN",
            "a" * 64,
            "b" * 64,
            reauthorize=denied,
        ),
    )
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["calls_reserved"] == 0
    assert latest["calls"] == []


def test_revocation_after_dispatch_keeps_usage_no_output_or_material_disclosure(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)

    def denied(*_args):
        raise DomainError("EXPORT_DENIED", status=403)

    result = a.finish_call(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE", "private_text": "should-not-publish"},
        {"state": "UNKNOWN"},
        reauthorize=denied,
    )
    assert result["publication_allowed"] is False
    assert "material_json" not in result and "output_json" not in result
    assert a.get(p, scope, row["id"], reauthorize=authorized)["calls"][0]["usage_state"] == "UNKNOWN"
    error("EXPORT_DENIED", lambda: a.get(p, scope, row["id"], reauthorize=denied))


def test_whole_parent_acl_revoked_after_dispatch_does_not_lose_usage(env):
    s, a, scope, p, run_id, _m = env
    # Synthetic source ACL introduced before snapshot is frozen at start.
    with Session(s.engine) as session, session.begin():
        session.add(
            Document(
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
                project_id=scope.project_id,
                id="sop",
                current_version="v1",
                roles=["pm"],
                classification=0,
                deleted=False,
            )
        )
        parent = session.scalar(select(Run).where(*filters(Run, scope), Run.id == run_id))
        parent.snapshot_json = {**parent.snapshot_json, "documents": [{"id": "sop"}]}
    row, lease = started(env)
    call = reserve(env, row, lease)
    with Session(s.engine) as session, session.begin():
        session.get(Document, {**scope.model_dump(), "id": "sop"}).roles = ["restricted"]
    result = finish(env, row, lease, call)
    assert result["publication_allowed"] is False
    assert result["error_code"] == "NOT_FOUND"
    with Session(s.engine) as session:
        stored = session.get(AnalysisCall, {**scope.model_dump(), "id": call["id"]})
        assert stored.delivery_status == "DELIVERED"
        assert stored.usage_json["input_tokens"] == 100
        assert stored.output_json is None
    error("NOT_FOUND", lambda: a.get(p, scope, row["id"], reauthorize=authorized))


@pytest.mark.parametrize("which", ["tenant_id", "workspace_id", "project_id"])
def test_cross_scope_read_and_full_scope_fk(env, which):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    other = scope.model_copy(update={which: "other"})
    broad = p.model_copy(update={"scopes": (scope, other)})
    error("NOT_FOUND", lambda: a.get(broad, other, row["id"], reauthorize=authorized))
    with pytest.raises(IntegrityError), Session(s.engine) as session, session.begin():
        item = session.get(AnalysisCall, {**scope.model_dump(), "id": call["id"]})
        setattr(item, which, "other")
        session.flush()


def test_peer_caller_cannot_mutate_or_steal_lease(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    peer = p.model_copy(update={"subject_id": "peer"})
    assert a.get(peer, scope, row["id"], reauthorize=authorized)["lease_token"] is None
    error("NOT_FOUND", lambda: a.cancel(peer, scope, row["id"]))
    error("NOT_FOUND", lambda: a.claim(peer, scope, row["id"], "peer-owner", reauthorize=authorized))
    call = reserve(env, row, lease)
    error(
        "STALE_ANALYSIS_CALL",
        lambda: a.finish_call(
            p,
            scope,
            row["id"],
            call["id"],
            "wrong-token",
            lease["epoch"],
            lease["owner"],
            {},
            None,
            reauthorize=authorized,
        ),
    )


def test_output_invalid_retains_usage_and_never_completes(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    result = finish(env, row, lease, call, output_valid=False)
    assert result["state"] == "OUTPUT_INVALID" and result["output_json"] is None
    assert result["usage_json"]["input_tokens"] == 100
    assert a.get(p, scope, row["id"], reauthorize=authorized)["state"] == "FAILED"
    error("ANALYSIS_TERMINAL", lambda: reserve(env, row, lease, "CONTEXT_PROPOSAL", "pm"))


def test_provider_claimed_token_overrun_is_invalid_and_cost_not_erased(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    result = a.finish_call(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE"},
        {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 801, "cost_microusd": 17},
        reauthorize=authorized,
    )
    assert result["state"] == "OUTPUT_INVALID"
    assert result["usage_json"]["cost_microusd"] == 17


def test_rejected_before_dispatch_releases_slot_without_fake_delivered_result(env):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    result = a.finish_not_dispatched(
        p, scope, row["id"], call["id"], lease["token"], lease["epoch"], lease["owner"]
    )
    assert result["delivery_status"] == "NOT_DISPATCHED"
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["tokens_reserved"] == 0 and latest["calls_reserved"] == 1
    assert latest["calls"][0]["usage_json"]["input_tokens"] is None
    with Session(s.engine) as session:
        assert session.get(AnalysisProviderSlot, row["profile_id"]).call_id is None
    assert reserve(env, row, lease)["dispatch_allowed"] is False


def test_abort_tool_step_preserves_validated_call_without_false_cancel(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    finish(env, row, lease, call)
    result = a.abort(
        p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"], code="TOOL_CONTEXT_DENIED"
    )
    assert result["state"] == "FAILED"
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["cancel_requested_at"] is None
    assert latest["calls"][0]["state"] == "VALIDATED"


def test_abort_active_call_becomes_unknown(env):
    s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    assert (
        a.abort(p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"], code="WORKER_FAILED")[
            "state"
        ]
        == "OUTCOME_UNKNOWN"
    )
    with Session(s.engine) as session:
        assert session.get(AnalysisProviderSlot, row["profile_id"]).call_id == call["id"]


def test_parent_snapshot_change_blocks_publication(env):
    s, _a, scope, _p, run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    with Session(s.engine) as session, session.begin():
        session.get(Run, {**scope.model_dump(), "id": run_id}).snapshot_hash = "f" * 64
    result = finish(env, row, lease, call)
    assert result["publication_allowed"] is False
    assert result["error_code"] == "ANALYSIS_MATERIAL_CHANGED"


def test_additional_call_sources_reauthorized_on_get_events_and_completion(env):
    _s, a, scope, p, _run_id, material = env
    row, lease = started(env)
    seen = []
    enriched = {**material, "source_snapshot": {"references": ["private-pm-source"]}}
    call = reserve(env, row, lease, material=enriched)
    finish(env, row, lease, call)

    def check(_session, _p, _scope, m):
        seen.append(m["source_snapshot"])
        if "private-pm-source" in m["source_snapshot"].get("references", []):
            raise DomainError("NOT_FOUND", status=404)

    error("NOT_FOUND", lambda: a.get(p, scope, row["id"], reauthorize=check))
    error("NOT_FOUND", lambda: a.events(p, scope, row["id"], reauthorize=check))
    assert len(seen) == 4


@pytest.mark.parametrize(
    "change,code",
    [
        ({"max_calls": 5}, "INVALID_ANALYSIS_LIMIT"),
        ({"max_output_tokens": 801}, "INVALID_ANALYSIS_LIMIT"),
        ({"timeout_seconds": 181}, "INVALID_ANALYSIS_LIMIT"),
        ({"provider_billing": "PAID"}, "PAID_PROFILE_DISABLED"),
    ],
)
def test_profile_limits_are_bound_not_user_expanded(env, change, code):
    _s, a, scope, p, run_id, material = env
    error(code, lambda: a.start(p, scope, run_id, "invalid", {**material, **change}, reauthorize=authorized))


def test_complete_cannot_skip_required_roles_and_phases(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    error(
        "ANALYSIS_INCOMPLETE",
        lambda: a.complete(
            p,
            scope,
            row["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            {"status": "COMPLETE"},
            reauthorize=authorized,
        ),
    )
    error("INVALID_ANALYSIS_CALL", lambda: reserve(env, row, lease, "FINAL_REPORT", "hardware"))
    reserve(env, row, lease)
    error("ANALYSIS_CALL_CONFLICT", lambda: reserve(env, row, lease, key="second-plan"))


def test_db_deadline_and_expired_export_prevent_dispatch(env):
    _s, a, scope, p, _run_id, material = env
    row, lease = started(env)
    expired = {**material, "export_grant_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()}
    error("ANALYSIS_DEADLINE", lambda: reserve(env, row, lease, material=expired))
    expire(env, row, global_deadline=True)
    error("STALE_ANALYSIS_LEASE", lambda: reserve(env, row, lease))
    assert a.recover(p, scope, row["id"])["state"] == "INCOMPLETE"


def test_authorization_callback_required_and_must_not_return_unawaited_value(env):
    _s, a, scope, p, run_id, material = env
    error("REAUTHORIZATION_REQUIRED", lambda: a.start(p, scope, run_id, "nohook", material, reauthorize=None))
    error(
        "INVALID_AUTHORIZATION_CALLBACK",
        lambda: a.start(p, scope, run_id, "badhook", material, reauthorize=lambda *_args: True),
    )


def test_concurrent_reserve_persists_one_dispatch_authority(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    with ThreadPoolExecutor(max_workers=8) as pool:
        calls = list(pool.map(lambda _: reserve(env, row, lease), range(12)))
    assert len({item["id"] for item in calls}) == 1
    assert sum(item["dispatch_allowed"] for item in calls) == 1
    assert a.get(p, scope, row["id"], reauthorize=authorized)["calls_reserved"] == 1


def test_concurrent_different_analyses_share_one_fixed_profile_slot(env):
    _s, _a, _scope, _p, _run_id, _m = env
    first = started(env, "first-global")
    second = started(env, "second-global")

    def attempt(pair):
        try:
            return reserve(env, *pair)["dispatch_allowed"]
        except DomainError as raised:
            return raised.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first, second)))
    assert results.count(True) == 1
    assert results.count("PROVIDER_UNAVAILABLE") == 1


def test_same_scope_cross_run_call_link_rejected_by_database(env):
    s, _a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    other_run = s.submit(p, RunRequest(scope=scope, fixture_id="aurora-unknown"), "other-hardware")
    with pytest.raises(IntegrityError), Session(s.engine) as session, session.begin():
        session.get(AnalysisCall, {**scope.model_dump(), "id": call["id"]}).run_id = other_run["id"]
        session.flush()


def test_pinned_revision_mismatch_records_delivery_and_usage_but_invalidates_output(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    result = a.finish_call(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE"},
        {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 20},
        provider_metadata={"actual_model_revision": "f" * 64, "finish_reason": "stop", "timing_ms": 1200},
        reauthorize=authorized,
    )
    assert result["state"] == "OUTPUT_INVALID"
    assert result["delivery_status"] == "DELIVERED"
    assert result["provider_metadata_json"]["actual_model_revision"] == "f" * 64
    assert result["usage_json"]["output_tokens"] == 20


def test_final_publication_rechecks_all_role_sources(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    for phase, role in (
        ("PLAN", "supervisor"),
        ("CONTEXT_PROPOSAL", "pm"),
        ("FINAL_REPORT", "pm"),
        ("SUMMARY", "supervisor"),
    ):
        finish(env, row, lease, reserve(env, row, lease, phase, role))

    def revoked(*_args):
        raise DomainError("SOURCE_REVOKED", status=403)

    error(
        "SOURCE_REVOKED",
        lambda: a.complete(
            p,
            scope,
            row["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            {"status": "COMPLETE"},
            reauthorize=revoked,
        ),
    )
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["summary_json"] is None
    assert latest["state"] == "RUNNING"
    assert all(item["state"] == "VALIDATED" for item in latest["calls"])


def test_incomplete_pm_cannot_be_upgraded_by_supervisor_complete(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    for phase, role in (
        ("PLAN", "supervisor"),
        ("CONTEXT_PROPOSAL", "pm"),
        ("FINAL_REPORT", "pm"),
        ("SUMMARY", "supervisor"),
    ):
        call = reserve(env, row, lease, phase, role)
        a.finish_call(
            p,
            scope,
            row["id"],
            call["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            {"status": "INCOMPLETE" if phase == "FINAL_REPORT" else "COMPLETE"},
            {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 20},
            reauthorize=authorized,
        )
    latest = a.complete(
        p,
        scope,
        row["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        {"status": "COMPLETE"},
        reauthorize=authorized,
    )
    assert latest["state"] == "INCOMPLETE"


def test_dispatch_phase_order_cannot_skip_supervisor_plan(env):
    _s, _a, _scope, _p, _run_id, _m = env
    row, lease = started(env)
    error("ANALYSIS_PHASE_ORDER", lambda: reserve(env, row, lease, "SUMMARY", "supervisor"))


def test_expired_owned_lease_can_abort_without_publication_or_renewal(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    expire(env, row, global_deadline=True)
    result = a.abort(
        p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"], code="ANALYSIS_DEADLINE"
    )
    assert result["state"] == "INCOMPLETE"
    latest = a.get(p, scope, row["id"], reauthorize=authorized)
    assert latest["lease_token"] is None and latest["summary_json"] is None
    assert latest["calls"] == []


def test_abort_terminal_unknown_is_idempotent_and_preserves_failure(env):
    _s, a, scope, p, _run_id, _m = env
    row, lease = started(env)
    call = reserve(env, row, lease)
    a.fail_unknown(
        p,
        scope,
        row["id"],
        call["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        code="PROVIDER_TIMEOUT",
    )
    before = a.events(p, scope, row["id"], reauthorize=authorized)
    assert (
        a.abort(p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"], code="FOLLOWUP_ERROR")[
            "state"
        ]
        == "OUTCOME_UNKNOWN"
    )
    assert a.events(p, scope, row["id"], reauthorize=authorized) == before


def test_start_replay_does_not_bypass_private_call_source_acl(env):
    _s, a, scope, p, run_id, material = env
    row, lease = started(env)
    call = reserve(
        env, row, lease, material={**material, "source_snapshot": {"references": ["private-pm-source"]}}
    )
    finish(env, row, lease, call)

    def revoked(_session, _p, _scope, m):
        if "private-pm-source" in m["source_snapshot"].get("references", []):
            raise DomainError("NOT_FOUND", status=404)

    error("NOT_FOUND", lambda: a.start(p, scope, run_id, "analysis", material, reauthorize=revoked))


def test_recover_never_exposes_cached_call_material_or_validated_output(env):
    _s, a, scope, p, _run_id, material = env
    row, lease = started(env)
    call = reserve(
        env, row, lease, material={**material, "source_snapshot": {"references": ["private-pm-source"]}}
    )
    finish(env, row, lease, call)
    expire(env, row)
    recovered = a.recover(p, scope, row["id"])
    assert set(recovered) == {"id", "state", "lease_epoch", "calls"}
    assert "private-pm-source" not in str(recovered)
    assert "output_json" not in str(recovered)


def test_locked_load_refreshes_orm_cache_after_claim_interleaving(env, monkeypatch):
    """TEST_DRIVER DB-only update reproduces identity-map READ COMMITTED hazard.

    SQLite serializes real writers; this models the committed change visible to
    a PG waiter after it hydrates an unlocked entity and then waits for parent.
    """
    from sqlalchemy import update

    _s, a, scope, p, _run_id, _m = env
    row = a.start(p, scope, env[4], "interleave", env[5], reauthorize=authorized)
    original = a._parent

    def claim_a_committed(session, principal, target_scope, run_id, **kwargs):
        parent = original(session, principal, target_scope, run_id, **kwargs)
        session.execute(
            update(AnalysisRun)
            .where(*filters(AnalysisRun, scope), AnalysisRun.id == row["id"])
            .values(
                state="RUNNING",
                lease_epoch=1,
                lease_owner="claim-a",
                lease_token="token-a",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
            )
            .execution_options(synchronize_session=False)
        )
        return parent

    monkeypatch.setattr(a, "_parent", claim_a_committed)
    error("ANALYSIS_LEASE_BUSY", lambda: a.claim(p, scope, row["id"], "claim-b", reauthorize=authorized))


def test_true_tool_receipt_audit_is_scoped_and_reauthorized_without_model_claims(env):
    _s, a, scope, p, _run_id, material = env
    row, lease = started(env)
    finish(env, row, lease, reserve(env, row, lease))
    finish(env, row, lease, reserve(env, row, lease, "CONTEXT_PROPOSAL", "pm"))
    contextual = {**material, "source_snapshot": {"sources": [{"ref": "opaque-pm-source"}]}}
    receipt = {
        "proposal_hash": "e" * 64,
        "receipts": [
            {
                "tool": "knowledge.search",
                "transport": "IN_PROCESS",
                "role": "pm",
                "query_hash": "f" * 64,
                "top_k": 5,
                "result_count": 1,
                "source_refs": ["opaque-pm-source"],
            }
        ],
    }
    recorded = a.record_tool(
        p,
        scope,
        row["id"],
        lease["token"],
        lease["epoch"],
        lease["owner"],
        receipt,
        material=contextual,
        reauthorize=authorized,
    )
    assert recorded["kind"] == "ANALYSIS_TOOL_READ"
    event = a.events(p, scope, row["id"], reauthorize=authorized)[-1]
    from forgeflow.contracts import digest

    assert event["payload_json"]["context_material_hash"] == digest(
        contextual
        | {
            "classification": 0,
            "max_calls": 4,
            "max_input_tokens": 3500,
            "max_output_tokens": 800,
            "timeout_seconds": 180,
            "provider_billing": "LOCAL_NO_BILLING",
        }
    )

    def revoked(_session, _p, _scope, m):
        if m["source_snapshot"].get("sources"):
            raise DomainError("NOT_FOUND", status=404)

    error("NOT_FOUND", lambda: a.events(p, scope, row["id"], reauthorize=revoked))
    error(
        "TOOL_ROUND_ALREADY_RECORDED",
        lambda: a.record_tool(
            p,
            scope,
            row["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            receipt,
            material=contextual,
            reauthorize=authorized,
        ),
    )
    assert a.get(p, scope, row["id"], reauthorize=authorized)["calls_reserved"] == 2


def test_tool_receipt_cannot_claim_mcp_or_sandbox_execution(env):
    _s, a, scope, p, _run_id, material = env
    row, lease = started(env)
    receipt = {
        "proposal_hash": "e" * 64,
        "receipts": [
            {
                "tool": "sandbox.execute",
                "transport": "MCP",
                "role": "hardware",
                "query_hash": "f" * 64,
                "top_k": 1,
                "result_count": 0,
                "source_refs": [],
            }
        ],
    }
    error(
        "INVALID_TOOL_RECEIPT",
        lambda: a.record_tool(
            p,
            scope,
            row["id"],
            lease["token"],
            lease["epoch"],
            lease["owner"],
            receipt,
            material=material,
            reauthorize=authorized,
        ),
    )
