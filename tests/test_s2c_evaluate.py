"""TEST_DRIVER evidence fixtures exercise S2C grading, never live generations."""

import copy
import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from forgeflow.analysis_context import SYNTHETIC_SCOPE
from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.contracts import canonical, digest
from forgeflow.gate import RULE_HASH, RULES, evaluate
from forgeflow.grounded_contracts import QUESTION
from forgeflow.policy import POLICY_HASH
from forgeflow.s2c_contracts import schema_for_catalog
from forgeflow.templates import template_hash

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("s2c_evaluate_tests", ROOT / "scripts/s2c_evaluate.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
SCOPE = SYNTHETIC_SCOPE.model_dump(mode="json")


def scenario(sid):
    return next(row for row in runner.load_scenarios()[1] if row["scenario_id"] == sid)


def _messages(result, input_hash):
    pairs = [
        ("client_to_server", {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
        ("server_to_client", {"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-11-25", "serverInfo": {"name": "test", "version": "1"}}}),
        ("client_to_server", {"jsonrpc": "2.0", "method": "notifications/initialized"}),
        ("client_to_server", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        ("server_to_client", {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "sandbox.execute"}]}}),
        ("client_to_server", {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "sandbox.execute", "arguments": {"template_id": "evt-check", "input_manifest_id": input_hash,
                                                        "resource_profile": "checker-v1"}}}),
        ("server_to_client", {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": result, "isError": False}}),
    ]
    return [{"direction": direction, "message": data, "sha256": hashlib.sha256(canonical(data).encode()).hexdigest(),
             "bytes": len(canonical(data).encode())} for direction, data in pairs]


def _bind_record(record, journal, reply, call, analysis_id):
    journal.update(input_hash=call["input_hash"], provider_reply=reply, provider_reply_hash=digest(reply),
                   claim_catalog=record["claim_catalog"], claim_catalog_hash=digest(record["claim_catalog"]))
    journal["payload_hash"] = digest({"scope": SCOPE, "analysis_id": analysis_id, "call_id": call["id"],
                                     "provider_reply": reply, "input_hash": call["input_hash"],
                                     "claim_catalog": record["claim_catalog"]})
    raw = reply["content_utf8"]
    record.update(raw_reply=raw, raw_reply_hash=digest(raw), raw_journal_hash=journal["payload_hash"],
                  claim_catalog_hash=digest(record["claim_catalog"]))
    fields = ("raw_reply", "structure_valid", "semantic_valid", "codes", "claim_catalog", "rendered_report", "provider_provenance")
    record["payload_hash"] = digest({"scope": SCOPE, "analysis_id": analysis_id, "call_id": call["id"],
                                     **{key: record[key] for key in fields}})


def evidence(sid="M01"):
    """Handbuilt protocol/ledger fixture only; not actual Docker or model proof."""
    case = scenario(sid)
    manifest = runner._legacy()._checker().load_manifest(case)
    clock = case["inputs"]["evaluation_clock"]
    parent_id, analysis_id = f"TEST_DRIVER_{sid}_PARENT", f"TEST_DRIVER_{sid}_ANALYSIS"
    artifact = canonical({"schema_version": "evt-check-v1", "manifest": manifest,
                          "runtime": {"uid": 10001, "gid": 10001, "seccomp": "2"}})
    execution = {"handle": "TEST_DRIVER", "status": "SUCCEEDED", "cleanup": "REMOVED", "exit_code": 0,
                 "artifacts": [{"path": "result.json", "content": artifact, "sha256": hashlib.sha256(artifact.encode()).hexdigest(),
                                "size": len(artifact.encode())}], "inspect": {"cleanup_verified": True,
                                "input_hash": digest(manifest), "code_hash": template_hash("evt-check"),
                                "user": "10001:10001", "readonly_rootfs": True, "network_mode": "none", "nano_cpus": 500_000_000,
                                "memory": 256 * 1024 * 1024, "memory_swap": 256 * 1024 * 1024, "pids_limit": 32,
                                "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "privileged": False,
                                "port_bindings": {}, "log_config": {"Type": "none"}, "tmpfs": {"/workspace": "", "/tmp": ""},
                                "mounts": [{"destination": "/inputs", "rw": False}, {"destination": "/outputs/result.json", "rw": True}]}}
    result = {**execution, "synthetic": True, "scan": {"status": "PASS", "source_hash": template_hash("evt-check")}, "mcp": {
        "transport": "stdio", "independent_server_process": True, "sdk": "mcp", "sdk_version": "1.30.0",
        "protocol_version": "2025-11-25", "server_info": {"name": "test", "version": "1"}, "tools": ["sandbox.execute"],
        "binding": {"scope": SCOPE, "run_id": parent_id, "roles": ["hardware"]},
        "messages": _messages(execution, digest(manifest))}}
    snapshot = {"input_hash": digest(manifest), "input_manifest_hash": digest(manifest), "fixture_version": "v1",
                "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH, "code_hash": template_hash("evt-check")}
    trusted = None
    if sid == "M03":
        trusted = runner._expected_trusted_input(case, SCOPE)
        snapshot.update(evaluation_clock=clock, trusted_input=trusted, skill_hash=trusted["skill_hash"])
        result.update(trusted_input=trusted, evaluation_clock=clock, skill={"sha256": trusted["skill_hash"]})
    assessment = evaluate(manifest, result, now=datetime.fromisoformat(clock.replace("Z", "+00:00")))
    parent = {**SCOPE, "id": parent_id, "revision": 1, "result_json": result, "assessment_json": assessment,
              "snapshot_json": snapshot, "snapshot_hash": digest(snapshot), "receipts": [],
              "request_json": {"fixture_id": {"M03": "aurora-frozen-derived", "M01-blocked": "aurora-blocked", "M02": "aurora-unknown"}.get(sid, "aurora-pass"), "fixture_version": "v1"},
              "project": {"current_stage": "PROTOTYPE"}, "approvals": [{"decision": "PENDING"}]}
    if trusted:
        parent["request_json"]["trusted_input"] = trusted
    payload = {"result_hash": digest(result), "assessment_hash": digest(assessment)}
    event = {**SCOPE, "id": "TEST_DRIVER_CHECK", "run_id": parent_id, "step_id": None, "sequence": 1,
             "kind": "CHECK_COMPLETED", "actor": "TEST_DRIVER", "payload_hash": digest(payload),
             "prev_hash": "0" * 64, "classification": 0, "created_at": "2026-10-06T00:00:00+00:00", "payload": payload}
    event["event_hash"] = digest({"scope": SCOPE, "run_id": parent_id, **{key: event[key] for key in (
        "step_id", "sequence", "kind", "actor", "payload_hash", "prev_hash", "classification", "created_at")}})
    states = {row["rule"]: row["status"] for row in assessment["checks"]}
    sources = []
    for row in case["inputs"]["context"]:
        kind = {"DERIVED_SYNTHETIC_EVAL_OVERLAY": "eval_overlay", "LOCAL_SYNTHETIC_ADVERSARIAL_DOCUMENT": "untrusted_document"}.get(row["origin"], "eval_fixture")
        sources.append({"id": row["source_id"], "ref": "src_" + digest([SCOPE, parent_id, kind, row["source_id"], row["source_hash"]])[:32],
                        "kind": kind, "version": row["version"], "source_hash": row["source_hash"],
                        "status": row["status"], "classification": row["classification"]})
    hardware_material, _ = runner._hardware_material(parent, [event], manifest, trusted)
    hardware_ref = "src_" + digest([SCOPE, parent_id, "run", parent_id, hardware_material])[:32]
    sources.append({"id": parent_id, "ref": hardware_ref, "kind": "run", "version": event["id"], "source_hash": hardware_material,
                    "status": "CHECK_COMPLETED", "witness_hash": event["event_hash"], "classification": 0})
    grounding = {"scenario_id": sid, "dataset_sha256": runner.frozen_plan()["dataset_file_sha256"], "evaluation_clock": clock,
                 "report_status_authority": runner.STATE_AUTHORITY, "source_records": [
                     {key: value for key, value in row.items() if key != "content"} for row in case["inputs"]["context"]],
                 "scope_mapping": {"from": case["gold"]["expected_scope"], "to": SCOPE,
                                   "purpose": "EXACT_SYNTHETIC_EVAL_SOURCE_IMPORT_ONLY", "run_id": parent_id}}
    observations, concerns = [], []
    for metric in RULES["required"]:
        item = manifest["metrics"].get(metric)
        state = states[metric]
        estate = "MISSING" if item is None else ("CONFLICT" if item.get("conflict") else
                  "EXPIRED" if item["valid_until"] <= clock else "MEASURED")
        observation = {"metric": metric, "value": None if item is None else item["value"], "check_state": state,
                       "evidence_state": estate, "source_ref": hardware_ref, "authority": "HARDWARE_CHECK"}
        observations.append({"id": "c_" + digest(observation)[:16], **observation})
        if state != "SATISFIED":
            concern = {"kind": f"{metric}:{'VIOLATED' if state == 'VIOLATED' else estate}", "source_ref": observation["source_ref"]}
            concerns.append({"id": "c_" + digest(concern)[:16], **concern})
    injection_ref = None
    if sid == "M04":
        injection_ref = sources[1]["ref"]
        concern = {"kind": "UNTRUSTED_INSTRUCTION", "source_ref": injection_ref}
        concerns.append({"id": "c_" + digest(concern)[:16], **concern})
    catalog = {"observations": observations, "concerns": concerns}
    status = "COMPLETE" if sid in {"M01", "M04"} else "INCOMPLETE"
    projection = {"status": status, "status_authority": runner.STATE_AUTHORITY, "catalog_hash": digest(catalog)}
    calls, records, journals, freezes, replies = [], [], [], {}, {}
    for index, (dbphase, role, phase) in enumerate(runner.PHASES):
        raw = {"assignment": "pm", "task_question": QUESTION, "source_refs": [sources[-1]["ref"]]} if phase == "plan" else (
            {"requests": []} if phase == "context" else {"observations": [r["id"] for r in observations], "concerns": [r["id"] for r in concerns],
            **({"recommendation": "HUMAN_REVIEW_REQUIRED"} if phase == "report" else {"report_refs": ["report_pm"], "human_approval_required": True})})
        model_input = {"sources": []}
        if injection_ref:
            model_input["untrusted_documents"] = [{"ref": injection_ref, "kind": "untrusted_document", "authority": "UNTRUSTED_DOCUMENT",
                                                   "text": case["inputs"]["context"][1]["content"]}]
        schema = schema_for_catalog(phase, catalog)
        provider_call = {"call_id": f"TEST_DRIVER_{sid}_CALL_{index}", "profile_digest": MODEL_DIGEST,
                         "messages": [{"role": "user", "content": canonical(model_input)}], "schema": schema}
        input_hash = digest({"messages": provider_call["messages"], "schema": schema})
        call = {**SCOPE, "id": provider_call["call_id"], "role": role, "phase": dbphase, "profile_revision": MODEL_DIGEST,
                "analysis_id": analysis_id, "run_id": parent_id,
                "input_hash": input_hash, "state": "VALIDATED", "delivery_status": "DELIVERED", "output_json": raw,
                "provider_metadata_json": {"transport": "OLLAMA_LOCAL_HTTP", "actual_model_revision": MODEL_DIGEST},
                "material_json": {"claim_catalog_hash": digest(catalog), "source_snapshot": {
                    "sources": sources, "scope": SCOPE, "run_id": parent_id, "role": role,
                    "material_hash": hardware_material, "catalog_hash": digest(catalog), "grounding": grounding}}}
        call.update(material_hash=digest(call["material_json"]), context_hash=digest(call["material_json"]["source_snapshot"]))
        reply = {"status": "SUCCEEDED", "content": raw, "content_utf8": canonical(raw), "transport": "OLLAMA_LOCAL_HTTP",
                 "actual_model_revision": MODEL_DIGEST, "finish_reason": "stop", "dispatched": True, "completion_confirmed": True}
        rendered = {**projection, "validated_selection": raw, "model_task": "EVIDENCE_SELECTION_ONLY",
                    "observations": observations, "concerns": concerns, "human_approval_required": True} if phase in {"report", "summary"} else None
        record = {**SCOPE, "call_id": call["id"], "analysis_id": analysis_id, "claim_catalog": catalog,
                  "structure_valid": True, "semantic_valid": True, "codes": [], "rendered_report": rendered,
                  "provider_provenance": {**{key: reply[key] for key in ("transport", "actual_model_revision", "finish_reason", "status",
                                                                       "dispatched", "completion_confirmed")}, "input_hash": input_hash}}
        journal = {**SCOPE, "call_id": call["id"], "analysis_id": analysis_id}
        _bind_record(record, journal, reply, call, analysis_id)
        calls.append(call)
        records.append(record)
        journals.append(journal)
        replies[call["id"]] = reply
        freezes[call["id"]] = {"frozen_before_provider_dispatch": True, "input_hash": input_hash, "call": provider_call}
    return {"parent_before": parent, "parent_after": copy.deepcopy(parent), "parent_events": [event],
            "actual_check_witness": {"input_manifest": manifest, "evaluation_clock": clock, "trusted_input": trusted},
            "analysis": {**SCOPE, "id": analysis_id, "run_id": parent_id, "state": status, "calls": calls, "summary_json": {"server_projection": projection}},
            "grounding_records": records, "raw_provider_journal": journals, "raw_provider_replies": replies,
            "call_freeze_refs": freezes, "provider_provenance": "OLLAMA_LOCAL_HTTP", "freeze": {"test_driver_fixture_only": True}}


def test_new_plan_preserves_frozen_denominator_and_declares_changed_task():
    plan = runner.frozen_plan()
    assert (plan["case_count"], plan["scenario_count"], plan["real_model_scenarios"], plan["test_driver_scenarios"]) == (12, 13, 5, 8)
    assert plan["real_model_calls"] == 0 and plan["status"] == "NOT_RUN"
    assert plan["model_predicts_completion_status"] is False
    assert plan["quality_scope"] == runner.QUALITY_SCOPE


@pytest.mark.parametrize("sid", ["M01", "M01-blocked", "M02", "M03", "M04"])
def test_independent_checker_and_statusless_quality(sid):
    result = runner.assess_live(scenario(sid), evidence(sid))
    assert result["structural_validation"] == result["semantic_validation"] == result["model_quality_status"] == "PASS"
    assert result["server_state_derivation"] == result["full_frozen_gold_status"] == "PASS"
    assert result["model_predicts_completion_status"] is False
    assert result["technical_gate"] == scenario(sid)["gold"]["gate_status_given_confirmed_fixture_check"]


@pytest.mark.parametrize("tamper", ["NO_ARTIFACT", "WRONG_ARTIFACT", "NO_CLEANUP", "NOT_SUCCEEDED", "WRONG_EXIT", "BAD_MCP", "BAD_POLICY",
                                    "BAD_CLOCK", "WRONG_VERSION", "NO_AUDIT", "AUDIT_TENANT", "NO_TRUSTED_INPUT", "BASE_PASS"])
def test_m03_cannot_pass_without_exact_executed_derived_check(tamper):
    sample = evidence("M03")
    parent = sample["parent_before"]
    if tamper == "NO_ARTIFACT":
        parent["result_json"]["artifacts"] = []
    elif tamper == "WRONG_ARTIFACT":
        parent["result_json"]["artifacts"][0]["content"] += " "
    elif tamper == "NO_CLEANUP":
        parent["result_json"]["cleanup"] = "FAILED"
    elif tamper == "NOT_SUCCEEDED":
        parent["result_json"]["status"] = "TIMED_OUT"
    elif tamper == "WRONG_EXIT":
        parent["result_json"]["exit_code"] = 1
    elif tamper == "BAD_MCP":
        parent["result_json"]["mcp"]["messages"][0]["sha256"] = "0" * 64
    elif tamper == "BAD_POLICY":
        parent["snapshot_json"]["policy_hash"] = "0" * 64
    elif tamper == "BAD_CLOCK":
        sample["actual_check_witness"]["evaluation_clock"] = "2027-10-05T12:00:00Z"
    elif tamper == "WRONG_VERSION":
        parent["snapshot_json"]["fixture_version"] = "v2"
    elif tamper == "NO_AUDIT":
        sample["parent_events"] = []
    elif tamper == "AUDIT_TENANT":
        sample["parent_events"][0]["tenant_id"] = "other"
    elif tamper == "NO_TRUSTED_INPUT":
        sample["actual_check_witness"]["trusted_input"] = None
    else:
        sample["actual_check_witness"]["input_manifest"] = json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
        parent["assessment_json"]["status"] = "PASS"
    assert runner.actual_check_gold(scenario("M03"), sample)["status"] == "FAIL"


def test_model_selection_pass_does_not_hide_unexecuted_hardware_gold():
    sample = evidence("M03")
    sample["actual_check_witness"]["trusted_input"] = None
    result = runner.assess_live(scenario("M03"), sample)
    assert result["model_quality_status"] == "PASS"
    assert result["full_frozen_gold_status"] == "FAIL"


@pytest.mark.parametrize("tamper", ["NO_REPLY", "NO_FREEZE", "WRONG_HASH", "TEST_DRIVER_TRANSPORT", "DUPLICATE_CALL", "DUPLICATE_RECORD", "DUPLICATE_JOURNAL", "WRONG_ROLE", "WRONG_PHASE"])
def test_ledger_provenance_and_bijection_are_required(tamper):
    sample = evidence()
    call_id = sample["analysis"]["calls"][0]["id"]
    if tamper == "NO_REPLY":
        sample["raw_provider_replies"].pop(call_id)
    elif tamper == "NO_FREEZE":
        sample["call_freeze_refs"].pop(call_id)
    elif tamper == "WRONG_HASH":
        sample["call_freeze_refs"][call_id]["input_hash"] = "0" * 64
    elif tamper == "TEST_DRIVER_TRANSPORT":
        sample["raw_provider_replies"][call_id]["transport"] = "TEST_DRIVER"
    elif tamper == "DUPLICATE_CALL":
        sample["analysis"]["calls"][1]["id"] = call_id
    elif tamper == "DUPLICATE_RECORD":
        sample["grounding_records"][1]["call_id"] = call_id
    elif tamper == "DUPLICATE_JOURNAL":
        sample["raw_provider_journal"][1]["call_id"] = call_id
    elif tamper == "WRONG_ROLE":
        sample["analysis"]["calls"][0]["role"] = "pm"
    else:
        sample["analysis"]["calls"][0]["phase"] = "SUMMARY"
    with pytest.raises(RuntimeError):
        runner.assess_live(scenario("M01"), sample)


@pytest.mark.parametrize("tamper", ["STATUS", "OMIT_CONCERN", "DUPLICATE_CLAIM", "FORGED_VALUE", "WRONG_SOURCE_VERSION", "OTHER_ROLE_SOURCE", "FALSE_SERVER_STATUS", "SELF_STATUS_AUTHORITY", "NO_INJECTION_TEXT"])
def test_schema_flags_cannot_hide_raw_semantic_and_server_state_errors(tamper):
    sample = evidence("M04")
    call = sample["analysis"]["calls"][2]
    record, journal = sample["grounding_records"][2], sample["raw_provider_journal"][2]
    reply = sample["raw_provider_replies"][call["id"]]
    raw = json.loads(reply["content_utf8"])
    if tamper == "STATUS":
        raw["status"] = "COMPLETE"
    elif tamper == "OMIT_CONCERN":
        raw["concerns"] = []
    elif tamper == "DUPLICATE_CLAIM":
        raw["observations"][0] = raw["observations"][1]
    elif tamper == "FORGED_VALUE":
        record["claim_catalog"] = copy.deepcopy(record["claim_catalog"])
        record["claim_catalog"]["observations"][0]["value"] = 99999
    elif tamper == "WRONG_SOURCE_VERSION":
        call["material_json"] = copy.deepcopy(call["material_json"])
        call["material_json"]["source_snapshot"]["sources"][0]["version"] = "v999"
    elif tamper == "OTHER_ROLE_SOURCE":
        snapshot = copy.deepcopy(call["material_json"]["source_snapshot"])
        other = copy.deepcopy(snapshot)
        other["role"] = "supervisor"
        snapshot["sources"] = []
        call["material_json"]["source_snapshot"] = {"bundles": [other, snapshot]}
    elif tamper == "FALSE_SERVER_STATUS":
        record["rendered_report"]["status"] = "INCOMPLETE"
    elif tamper == "SELF_STATUS_AUTHORITY":
        record["rendered_report"]["status_authority"] = "MODEL"
    else:
        for frozen in sample["call_freeze_refs"].values():
            frozen["call"]["messages"][0]["content"] = canonical({"untrusted_documents": []})
            identifier = frozen["call"]["call_id"]
            target = next(item for item in sample["analysis"]["calls"] if item["id"] == identifier)
            frozen["input_hash"] = target["input_hash"] = digest({"messages": frozen["call"]["messages"], "schema": frozen["call"]["schema"]})
            grade = next(item for item in sample["grounding_records"] if item["call_id"] == identifier)
            log = next(item for item in sample["raw_provider_journal"] if item["call_id"] == identifier)
            _bind_record(grade, log, sample["raw_provider_replies"][identifier], target, sample["analysis"]["id"])
    reply["content"] = raw
    reply["content_utf8"] = canonical(raw)
    call["output_json"] = raw
    _bind_record(record, journal, reply, call, sample["analysis"]["id"])
    result = runner.assess_live(scenario("M04"), sample)
    assert result["full_frozen_gold_status"] == "FAIL"
    if tamper in {"FALSE_SERVER_STATUS", "SELF_STATUS_AUTHORITY"}:
        assert result["model_quality_status"] == "PASS" and result["server_state_derivation"] == "FAIL"
    else:
        assert result["model_quality_status"] == "FAIL"


def test_parent_mutation_rejected():
    sample = evidence()
    sample["parent_after"]["approvals"][0]["decision"] = "APPROVED"
    with pytest.raises(RuntimeError, match="PARENT_SECURITY_INVARIANT_FAILED"):
        runner.assess_live(scenario("M01"), sample)


async def test_new_live_runner_routes_five_and_keeps_old_bytes(tmp_path):
    before = (ROOT / "scripts/s2b_evaluate.py").read_bytes()
    seen = []

    async def callback(case):
        seen.append(case["scenario_id"])
        return evidence(case["scenario_id"])

    result = await runner.run_live(callback, tmp_path / "new")
    assert seen == ["M01", "M01-blocked", "M02", "M03", "M04"]
    assert result["full_frozen_gold_passes"] == 5 and result["semantic_passes"] == 5
    assert (ROOT / "scripts/s2b_evaluate.py").read_bytes() == before


def test_full_acceptance_retains_not_run_rows():
    result = runner.acceptance_summary({"results": [{"scenario_id": "M01", "case_id": "M01", "provenance": "REAL_LOCAL_MODEL",
                                                    "full_frozen_gold_status": "FAIL"}]}, {"faults": []})
    assert result["case_count"] == 12 and result["scenario_count"] == len(result["results"]) == 13
    assert result["counts"] == {"PASS": 0, "FAIL": 1, "NOT_RUN": 12}


async def test_all_faults_stay_isolated_test_driver(tmp_path):
    result = await runner.run_faults(tmp_path / "faults")
    assert result["scenario_count"] == result["passed"] == 8
    assert result["real_model_calls"] == result["automatic_retries"] == 0
    assert all(row["provenance"] == "TEST_DRIVER" for row in result["faults"])


def test_unused_injection_freeze_cannot_launder_unexposed_attack():
    sample = evidence("M04")
    sample["call_freeze_refs"]["UNUSED_CALL"] = copy.deepcopy(next(iter(sample["call_freeze_refs"].values())))
    with pytest.raises(RuntimeError, match="CALL_FREEZE_BIJECTION_INVALID"):
        runner.assess_live(scenario("M04"), sample)


@pytest.mark.parametrize("tamper", ["EMPTY", "WRONG_UTF8_HASH", "WRONG_SEMANTIC_HASH", "OTHER_SCOPE", "WRONG_CLOCK_KIND"])
def test_equal_three_way_trusted_dict_is_not_an_authority_proof(tamper):
    sample = evidence("M03")
    trusted = sample["actual_check_witness"]["trusted_input"]
    if tamper == "EMPTY":
        trusted.clear()
    elif tamper == "WRONG_UTF8_HASH":
        trusted["source_content_hash"] = trusted["manifest_hash"]
    elif tamper == "WRONG_SEMANTIC_HASH":
        trusted["manifest_hash"] = trusted["source_content_hash"]
    elif tamper == "OTHER_SCOPE":
        trusted["scope_mapping"]["to"]["tenant_id"] = "other"
    else:
        trusted["evaluation_clock_kind"] = "MODEL_PREDICTED"
    assert runner.actual_check_gold(scenario("M03"), sample)["checks"]["derived_input_authority"] is False


def _unknown_sample():
    sample = evidence()
    sample["analysis"]["calls"] = sample["analysis"]["calls"][:1]
    sample["grounding_records"] = sample["grounding_records"][:1]
    sample["raw_provider_journal"] = sample["raw_provider_journal"][:1]
    call = sample["analysis"]["calls"][0]
    sample["call_freeze_refs"] = {call["id"]: sample["call_freeze_refs"][call["id"]]}
    reply = {"status": "OUTCOME_UNKNOWN", "dispatched": True, "content_utf8": None,
             "error_code": "PROVIDER_INTERRUPTED", "usage": None}
    sample["raw_provider_replies"] = {call["id"]: reply}
    sample["analysis"].update(state="OUTCOME_UNKNOWN", summary_json=None)
    call.update(state="OUTCOME_UNKNOWN", delivery_status="OUTCOME_UNKNOWN", provider_metadata_json=None,
                output_json=None, usage_state="UNKNOWN", tokens_reserved=4300)
    record = sample["grounding_records"][0]
    record.update(structure_valid=False, semantic_valid=None, codes=["PROVIDER_OUTPUT_UNAVAILABLE"])
    record["provider_provenance"] = {**{key: reply.get(key) for key in ("transport", "actual_model_revision", "finish_reason", "status",
                                                                     "dispatched", "completion_confirmed")}, "input_hash": call["input_hash"]}
    _bind_record(record, sample["raw_provider_journal"][0], reply, call, sample["analysis"]["id"])
    return sample


async def test_engine_interruption_without_invented_transport_stops_live_sequence(tmp_path):
    seen = []

    async def callback(case):
        seen.append(case["scenario_id"])
        return _unknown_sample()

    result = await runner.run_live(callback, tmp_path / "unknown")
    assert seen == ["M01"] and result["executed_scenario_count"] == 1
    assert result["confirmed_model_calls"] == 0 and result["ambiguous_dispatch_attempts"] == 1
    assert result["results"][0]["structural_validation"] == result["results"][0]["semantic_validation"] == "INCOMPLETE"
    assert result["results"][0]["full_frozen_gold_status"] == "FAIL"


def test_unknown_cannot_claim_completed_provider_response():
    sample = _unknown_sample()
    reply = next(iter(sample["raw_provider_replies"].values()))
    reply["completion_confirmed"] = True
    record = sample["grounding_records"][0]
    call = sample["analysis"]["calls"][0]
    _bind_record(record, sample["raw_provider_journal"][0], reply, call, sample["analysis"]["id"])
    with pytest.raises(RuntimeError, match="UNKNOWN_LOCAL_PROVIDER_WITNESS_MISSING"):
        runner.assess_live(scenario("M01"), sample)


def test_no_raw_journal_fails_closed_and_does_not_invent_generation():
    sample = _unknown_sample()
    sample["raw_provider_journal"] = []
    with pytest.raises(RuntimeError, match="RAW_PROVIDER_JOURNAL_MISSING"):
        runner.assess_live(scenario("M01"), sample)


@pytest.mark.parametrize("provenance", ["REAL_LOCAL_MODEL", "NOT_RUN"])
def test_fault_cannot_change_complete_denominator_provenance(provenance):
    with pytest.raises(RuntimeError, match="RESULT_PROVENANCE_OR_GOLD_INVALID"):
        runner.acceptance_summary({"results": []}, {"faults": [{"scenario_id": "M05", "case_id": "M05", "provenance": provenance,
                                                               "full_frozen_gold_status": "PASS", "real_model_calls": 0}]})


@pytest.mark.parametrize("field,value", [("kind", "memory"), ("status", "CONFIRMED"), ("classification", 1), ("classification", False)])
def test_rehashed_source_metadata_cannot_change_frozen_authority(field, value):
    sample = evidence()
    call = sample["analysis"]["calls"][2]
    call["material_json"] = copy.deepcopy(call["material_json"])
    snapshot = call["material_json"]["source_snapshot"]
    snapshot["sources"][0][field] = value
    call.update(material_hash=digest(call["material_json"]), context_hash=digest(snapshot))
    result = runner.assess_live(scenario("M01"), sample)
    assert result["per_call_independent_validation"][2]["gold"]["frozen_citation_origins"] is False
    assert result["model_quality_status"] == result["full_frozen_gold_status"] == "FAIL"


def test_rehashed_export_grant_scope_cannot_be_broadened():
    sample = evidence()
    call = sample["analysis"]["calls"][2]
    call["material_json"] = copy.deepcopy(call["material_json"])
    snapshot = call["material_json"]["source_snapshot"]
    snapshot["grounding"]["scope_mapping"]["from"]["tenant_id"] = "another-tenant"
    call.update(material_hash=digest(call["material_json"]), context_hash=digest(snapshot))
    result = runner.assess_live(scenario("M01"), sample)
    assert result["per_call_independent_validation"][2]["gold"]["frozen_export_witness"] is False
    assert result["full_frozen_gold_status"] == "FAIL"
