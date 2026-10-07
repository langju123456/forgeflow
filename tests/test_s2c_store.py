"""TEST_DRIVER persistence: statusless model replies, server-owned completion."""

import copy
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_grounded_store import authorized, catalog, expect, plan
from test_grounded_store import env as old_environment
from test_s2c_contracts import selection

from forgeflow import contracts
from forgeflow.analysis_store import AnalysisCall, AnalysisRun
from forgeflow.contracts import DomainError, canonical, digest
from forgeflow.grounded_store import GroundedStore, GroundingRecord, RawProviderReply
from forgeflow.s2c_contracts import REVISION, STATUS_AUTHORITY, project, render
from forgeflow.s2c_store import STEP_KEYS, S2CAnalysisStore, S2CStore
from forgeflow.store import filters


@pytest.fixture
def env(tmp_path):
    source, _old_analysis, _old_ledger, scope, principal, run_id, material = old_environment.__wrapped__(tmp_path)
    analysis = S2CAnalysisStore(source)
    ledger = S2CStore(analysis)
    material.update(analysis_revision=REVISION, schema_catalog=catalog(), claim_catalog_hash=digest(catalog()))
    return source, analysis, ledger, scope, principal, run_id, material


def start(env):
    _source, analysis, _ledger, scope, p, run_id, material = env
    row = analysis.start(p, scope, run_id, "s2c", material, reauthorize=authorized)
    lease = analysis.claim(p, scope, row["id"], "s2c-controller", reauthorize=authorized)
    return row, lease


def phases(env, row, lease, *, journal=True, grade=True, summary_catalog=None, wrong_steps=False, raw_usage=None):
    _source, analysis, ledger, scope, p, _run, material = env
    outputs = {"PLAN": plan(), "CONTEXT_PROPOSAL": {"requests": []},
               "FINAL_REPORT": selection(material["schema_catalog"]),
               "SUMMARY": selection(summary_catalog or material["schema_catalog"], "summary")}
    saved = []
    for phase, role in (("PLAN", "supervisor"), ("CONTEXT_PROPOSAL", "pm"),
                        ("FINAL_REPORT", "pm"), ("SUMMARY", "supervisor")):
        data = summary_catalog if phase == "SUMMARY" and summary_catalog is not None else material["schema_catalog"]
        call_material = {**material, "schema_catalog": data, "claim_catalog_hash": digest(data)}
        call = analysis.reserve_call(
            p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"],
            phase.lower() if wrong_steps else STEP_KEYS[phase], role, phase, "c" * 64, "d" * 64,
            material=call_material, reauthorize=authorized,
        )
        output = outputs[phase]
        raw = canonical(output)
        provenance = {"transport": "TEST_DRIVER", "actual_model_revision": "b" * 64,
                      "finish_reason": "stop", "status": "SUCCEEDED", "dispatched": True,
                      "completion_confirmed": True}
        usage = {"state": "CONFIRMED", "input_tokens": 100, "output_tokens": 30, "cost_microusd": None}
        reply = {**provenance, "content": output, "content_utf8": raw,
                 "usage": raw_usage if raw_usage is not None else usage}
        if journal:
            ledger.capture(p, scope, row["id"], call["id"], reply, "c" * 64, data, reauthorize=authorized)
        analysis.finish_call(p, scope, row["id"], call["id"], lease["token"], lease["epoch"],
                             lease["owner"], output, usage, provider_metadata={
                                 k: provenance[k] for k in ("transport", "actual_model_revision", "finish_reason")
                             }, reauthorize=authorized)
        if grade:
            ledger.append(p, scope, row["id"], call["id"], {
                "raw_reply": raw, "structure_valid": True, "semantic_valid": True, "codes": [],
                "claim_catalog": data, "rendered_report": render(data, output) if phase in {"FINAL_REPORT", "SUMMARY"} else None,
                "provider_provenance": {**provenance, "input_hash": "c" * 64},
            }, reauthorize=authorized)
        saved.append(call)
    return saved, outputs["SUMMARY"]


def complete(env, row, lease, final, *, reauthorize=authorized):
    _source, analysis, _ledger, scope, p, _run, _material = env
    return analysis.complete(p, scope, row["id"], lease["token"], lease["epoch"], lease["owner"],
                             final, reauthorize=reauthorize)


