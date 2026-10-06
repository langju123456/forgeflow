"""Independent read/replay of completed local evidence; no new model generation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from selection_audit_evidence import verify
from sqlalchemy import inspect

from forgeflow.analysis_context import _mcp_projection
from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.contracts import digest
from forgeflow.grounded_selection_runtime import SelectionRuntimeFactory
from forgeflow.local import SCOPE, load_identities


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


async def execute(experiment: Path):
    factory = SelectionRuntimeFactory()
    identities = load_identities()
    token, caller = next((token, p) for token, p in identities.items() if "admin" in p.roles)
    summary = json.loads((experiment / "live/summary.json").read_text())
    rows = []
    with httpx.Client(base_url="http://127.0.0.1:8770", trust_env=False, timeout=5) as client:
        health = client.get("/grounded/selection-health")
        require(health.status_code == 200, "V2_API_NOT_READY")
        query = SCOPE.model_dump(mode="json")
        headers = {"Authorization": "Bearer " + token}
        for result in summary["results"]:
            archived = json.loads((experiment / "raw-chains" / (result["scenario_id"] + ".json")).read_text())
            original = archived["analysis"]
            workflow = factory(result["scenario_id"])
            current = factory.persistence.get(caller, SCOPE, original["id"], reauthorize=factory.reauthorize)
            require(current == original, "PERSISTED_ANALYSIS_DRIFT")
            events = factory.persistence.events(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            require(events == archived["events"], "PERSISTED_ANALYSIS_AUDIT_DRIFT")
            audit_head = verify(events)
            parent = factory.source.get_run(caller, SCOPE, current["run_id"])
            require(parent == archived["parent_after"] == archived["parent_before"], "PARENT_DRIFT")
            require(factory.source.verify_events(caller, SCOPE, parent["id"]), "PARENT_AUDIT_INVALID")
            require(_mcp_projection(SCOPE, parent["id"], parent) == "RECORDED", "MCP_WITNESS_INVALID")
            require(parent["result_json"]["scan"]["status"] == "PASS", "SCANNER_NOT_PASS")
            require(parent["result_json"]["cleanup"] == "REMOVED", "SANDBOX_CLEANUP_NOT_CONFIRMED")
            records = factory.grounding.list(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            raw = factory.grounding.raw_list(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            require(records == archived["grounding_records"] and len(raw) == len(current["calls"]), "RAW_GRADE_LEDGER_DRIFT")
            freezes = archived["call_freeze_refs"]
            for call in current["calls"]:
                frozen = freezes[call["id"]]
                require(frozen["input_hash"] == call["input_hash"], "CALL_FREEZE_HASH_MISMATCH")
                require(frozen["input_conservative_byte_bound"] <= 3500, "CALL_INPUT_BOUND_EXCEEDED")
                actual_input = {key: frozen["call"][key] for key in ("messages", "schema")}
                require(digest(actual_input) == call["input_hash"], "ACTUAL_PROMPT_SCHEMA_MISMATCH")
                require(call["provider_metadata_json"].get("actual_model_revision") == MODEL_DIGEST, "ACTUAL_MODEL_MISMATCH")
                require(call["usage_state"] == "CONFIRMED", "USAGE_UNCONFIRMED")
            replay = await workflow.analyze(caller, SCOPE, parent["id"], current["idempotency_key"])
            require(replay == original, "FRESH_PROCESS_REPLAY_DRIFT")
            response = client.post(
                f"/runs/{parent['id']}/grounded-analysis",
                headers={**headers, "Idempotency-Key": current["idempotency_key"]},
                json={"scope": query, "scenario_id": result["scenario_id"]},
            )
            require(response.status_code == 200 and response.json() == original, "HTTP_REPLAY_DRIFT")
            read = client.get(f"/grounded-analyses/{current['id']}/records", params=query, headers=headers)
            require(read.status_code == 200 and read.json() == records, "HTTP_RECORDS_DRIFT")
            unauthenticated = client.get(f"/grounded-analyses/{current['id']}", params=query)
            foreign = client.get(
                f"/grounded-analyses/{current['id']}", params={**query, "tenant_id": "foreign-tenant"}, headers=headers
            )
            forged = client.post(
                f"/runs/{parent['id']}/grounded-analysis",
                headers={**headers, "Idempotency-Key": "forged-never-dispatched"},
                json={"scope": query, "scenario_id": result["scenario_id"], "role": "hardware", "approval_decision": "APPROVED"},
            )
            require(unauthenticated.status_code == 401 and foreign.status_code in {403, 404}
                    and forged.status_code == 422, "HTTP_AUTHORITY_GUARD_FAILED")
            require(factory.persistence.get(caller, SCOPE, current["id"], reauthorize=factory.reauthorize) == original,
                    "REPLAY_ADDED_MODEL_CALL")
            rows.append({"scenario": result["scenario_id"], "analysis_id": current["id"],
                         "stored_calls": len(current["calls"]), "raw_journals": len(raw), "grades": len(records),
                         "fresh_process_and_http_replay": "IDENTICAL_NO_NEW_CALLS",
                         "http_guards": {"unauthenticated": unauthenticated.status_code,
                                         "foreign_tenant": foreign.status_code, "forged_role": forged.status_code},
                         "actual_input_tokens": sum(c["usage_json"]["input_tokens"] for c in current["calls"]),
                         "actual_output_tokens": sum(c["usage_json"]["output_tokens"] for c in current["calls"]),
                         "provider_time_ms": sum(c["provider_metadata_json"]["timing_ms"] for c in current["calls"]),
                         "technical_gate": parent["assessment_json"]["status"],
                         "stage": parent["project"]["current_stage"], "pending_approvals": len(parent["approvals"]),
                         "business_receipts": len(parent["receipts"]), "scan": "PASS", "sandbox_cleanup": "REMOVED",
                         "actual_mcp": "RECORDED", "parent_audit": "VERIFIED"})
            rows[-1]["analysis_audit"] = {"status": "VERIFIED", "events": len(events), "head": audit_head}
    inspector = inspect(factory.source.engine)
    tables = {name: inspector.get_foreign_keys(name) for name in ("grounding_records", "raw_provider_replies")}
    return {"status": "PERSISTED_REAL_CHAINS_REAUTHORIZED_AND_REPLAYED", "database": factory.source.engine.dialect.name,
            "new_table_foreign_keys": tables, "api_health": health.json(), "cases": rows,
            "new_model_calls_by_this_verification": 0, "external_business_writes": False,
            "model_quality": {key: summary[key] for key in ("structural_passes", "semantic_passes", "real_model_calls")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Use a new evidence output path")
    try:
        result = asyncio.run(execute(args.experiment))
    except Exception as error:
        print(json.dumps({"status": "VALIDATION_FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    with args.output.open("x") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)
        file.write("\n")
    print(json.dumps({"status": result["status"], "scenarios": len(result["cases"]), "new_model_calls": 0}))


if __name__ == "__main__":
    main()
