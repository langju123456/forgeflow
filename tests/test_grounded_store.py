"""Real persistence and adversarial scoped grounding checks; no model calls."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeflow.analysis_store import AnalysisCall, AnalysisRun, AnalysisStore
from forgeflow.contracts import DomainError, Principal, RunRequest, Scope, digest
from forgeflow.grounded_contracts import QUESTION, render
from forgeflow.grounded_store import GroundedStore, GroundingRecord, RawProviderReply
from forgeflow.store import Base, Store, filters


def authorized(_session, _principal, _scope, _material):
    """TEST_DRIVER only: this test's synthetic fixture is export-authorized."""


def catalog():
    return {
        "observations": [
            {
                "id": f"c_{index:016x}", "source_ref": "src_" + "a" * 32,
                "metric": name, "value": value, "check_state": "SATISFIED",
                "evidence_state": "MEASURED", "authority": "SYNTHETIC_CHECK",
            }
            for index, (name, value) in enumerate(
                (("boot_cycles", 100), ("boot_failures", 0), ("bom_revision", "A3"),
                 ("ci_smoke", True), ("thermal_peak_c", 65))
            )
        ],
        "concerns": [{"id": "c_ffffffffffffffff", "source_ref": "src_" + "a" * 32,
                      "kind": "HUMAN_APPROVAL_REQUIRED"}],
    }