def test_complete_uses_server_projection_and_does_not_rewrite_any_model_output(env):
    source, analysis, ledger, scope, p, parent_id, material = env
    material["schema_catalog"]["concerns"] = [{"id": "c_" + "f" * 16, "source_ref": "src_" + "a" * 32,
                                              "kind": "UNTRUSTED_INSTRUCTION"}]
    material["claim_catalog_hash"] = digest(material["schema_catalog"])
    before_parent = source.get_run(p, scope, parent_id)
    row, lease = start(env)
    _calls, raw_summary = phases(env, row, lease)
    before = analysis.get(p, scope, row["id"], reauthorize=authorized)
    result = complete(env, row, lease, project("summary", raw_summary, material["schema_catalog"]).model_dump(mode="json"))
    assert result["state"] == "COMPLETE"
    assert result["summary_json"]["selection"] == raw_summary
    assert result["summary_json"]["server_projection"] == {
        "status": "COMPLETE", "status_authority": STATUS_AUTHORITY, "catalog_hash": digest(material["schema_catalog"]),
    }
    assert before["calls"] == result["calls"]
    assert all("status" not in call["output_json"] for call in result["calls"][2:])
    assert all(ledger.raw(p, scope, row["id"], call["id"], reauthorize=authorized)["provider_reply"]["content_utf8"] == canonical(call["output_json"])
               for call in result["calls"])
    assert source.get_run(p, scope, parent_id) == before_parent
    assert result["lease_token"] is None
    event = analysis.events(p, scope, row["id"], reauthorize=authorized)[-1]
    assert event["kind"] == "ANALYSIS_FINISHED" and event["payload_json"]["status_authority"] == STATUS_AUTHORITY


@pytest.mark.parametrize("check,evidence", [("UNKNOWN", "MISSING"), ("UNKNOWN", "EXPIRED"),
                                            ("UNKNOWN", "CONFLICT"), ("VIOLATED", "MEASURED")])
def test_unresolved_catalog_is_valid_selection_but_server_incomplete(env, check, evidence):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    material["schema_catalog"]["observations"][3].update(check_state=check, evidence_state=evidence)
    material["claim_catalog_hash"] = digest(material["schema_catalog"])
    row, lease = start(env)
    _calls, output = phases(env, row, lease)
    result = complete(env, row, lease, project("summary", output, material["schema_catalog"]).model_dump(mode="json"))
    assert result["state"] == "INCOMPLETE"
    assert "status" not in result["summary_json"]["selection"]


@pytest.mark.parametrize("journal,grade", [(False, True), (True, False), (False, False)])
def test_direct_finish_call_without_raw_and_grade_cannot_complete(env, journal, grade):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    row, lease = start(env)
    _calls, output = phases(env, row, lease, journal=journal, grade=grade)
    expect("S2C_GROUNDING_EVIDENCE_REQUIRED", lambda: complete(env, row, lease,
        project("summary", output, material["schema_catalog"]).model_dump(mode="json")))


def test_summary_cannot_upgrade_from_a_different_authorized_catalog(env):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    row, lease = start(env)
    other = copy.deepcopy(material["schema_catalog"])
    other["observations"][4]["value"] = 67
    _calls, output = phases(env, row, lease, summary_catalog=other)
    expect("S2C_HANDOFF_CATALOG_MISMATCH", lambda: complete(env, row, lease,
        project("summary", output, other).model_dump(mode="json")))


@pytest.mark.parametrize("mutation", ["status", "concerns", "extra"])
def test_caller_final_must_equal_exact_internal_projection(env, mutation):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    row, lease = start(env)
    _calls, output = phases(env, row, lease)
    final = project("summary", output, material["schema_catalog"]).model_dump(mode="json")
    if mutation == "status":
        final["status"] = "INCOMPLETE"
    elif mutation == "concerns":
        final["concerns"] = []
    else:
        final["arbitrary_model_summary"] = "Already approved"
    expect("S2C_PROJECTION_MISMATCH", lambda: complete(env, row, lease, final))


