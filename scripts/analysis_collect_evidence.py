"""Check actual persisted local chains without dispatching model or business calls."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.analysis_context import _mcp_projection
from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.analysis_runtime import build_runtime
from forgeflow.contracts import digest
from forgeflow.local import SCOPE, load_identities


def verify_chain(events):
    previous = "0" * 64
    for index, event in enumerate(events, start=1):
        if not (event["sequence"] == index and event["prev_hash"] == previous):
            raise RuntimeError(
                "EVIDENCE_CHECK_FAILED: event['sequence'] == index and event['prev_hash'] == previous"
            )
        if not event["payload_hash"] == digest(event["payload_json"]):
            raise RuntimeError(
                "EVIDENCE_CHECK_FAILED: event['payload_hash'] == digest(event['payload_json'])"
            )
        if event.get("material_json") is not None:
            if not event["payload_json"]["context_material_hash"] == digest(event["material_json"]):
                raise RuntimeError(
                    "EVIDENCE_CHECK_FAILED: event['payload_json']['context_material_hash'] == digest(event['material_json'])"
                )
        metadata = {
            "scope": SCOPE.model_dump(mode="json"),
            "analysis_id": event["analysis_id"],
            "actor": event["actor"],
            "kind": event["kind"],
            "sequence": event["sequence"],
            "payload_hash": event["payload_hash"],
            "prev_hash": event["prev_hash"],
            "created_at": datetime.fromisoformat(event["created_at"]).isoformat(),
        }
        if not event["event_hash"] == digest(metadata):
            raise RuntimeError("EVIDENCE_CHECK_FAILED: event['event_hash'] == digest(metadata)")
        previous = event["event_hash"]
    return previous


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    raise SystemExit("Use a new output path; preserve old evidence")
store, workflow = build_runtime()
caller = next(principal for principal in load_identities().values() if "admin" in principal.roles)
root = Path(__file__).resolve().parents[1]
evidence = root / "deliveries/2026-10-06/s2-evidence"
cases = []
for fixture, gate in (("pass", "PASS"), ("blocked", "BLOCKED"), ("unknown", "UNKNOWN")):
    snapshot = json.loads((evidence / f"live-{fixture}-attempt-03.json").read_text())
    original = snapshot["analysis"]
    current = workflow.store.get(caller, SCOPE, original["id"], reauthorize=workflow._reauthorize)
    if not current == original:
        raise RuntimeError("EVIDENCE_CHECK_FAILED: current == original")
    parent = store.get_run(caller, SCOPE, current["run_id"])
    if not parent["assessment_json"] == snapshot["technical_gate"]:
        raise RuntimeError("EVIDENCE_CHECK_FAILED: parent['assessment_json'] == snapshot['technical_gate']")
    if not parent["assessment_json"]["status"] == gate:
        raise RuntimeError("EVIDENCE_CHECK_FAILED: parent['assessment_json']['status'] == gate")
    if not (parent["project"] == snapshot["project"] and parent["project"]["current_stage"] == "PROTOTYPE"):
        raise RuntimeError(
            "EVIDENCE_CHECK_FAILED: parent['project'] == snapshot['project'] and parent['project']['current_stage'] == 'PROTOTYPE'"
        )
    if not (not parent["receipts"] and all(item["decision"] == "PENDING" for item in parent["approvals"])):
        raise RuntimeError(
            "EVIDENCE_CHECK_FAILED: not parent['receipts'] and all((item['decision'] == 'PENDING' for item in parent['approvals']))"
        )
    if not store.verify_events(caller, SCOPE, parent["id"]):
        raise RuntimeError("EVIDENCE_CHECK_FAILED: store.verify_events(caller, SCOPE, parent['id'])")
    if not _mcp_projection(SCOPE, parent["id"], parent) == "RECORDED":
        raise RuntimeError(
            "EVIDENCE_CHECK_FAILED: _mcp_projection(SCOPE, parent['id'], parent) == 'RECORDED'"
        )
    if not parent["result_json"]["scan"]["status"] == "PASS":
        raise RuntimeError("EVIDENCE_CHECK_FAILED: parent['result_json']['scan']['status'] == 'PASS'")
    if not parent["result_json"]["cleanup"] == "REMOVED":
        raise RuntimeError("EVIDENCE_CHECK_FAILED: parent['result_json']['cleanup'] == 'REMOVED'")
    calls = current["calls"]
    if not (len(calls) == 4 and all(call["state"] == "VALIDATED" for call in calls)):
        raise RuntimeError(
            "EVIDENCE_CHECK_FAILED: len(calls) == 4 and all((call['state'] == 'VALIDATED' for call in calls))"
        )
    for call in calls:
        metadata = call["provider_metadata_json"]
        if not metadata["transport"] == "OLLAMA_LOCAL_HTTP":
            raise RuntimeError("EVIDENCE_CHECK_FAILED: metadata['transport'] == 'OLLAMA_LOCAL_HTTP'")
        if not (metadata["actual_model_revision"] == MODEL_DIGEST and metadata["finish_reason"] == "stop"):
            raise RuntimeError(
                "EVIDENCE_CHECK_FAILED: metadata['actual_model_revision'] == MODEL_DIGEST and metadata['finish_reason'] == 'stop'"
            )
        if not call["usage_state"] == call["usage_json"]["state"] == "CONFIRMED":
            raise RuntimeError(
                "EVIDENCE_CHECK_FAILED: call['usage_state'] == call['usage_json']['state'] == 'CONFIRMED'"
            )
        if not 0 <= call["usage_json"]["input_tokens"] <= 3500:
            raise RuntimeError("EVIDENCE_CHECK_FAILED: 0 <= call['usage_json']['input_tokens'] <= 3500")
        if not 0 <= call["usage_json"]["output_tokens"] <= 800:
            raise RuntimeError("EVIDENCE_CHECK_FAILED: 0 <= call['usage_json']['output_tokens'] <= 800")
        if call["usage_json"]["cost_microusd"] is not None:
            raise RuntimeError("EVIDENCE_CHECK_FAILED: call['usage_json']['cost_microusd'] is None")
        if not call["output_hash"] == digest(call["output_json"]):
            raise RuntimeError("EVIDENCE_CHECK_FAILED: call['output_hash'] == digest(call['output_json'])")
    events = workflow.store.events(caller, SCOPE, current["id"], reauthorize=workflow._reauthorize)
    audit_head = verify_chain(events)
    tool_rounds = [event["payload_json"] for event in events if event["kind"] == "ANALYSIS_TOOL_READ"]
    if not len(tool_rounds) == 1:
        raise RuntimeError("EVIDENCE_CHECK_FAILED: len(tool_rounds) == 1")
    receipts = tool_rounds[0]["receipts"]
    if not all(item["transport"] == "IN_PROCESS" for item in receipts):
        raise RuntimeError(
            "EVIDENCE_CHECK_FAILED: all((item['transport'] == 'IN_PROCESS' for item in receipts))"
        )
    cases.append(
        {
            "fixture": fixture,
            "run_id": parent["id"],
            "analysis_id": current["id"],
            "technical_gate": gate,
            "analysis_state": current["state"],
            "known_validated_calls": 4,
            "source_reauthorized_read": True,
            "model_revision": MODEL_DIGEST,
            "actual_input_tokens": sum(call["usage_json"]["input_tokens"] for call in calls),
            "actual_output_tokens": sum(call["usage_json"]["output_tokens"] for call in calls),
            "provider_time_ms": sum(call["provider_metadata_json"]["timing_ms"] for call in calls),
            "actual_read_tool_receipts": receipts,
            "parent_audit_verified": True,
            "analysis_audit_verified": True,
            "analysis_audit_head": audit_head,
            "project_stage": "PROTOTYPE",
            "pending_approvals": len(parent["approvals"]),
            "action_receipts": 0,
            "mcp_protocol_activity": "RECORDED",
            "checker_cleanup": "REMOVED",
            "scanner_result": "PASS",
        }
    )
    event_path = evidence / f"live-{fixture}-attempt-03-events.json"
    if not event_path.exists():
        event_path.write_text(json.dumps(events, ensure_ascii=False, indent=2) + "\n")
replay = json.loads((evidence / "live-pass-replay-03.json").read_text())
first = json.loads((evidence / "live-pass-attempt-03.json").read_text())
if not (replay["analysis"] == first["analysis"] and replay["run_id"] == first["run_id"]):
    raise RuntimeError(
        "EVIDENCE_CHECK_FAILED: replay['analysis'] == first['analysis'] and replay['run_id'] == first['run_id']"
    )
result = {
    "checked_at_utc": datetime.now(UTC).isoformat(),
    "runtime_chain_status": "THREE_REAL_CASES_EXECUTED_AND_PERSISTED",
    "provenance": "ACTUAL_POSTGRES_DOCKER_SEMGREP_MCP_OLLAMA_LOCAL",
    "real_case_model_calls": 12,
    "cross_process_replay": "SAME_ANALYSIS_SAME_FOUR_CALLS",
    "cases": cases,
    "semantic_quality": {
        "method": "LIMITED_MANUAL_REVIEW_NOT_FULL_MODEL_BENCHMARK",
        "status": "NOT_FULLY_ACCEPTED",
        "observed_failure": {
            "fixture": "pass",
            "claim": "Unknown evidence status",
            "source": "aurora-evt-sop v1 general unknown-evidence rule",
            "reason": "The cited general rule does not establish an unknown measurement in this PASS instance.",
        },
        "gate_and_action_authority": "IMMUTABLE_DETERMINISTIC_GATE_AND_SEPARATE_HUMAN_APPROVAL",
    },
    "frozen_twelve_model_cases": "NOT_EXECUTED_BY_THIS_THREE_CASE_ACCEPTANCE",
    "production_acceptance": "NOT_ACCEPTED_DEV_ONLY",
    "external_business_writes": False,
}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(
    json.dumps(
        {
            "status": result["runtime_chain_status"],
            "model_calls": 12,
            "semantic_quality": result["semantic_quality"]["status"],
            "output": str(args.output),
        }
    )
)