@pytest.fixture
def env(tmp_path):
    # Intentionally SQLite. Root may run a separately authorized PostgreSQL set.
    store = Store(f"sqlite:///{tmp_path / 'grounding.sqlite'}")
    # Reconstruct the existing test schema without this module's new table;
    # Store.create_schema includes all classes already imported into Base.
    Base.metadata.create_all(store.engine, tables=[table for table in Base.metadata.sorted_tables
                                                  if table.name not in {"grounding_records", "raw_provider_replies"}])
    analysis = AnalysisStore(store)
    analysis.create_schema()
    before = set(inspect(store.engine).get_table_names())
    ledger = GroundedStore(analysis)
    ledger.create_schema()
    assert set(inspect(store.engine).get_table_names()) - before == {"grounding_records", "raw_provider_replies"}
    scope = Scope(tenant_id="t" + uuid4().hex[:12], workspace_id="w", project_id="aurora")
    p = Principal(subject_id="controller", kind="user", scopes=(scope,), clearance=0,
                  roles=frozenset({"submitter", "pm", "hardware"}))
    worker = p.model_copy(update={"kind": "service", "subject_id": "hardware-worker",
                                  "roles": frozenset({"hardware", "worker"})})
    run = store.submit(p, RunRequest(scope=scope, fixture_id="aurora-pass"), "hardware")
    lease = store.claim(worker, scope, run["id"], "hardware-test-driver")
    store.complete_check(worker, scope, run["id"], lease["token"], lease["epoch"],
                         {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": []},
                         {"status": "PASS", "evidence": []},
                         {"classification": 0, "documents": [], "input_hash": "a" * 64})
    material = {"profile_id": "test-" + uuid4().hex, "profile_revision": "b" * 64,
                "policy_revision": "s2b-grounded-test", "source_snapshot": {"references": []},
                "claim_catalog_hash": digest(catalog())}
    return store, analysis, ledger, scope, p, run["id"], material


def start(env, key="grounded"):
    _s, analysis, _ledger, scope, p, run_id, material = env
    row = analysis.start(p, scope, run_id, key, material, reauthorize=authorized)
    lease = analysis.claim(p, scope, row["id"], "controller-worker", reauthorize=authorized)
    return row, lease


def reserve(env, row, lease, phase="PLAN", role="supervisor"):
    _s, analysis, _ledger, scope, p, _run_id, _material = env
    return analysis.reserve_call(p, scope, row["id"], lease["token"], lease["epoch"],
                                 lease["owner"], phase.lower(), role, phase, "c" * 64, "d" * 64,
                                 reauthorize=authorized)


def finish(env, row, lease, call, output, valid=True):
    _s, analysis, _ledger, scope, p, _run_id, _material = env
    return analysis.finish_call(
        p, scope, row["id"], call["id"], lease["token"], lease["epoch"], lease["owner"],
        output, {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 30,
                 "cost_microusd": None}, output_valid=valid,
        provider_metadata={"transport": "TEST_DRIVER", "actual_model_revision": "b" * 64,
                           "finish_reason": "stop"},
        reauthorize=authorized,
    )


def plan():
    return {"assignment": "pm", "task_question": QUESTION, "source_refs": ["src_" + "a" * 32]}


def record(raw=None, *, structure=True, semantic=True, rendered=None, codes=None):
    return {
        "raw_reply": plan() if raw is None else raw, "structure_valid": structure,
        "semantic_valid": semantic, "codes": codes or [], "claim_catalog": catalog(),
        "rendered_report": rendered,
        "provider_provenance": {"transport": "TEST_DRIVER", "actual_model_revision": "b" * 64,
                                "finish_reason": "stop", "status": "SUCCEEDED", "dispatched": True,
                                "completion_confirmed": True, "input_hash": "c" * 64},
    }


def append(env, row, call, payload, **kwargs):
    _s, _a, ledger, scope, p, _run, _m = env
    return ledger.append(p, scope, row["id"], call["id"], payload,
                         reauthorize=kwargs.get("reauthorize", authorized))


def expect(code, operation):
    with pytest.raises(DomainError) as raised:
        operation()
    assert raised.value.code == code


def ready(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    finish(env, row, lease, call, plan())
    return row, call


def test_exact_raw_and_immutable_same_payload_replay(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, call = ready(env)
    raw = '{"assignment":"pm", "task_question":"' + QUESTION + '","source_refs":["src_' + "a" * 32 + '"]}'
    payload = record(raw)
    first = append(env, row, call, payload)
    repeated = append(env, row, call, deepcopy(payload))
    assert first["id"] == repeated["id"]
    assert first["replayed"] is False and repeated["replayed"] is True
    assert first["raw_reply"] == raw
    assert first["claim_catalog_hash"] == digest(catalog())
    assert len(ledger.list(p, scope, row["id"], reauthorize=authorized)) == 1
    changed = {**payload, "codes": ["CHANGED_DIAGNOSTIC"]}
    expect("GROUNDING_CONFLICT", lambda: append(env, row, call, changed))


def test_concurrent_append_single_immutable_record(env):
    row, call = ready(env)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: append(env, row, call, record()), range(12)))
    assert len({result["id"] for result in results}) == 1
    assert sum(not result["replayed"] for result in results) == 1


def test_malformed_provider_output_kept_without_semantic_or_report_acceptance(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    finish(env, row, lease, call, {}, valid=False)
    raw = '{"assignment":'
    saved = append(env, row, call, record(raw, structure=False, semantic=None,
                                        codes=["INVALID_MODEL_OUTPUT"]))
    assert saved["raw_reply"] == raw
    assert saved["structure_valid"] is False and saved["semantic_valid"] is None
    assert saved["rendered_report"] is None


def test_structurally_valid_but_unfounded_claim_is_not_an_accepted_report(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    wrong = {**plan(), "task_question": "Invent missing evidence"}
    finish(env, row, lease, call, wrong, valid=False)
    expect("GROUNDING_PUBLICATION_REJECTED", lambda: append(env, row, call, record(wrong)))
    saved = append(env, row, call, record(wrong, semantic=False,
                                        codes=["UNGROUNDED_DELEGATED_QUESTION"]))
    assert saved["structure_valid"] is True and saved["semantic_valid"] is False
    expect("GROUNDING_CONFLICT", lambda: append(env, row, call, record(wrong)))


def test_report_rendering_has_exact_trusted_text_and_catalog(env):
    row, lease = start(env)
    for phase, role, output in (("PLAN", "supervisor", plan()),
                                ("CONTEXT_PROPOSAL", "pm", {"requests": []})):
        call = reserve(env, row, lease, phase, role)
        finish(env, row, lease, call, output)
    call = reserve(env, row, lease, "FINAL_REPORT", "pm")
    output = {"status": "COMPLETE", "observations": [o["id"] for o in catalog()["observations"]],
              "concerns": [c["id"] for c in catalog()["concerns"]],
              "recommendation": "HUMAN_REVIEW_REQUIRED"}
    finish(env, row, lease, call, output)
    expected = render(catalog(), output)
    altered = {**expected, "summary": "The model approved production deployment"}
    expect("GROUNDING_RENDER_MISMATCH", lambda: append(env, row, call, record(output, rendered=altered)))
    saved = append(env, row, call, record(output, rendered=expected))
    assert saved["rendered_report"] == expected


def report_ready(env):
    row, lease = start(env)
    for phase, role, output in (("PLAN", "supervisor", plan()),
                                ("CONTEXT_PROPOSAL", "pm", {"requests": []})):
        call = reserve(env, row, lease, phase, role)
        finish(env, row, lease, call, output)
    call = reserve(env, row, lease, "FINAL_REPORT", "pm")
    output = {"status": "COMPLETE", "observations": [o["id"] for o in catalog()["observations"]],
              "concerns": [c["id"] for c in catalog()["concerns"]],
              "recommendation": "HUMAN_REVIEW_REQUIRED"}
    finish(env, row, lease, call, output)
    return row, call, output


def test_cancel_after_call_validation_blocks_fresh_render_but_preserves_raw(env):
    _store, analysis, _ledger, scope, p, _r, _m = env
    row, call, output = report_ready(env)
    assert analysis.cancel(p, scope, row["id"])["state"] == "CANCELLED"
    expect("GROUNDING_PUBLICATION_REJECTED", lambda: append(
        env, row, call, record(output, rendered=render(catalog(), output))))
    saved = append(env, row, call, record(output))
    assert saved["semantic_valid"] is True
    assert saved["raw_reply"] == output and saved["rendered_report"] is None


def test_previously_published_render_replays_as_historical_after_cancel(env):
    _store, analysis, _ledger, scope, p, _r, _m = env
    row, call, output = report_ready(env)
    payload = record(output, rendered=render(catalog(), output))
    published = append(env, row, call, payload)
    analysis.cancel(p, scope, row["id"])
    replayed = append(env, row, call, payload)
    assert replayed["id"] == published["id"] and replayed["replayed"] is True


def test_unknown_completion_preserves_original_provider_error_disposition(env):
    _store, analysis, _ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    analysis.fail_unknown(p, scope, row["id"], call["id"], lease["token"], lease["epoch"],
                          lease["owner"], code="PROVIDER_HTTP_500")
    payload = record(structure=False, semantic=None, codes=["PROVIDER_OUTPUT_UNAVAILABLE"])
    payload["raw_reply"] = None
    payload["provider_provenance"].update(status="PROVIDER_ERROR", completion_confirmed=False,
                                           actual_model_revision=None, finish_reason=None)
    saved = append(env, row, call, payload)
    assert saved["provider_provenance"]["status"] == "PROVIDER_ERROR"
    assert saved["semantic_valid"] is None and saved["rendered_report"] is None


@pytest.mark.parametrize("change,code", [
    (lambda payload: payload["claim_catalog"]["observations"][0].update(value=999), "GROUNDING_CATALOG_MISMATCH"),
    (lambda payload: payload["provider_provenance"].update(input_hash="e" * 64), "GROUNDING_INPUT_MISMATCH"),
    (lambda payload: payload["raw_reply"].update(source_refs=["src_" + "f" * 32]), "GROUNDING_OUTPUT_MISMATCH"),
])
def test_predispatch_catalog_input_and_raw_output_binding(env, change, code):
    row, call = ready(env)
    payload = record()
    change(payload)
    expect(code, lambda: append(env, row, call, payload))


@pytest.mark.parametrize("change", [
    {"transport": "OLLAMA_LOCAL_HTTP"}, {"actual_model_revision": "e" * 64},
    {"finish_reason": "length"}, {"status": "DENIED"}, {"dispatched": False},
    {"completion_confirmed": False},
])
def test_provider_facts_cannot_be_relabelled_as_real_or_successful(env, change):
    row, call = ready(env)
    payload = record()
    payload["provider_provenance"].update(change)
    expect("GROUNDING_PROVENANCE_MISMATCH", lambda: append(env, row, call, payload))


def test_consistently_committed_length_finish_is_still_not_accepted(env):
    store, analysis, _ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    analysis.finish_call(
        p, scope, row["id"], call["id"], lease["token"], lease["epoch"], lease["owner"], plan(),
        {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 30, "cost_microusd": None},
        provider_metadata={"transport": "TEST_DRIVER", "actual_model_revision": "b" * 64,
                           "finish_reason": "length"}, reauthorize=authorized,
    )
    assert store.engine is analysis.engine
    payload = record()
    payload["provider_provenance"]["finish_reason"] = "length"
    expect("GROUNDING_PROVENANCE_MISMATCH", lambda: append(env, row, call, payload))


def test_reported_structure_result_cannot_contradict_exact_raw(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    finish(env, row, lease, call, {}, valid=False)
    expect("GROUNDING_VERDICT_MISMATCH", lambda: append(env, row, call, record(
        '{"assignment":', structure=True, semantic=None, codes=["PROVIDER_OUTPUT_UNAVAILABLE"])))


def test_same_scope_call_from_different_analysis_is_unavailable(env):
    row, call = ready(env)
    other, _lease = start(env, "other")
    expect("NOT_FOUND", lambda: append(env, other, call, record()))
    assert row["id"] != other["id"]


def test_cross_tenant_even_multiscope_identity_cannot_read_or_write(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, call = ready(env)
    append(env, row, call, record())
    other = scope.model_copy(update={"tenant_id": "other-tenant"})
    multiscope = p.model_copy(update={"scopes": (scope, other)})
    expect("NOT_FOUND", lambda: ledger.get(multiscope, other, row["id"], call["id"], reauthorize=authorized))
    expect("NOT_FOUND", lambda: ledger.list(multiscope, other, row["id"], reauthorize=authorized))
    expect("NOT_FOUND", lambda: ledger.append(multiscope, other, row["id"], call["id"], record(), reauthorize=authorized))


def test_model_agent_cannot_self_grade_and_other_user_cannot_append(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, call = ready(env)
    agent = p.model_copy(update={"kind": "agent"})
    expect("GROUNDING_AGENT_WRITE_DENIED", lambda: ledger.append(agent, scope, row["id"], call["id"], record(), reauthorize=authorized))
    other = p.model_copy(update={"subject_id": "another-user"})
    expect("NOT_FOUND", lambda: ledger.append(other, scope, row["id"], call["id"], record(), reauthorize=authorized))


def test_revoked_source_blocks_whole_record_and_duplicate_replay(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, call = ready(env)
    append(env, row, call, record())

    def revoked(_session, _p, _scope, _material):
        raise DomainError("ANALYSIS_SOURCE_CHANGED", status=409)

    for operation in (
        lambda: ledger.get(p, scope, row["id"], call["id"], reauthorize=revoked),
        lambda: ledger.list(p, scope, row["id"], reauthorize=revoked),
        lambda: append(env, row, call, record(), reauthorize=revoked),
    ):
        expect("ANALYSIS_SOURCE_CHANGED", operation)


def test_reauthorization_repeated_inside_publication_transaction(env):
    row, call = ready(env)
    sessions = []

    def revoke_between_transactions(session, _p, _scope, _material):
        if not sessions:
            sessions.append(session)
        elif session is not sessions[0]:
            raise DomainError("ANALYSIS_SOURCE_CHANGED", status=409)

    expect("ANALYSIS_SOURCE_CHANGED", lambda: append(env, row, call, record(), reauthorize=revoke_between_transactions))
    _s, _a, ledger, scope, p, _r, _m = env
    assert ledger.list(p, scope, row["id"], reauthorize=authorized) == []


def test_tampered_stored_raw_denies_read(env):
    store, _a, ledger, scope, p, _r, _m = env
    row, call = ready(env)
    saved = append(env, row, call, record())
    with Session(store.engine) as session, session.begin():
        item = session.scalar(select(GroundingRecord).where(*filters(GroundingRecord, scope), GroundingRecord.id == saved["id"]))
        item.raw_reply = {"unapproved": True}
    expect("GROUNDING_RECORD_TAMPERED", lambda: ledger.get(p, scope, row["id"], call["id"], reauthorize=authorized))


def test_scoped_database_fk_blocks_cross_analysis_reference(env):
    store, _a, _ledger, scope, _p, _r, _m = env
    row, call = ready(env)
    other, _lease = start(env, "other")
    saved = append(env, row, call, record())
    bad = {k: v for k, v in saved.items() if k not in {"replayed", "created_at", "raw_journal_hash"}}
    bad.update(id=str(uuid4()), analysis_id=other["id"])
    with pytest.raises(IntegrityError), Session(store.engine) as session, session.begin():
        session.add(GroundingRecord(**bad, created_at=store._now(session)))
        session.flush()


def test_unfinished_call_cannot_be_recorded_as_grounded(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    expect("GROUNDING_CALL_UNFINISHED", lambda: append(env, row, call, record()))


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(raw_reply="x" * (128 * 1024 + 1)),
    lambda p: p.update(semantic_valid="true"),
    lambda p: p.update(structure_valid=False, semantic_valid=True),
    lambda p: p.update(codes=["not-a-safe-code"]),
    lambda p: p["provider_provenance"].update(messages=[{"role": "system", "content": "secret prompt"}]),
    lambda p: p["provider_provenance"].update(dispatched=1),
    lambda p: p["claim_catalog"].update(nonfinite=float("nan")),
])
def test_bounded_json_and_provenance_never_accept_secret_prompt_fields(env, mutate):
    row, call = ready(env)
    payload = record()
    mutate(payload)
    with pytest.raises(DomainError):
        append(env, row, call, payload)


def test_table_is_additive_and_plain_store_constructor_supported(env):
    store, _a, _ledger, _scope, _p, _r, _m = env
    before = set(inspect(store.engine).get_table_names())
    alternate = GroundedStore(store)
    alternate.create_schema()
    assert set(inspect(store.engine).get_table_names()) == before
    with Session(store.engine) as session:
        assert session.scalar(select(AnalysisCall.id)) is None


def provider_reply(raw=None):
    return {
        **{k: v for k, v in record()["provider_provenance"].items() if k != "input_hash"},
        "content": plan(), "content_utf8": raw,
        "usage": {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 30,
                  "cost_microusd": None},
        "elapsed_seconds": 1.25, "error_code": None,
    }


def capture(env, row, call, reply, **kwargs):
    _s, _a, ledger, scope, p, _run, _material = env
    return ledger.capture(p, scope, row["id"], call["id"], reply, kwargs.get("input_hash", "c" * 64),
                          kwargs.get("catalog", catalog()), reauthorize=kwargs.get("reauthorize", authorized))


def test_capture_commits_exact_reply_before_grading_and_survives_crash_recovery(env):
    store, analysis, ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    raw = '{"assignment":"pm","task_question":"' + QUESTION + '","source_refs":["src_' + "a" * 32 + '"]}'
    reply = provider_reply(raw)
    saved = capture(env, row, call, reply)
    assert saved["provider_reply"] == reply and saved["provider_reply"]["content_utf8"] == raw
    assert analysis.get(p, scope, row["id"], reauthorize=authorized)["calls"][0]["state"] == "DISPATCHING"
    assert ledger.list(p, scope, row["id"], reauthorize=authorized) == []
    # TEST_DRIVER crash: the worker vanishes after capture and before finish.
    with Session(store.engine) as session, session.begin():
        item = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope), AnalysisRun.id == row["id"]))
        item.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    recovered = analysis.recover(p, scope, row["id"])
    assert recovered["state"] == "OUTCOME_UNKNOWN"
    expect("ANALYSIS_CALL_TERMINAL", lambda: finish(env, row, lease, call, plan()))
    kept = ledger.raw(p, scope, row["id"], call["id"], reauthorize=authorized)
    assert kept["payload_hash"] == saved["payload_hash"]
    assert kept["provider_reply"]["completion_confirmed"] is True
    assert ledger.list(p, scope, row["id"], reauthorize=authorized) == []
    assert analysis.get(p, scope, row["id"], reauthorize=authorized)["calls"][0]["state"] == "OUTCOME_UNKNOWN"
    # The journal is evidence for manual reconciliation, never permission to
    # finish a fenced call, redispatch it, release its slot, or publish a report.


def test_grader_links_same_raw_journal_and_rejects_reconstructed_raw(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    raw = '{"assignment":"pm", "task_question":"' + QUESTION + '","source_refs":["src_' + "a" * 32 + '"]}'
    saved = capture(env, row, call, provider_reply(raw))
    finish(env, row, lease, call, plan())
    expect("GROUNDING_RAW_JOURNAL_MISMATCH", lambda: append(env, row, call, record()))
    graded = append(env, row, call, record(raw))
    assert graded["raw_journal_hash"] == saved["payload_hash"]
    assert ledger.get(p, scope, row["id"], call["id"], reauthorize=authorized)["raw_journal_hash"] == saved["payload_hash"]


def test_raw_capture_concurrent_replay_is_one_immutable_row(env):
    row, lease = start(env)
    call = reserve(env, row, lease)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: capture(env, row, call, provider_reply()), range(12)))
    assert len({item["id"] for item in results}) == 1
    assert sum(not item["replayed"] for item in results) == 1
    expect("RAW_PROVIDER_REPLY_CONFLICT", lambda: capture(env, row, call, {
        **provider_reply(), "elapsed_seconds": 3.0}))


@pytest.mark.parametrize("field", ["messages", "api_key", "structure_valid", "semantic_valid", "rendered_report"])
def test_raw_capture_does_not_accept_request_secrets_or_publication_grades(env, field):
    row, lease = start(env)
    call = reserve(env, row, lease)
    expect("INVALID_RAW_PROVIDER_REPLY", lambda: capture(env, row, call, {**provider_reply(), field: "forbidden"}))


@pytest.mark.parametrize("change", [
    {"usage": {"state": "CONFIRMED", "api_key": "forbidden"}},
    {"usage": {"state": "UNKNOWN", "input_tokens": {"api_key": "forbidden"}}},
    {"usage": {"state": "UNKNOWN", "output_tokens": True}},
    {"usage": {"state": "UNKNOWN", "cost_microusd": ["private"]}},
    {"usage": {"state": ["UNKNOWN"]}},
    {"transport": {"headers": {"Authorization": "forbidden"}}},
    {"dispatched": 1}, {"content_utf8": {"messages": []}},
])
def test_raw_metadata_never_accepts_nested_credentials_or_invalid_types(env, change):
    row, lease = start(env)
    call = reserve(env, row, lease)
    expect("INVALID_RAW_PROVIDER_REPLY", lambda: capture(env, row, call, {**provider_reply(), **change}))


def test_late_capture_cannot_poison_an_existing_graded_record(env):
    row, call = ready(env)
    append(env, row, call, record())
    expect("GROUNDING_RAW_JOURNAL_MISMATCH", lambda: capture(env, row, call, {
        **provider_reply(), "content": {"fabricated": True}}))


def test_raw_capture_exact_binding_and_cross_scope_reads(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    expect("RAW_PROVIDER_REPLY_BINDING_MISMATCH", lambda: capture(env, row, call, provider_reply(), input_hash="f" * 64))
    changed_catalog = deepcopy(catalog())
    changed_catalog["observations"][0]["value"] = 999
    expect("RAW_PROVIDER_REPLY_BINDING_MISMATCH", lambda: capture(env, row, call, provider_reply(), catalog=changed_catalog))
    capture(env, row, call, provider_reply())
    other = scope.model_copy(update={"tenant_id": "other"})
    both = p.model_copy(update={"scopes": (scope, other)})
    expect("NOT_FOUND", lambda: ledger.raw(both, other, row["id"], call["id"], reauthorize=authorized))
    expect("NOT_FOUND", lambda: ledger.raw_list(both, other, row["id"], reauthorize=authorized))


def test_raw_journal_revocation_and_preflight_race_fail_closed(env):
    _s, _a, ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    sessions = []

    def revoked_after_preflight(session, _p, _scope, _material):
        if not sessions:
            sessions.append(session)
        elif session is not sessions[0]:
            raise DomainError("ANALYSIS_SOURCE_CHANGED")

    expect("ANALYSIS_SOURCE_CHANGED", lambda: capture(env, row, call, provider_reply(), reauthorize=revoked_after_preflight))
    assert ledger.raw_list(p, scope, row["id"], reauthorize=authorized) == []
    capture(env, row, call, provider_reply())

    def revoked(_session, _p, _scope, _material):
        raise DomainError("ANALYSIS_SOURCE_CHANGED")

    expect("ANALYSIS_SOURCE_CHANGED", lambda: ledger.raw(p, scope, row["id"], call["id"], reauthorize=revoked))
    expect("ANALYSIS_SOURCE_CHANGED", lambda: ledger.raw_list(p, scope, row["id"], reauthorize=revoked))


def test_raw_journal_scoped_fk_and_tamper_guard(env):
    store, _a, ledger, scope, p, _r, _m = env
    row, lease = start(env)
    call = reserve(env, row, lease)
    saved = capture(env, row, call, provider_reply())
    other, _lease = start(env, "other")
    bad = {k: v for k, v in saved.items() if k not in {"replayed", "created_at"}}
    bad.update(id=str(uuid4()), analysis_id=other["id"])
    with pytest.raises(IntegrityError), Session(store.engine) as session, session.begin():
        session.add(RawProviderReply(**bad, created_at=store._now(session)))
        session.flush()
    with Session(store.engine) as session, session.begin():
        item = session.scalar(select(RawProviderReply).where(*filters(RawProviderReply, scope), RawProviderReply.id == saved["id"]))
        item.provider_reply = {**item.provider_reply, "elapsed_seconds": 999}
    expect("RAW_PROVIDER_REPLY_TAMPERED", lambda: ledger.raw(p, scope, row["id"], call["id"], reauthorize=authorized))