def test_roles_and_step_keys_are_exact_not_just_four_validated_rows(env):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    row, lease = start(env)
    _calls, output = phases(env, row, lease, wrong_steps=True)
    expect("ANALYSIS_INCOMPLETE", lambda: complete(env, row, lease,
        project("summary", output, material["schema_catalog"]).model_dump(mode="json")))


@pytest.mark.parametrize("usage", [
    {"state": "UNKNOWN", "input_tokens": None, "output_tokens": None, "cost_microusd": None},
    {"state": "CONFIRMED", "input_tokens": 999, "output_tokens": 30, "cost_microusd": None},
    {"state": "CONFIRMED", "input_tokens": 3501, "output_tokens": 30, "cost_microusd": None},
])
def test_recorded_usage_cannot_disagree_with_actual_raw_provider_reply(env, usage):
    _source, _analysis, _ledger, _scope, _p, _run, material = env
    row, lease = start(env)
    _calls, output = phases(env, row, lease, raw_usage=usage)
    expect("S2C_PROVIDER_EVIDENCE_REQUIRED", lambda: complete(env, row, lease,
        project("summary", output, material["schema_catalog"]).model_dump(mode="json")))


def test_current_revocation_cancellation_and_stale_lease_block_completion(env):
    source, analysis, _ledger, scope, p, _run, material = env
    row, lease = start(env)
    _calls, output = phases(env, row, lease)
    final = project("summary", output, material["schema_catalog"]).model_dump(mode="json")

    def revoked(_session, _p, _scope, _material):
        raise DomainError("ANALYSIS_SOURCE_CHANGED")

    expect("ANALYSIS_SOURCE_CHANGED", lambda: complete(env, row, lease, final, reauthorize=revoked))
    with Session(source.engine) as session, session.begin():
        item = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope), AnalysisRun.id == row["id"]))
        item.lease_expires_at = contracts.utcnow() - timedelta(seconds=1)
    expect("STALE_ANALYSIS_LEASE", lambda: complete(env, row, lease, final))
    with Session(source.engine) as session, session.begin():
        item = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope), AnalysisRun.id == row["id"]))
        item.lease_expires_at = contracts.utcnow() + timedelta(seconds=60)
    analysis.cancel(p, scope, row["id"])
    expect("ANALYSIS_CANCEL_REQUESTED", lambda: complete(env, row, lease, final))


def test_new_store_does_not_reinterpret_old_revision_records(env):
    _source, analysis, _ledger, scope, p, run_id, material = env
    old = analysis.__class__.__bases__[0](analysis.store)
    old_material = {**material, "analysis_revision": "grounded-selection-v2"}
    row = old.start(p, scope, run_id, "old", old_material, reauthorize=authorized)
    GroundedStore(old).list(p, scope, row["id"], reauthorize=authorized)
    expect("S2C_REVISION_REQUIRED", lambda: S2CStore(old).list(p, scope, row["id"], reauthorize=authorized))
    expect("S2C_REVISION_REQUIRED", lambda: analysis.start(p, scope, run_id, "bad-new", old_material, reauthorize=authorized))


@pytest.mark.parametrize("target", ["raw", "grade", "output"])
def test_complete_rejects_storage_tampering(env, target):
    source, _analysis, _ledger, scope, _p, _run, material = env
    row, lease = start(env)
    calls, output = phases(env, row, lease)
    with Session(source.engine) as session, session.begin():
        if target == "raw":
            item = session.scalar(select(RawProviderReply).where(*filters(RawProviderReply, scope), RawProviderReply.call_id == calls[2]["id"]))
            item.provider_reply = {**item.provider_reply, "content_utf8": "fabricated"}
            code = "RAW_PROVIDER_REPLY_TAMPERED"
        elif target == "grade":
            item = session.scalar(select(GroundingRecord).where(*filters(GroundingRecord, scope), GroundingRecord.call_id == calls[2]["id"]))
            item.rendered_report = {"status": "COMPLETE"}
            code = "GROUNDING_RECORD_TAMPERED"
        else:
            item = session.scalar(select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.id == calls[2]["id"]))
            item.output_json = {**item.output_json, "status": "COMPLETE"}
            code = "GROUNDING_OUTPUT_MISMATCH"
    expect(code, lambda: complete(env, row, lease,
        project("summary", output, material["schema_catalog"]).model_dump(mode="json")))
