"""Read/replay completed S2C PostgreSQL and HTTP chains without inference."""

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
from forgeflow.local import SCOPE, load_identities
from forgeflow.s2c_runtime import S2CRuntimeFactory


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


async def execute(experiment):
    factory = S2CRuntimeFactory()
    identities = load_identities()
    token, caller = next((token, p) for token, p in identities.items() if "admin" in p.roles)
    summary = json.loads((experiment / "live/summary.json").read_text())
    rows = []
    with httpx.Client(base_url="http://127.0.0.1:8771", trust_env=False, timeout=15) as client:
        health = client.get("/s2c/health")
        require(health.status_code == 200, "S2C_API_NOT_READY")
        query, headers = SCOPE.model_dump(mode="json"), {"Authorization": "Bearer " + token}
        for result in summary["results"]:
            sid = result["scenario_id"]
            archived = json.loads((experiment / "raw-chains" / (sid + ".json")).read_text())
            original = archived["analysis"]
            current = factory.persistence.get(caller, SCOPE, original["id"], reauthorize=factory.reauthorize)
            require(digest(current) == digest(original), "PERSISTED_ANALYSIS_DRIFT")
            events = factory.persistence.events(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            require(digest(events) == digest(archived["events"]), "ANALYSIS_AUDIT_DRIFT")
            head = verify(events)
            parent = factory.source.get_run(caller, SCOPE, current["run_id"])
            require(digest(parent) == digest(archived["parent_after"]) == digest(archived["parent_before"]), "PARENT_DRIFT")
            require(factory.source.verify_events(caller, SCOPE, parent["id"]), "PARENT_AUDIT_INVALID")
            require(_mcp_projection(SCOPE, parent["id"], parent) == "RECORDED", "MCP_WITNESS_INVALID")
            require(parent["result_json"]["scan"]["status"] == "PASS", "SCANNER_NOT_PASS")
            require(parent["result_json"]["cleanup"] == "REMOVED", "SANDBOX_CLEANUP_NOT_CONFIRMED")
            records = factory.grounding.list(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            raw = factory.grounding.raw_list(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)
            require(digest(records) == digest(archived["grounding_records"]), "GROUNDING_DRIFT")
            require(len(raw) == len(records) == len(current["calls"]), "RAW_CALL_GRADE_COUNT_MISMATCH")
            for call in current["calls"]:
                frozen = archived["call_freeze_refs"][call["id"]]
                require(frozen["input_hash"] == call["input_hash"], "CALL_FREEZE_HASH_MISMATCH")
                require(digest({k: frozen["call"][k] for k in ("messages", "schema")}) == call["input_hash"], "ACTUAL_INPUT_MISMATCH")
                require(frozen["input_conservative_byte_bound"] <= 3500, "INPUT_BOUND_EXCEEDED")
                require(call["provider_metadata_json"].get("actual_model_revision") == MODEL_DIGEST, "ACTUAL_MODEL_MISMATCH")
                require(call["usage_state"] == "CONFIRMED", "USAGE_UNCONFIRMED")
                if call["phase"] in {"FINAL_REPORT", "SUMMARY"}:
                    require("status" not in call["output_json"] and "status" not in frozen["call"]["schema"]["properties"],
                            "MODEL_STATUS_FIELD_FORBIDDEN")
            if sid == "M03":
                checked, manifest, _, _, _ = factory.input_provider.checked(caller, SCOPE, parent["id"])
                require(digest(checked) == digest(parent), "DERIVED_CURRENT_CHECK_DRIFT")
                require(digest(manifest) == digest(archived["actual_check_witness"]["input_manifest"]), "DERIVED_MANIFEST_DRIFT")
                require(parent["assessment_json"]["status"] == "UNKNOWN", "DERIVED_GATE_MUST_BE_UNKNOWN")
            replay = await factory(sid).analyze(caller, SCOPE, parent["id"], current["idempotency_key"])
            require(digest(replay) == digest(original), "FRESH_PROCESS_REPLAY_DRIFT")
            response = client.post(f"/runs/{parent['id']}/grounded-analysis",
                                   headers={**headers, "Idempotency-Key": current["idempotency_key"]},
                                   json={"scope": query, "scenario_id": sid})
            require(response.status_code == 200 and digest(response.json()) == digest(original), "HTTP_REPLAY_DRIFT")
            read = client.get(f"/grounded-analyses/{current['id']}/records", params=query, headers=headers)
            require(read.status_code == 200 and digest(read.json()) == digest(records), "HTTP_RECORDS_DRIFT")
            unauth = client.get(f"/grounded-analyses/{current['id']}", params=query)
            foreign = client.get(f"/grounded-analyses/{current['id']}", params={**query, "tenant_id": "foreign"}, headers=headers)
            forged = client.post(f"/runs/{parent['id']}/grounded-analysis",
                                 headers={**headers, "Idempotency-Key": "forged-never-dispatched"},
                                 json={"scope": query, "scenario_id": sid, "role": "hardware", "status": "COMPLETE"})
            require(unauth.status_code == 401 and foreign.status_code in {403, 404} and forged.status_code == 422,
                    "HTTP_AUTHORITY_GUARD_FAILED")
            require(digest(factory.persistence.get(caller, SCOPE, current["id"], reauthorize=factory.reauthorize)) == digest(original),
                    "REPLAY_ADDED_MODEL_CALL")
            rows.append({"scenario": sid, "analysis_id": current["id"], "calls": len(current["calls"]),
                         "raw_journals": len(raw), "grades": len(records), "audit_head": head,
                         "audit_events": len(events), "technical_gate": parent["assessment_json"]["status"],
                         "server_state": current["state"], "projection": current["summary_json"]["server_projection"],
                         "stage": parent["project"]["current_stage"], "pending_approvals": len(parent["approvals"]),
                         "business_receipts": len(parent["receipts"]), "scan": "PASS", "sandbox_cleanup": "REMOVED",
                         "actual_mcp": "RECORDED", "fresh_process_and_http_replay": "IDENTICAL_NO_NEW_CALLS",
                         "http_guards": {"unauthenticated": unauth.status_code, "foreign_tenant": foreign.status_code,
                                         "forged_authority": forged.status_code}})
    inspector = inspect(factory.source.engine)
    tables = {name: inspector.get_foreign_keys(name) for name in ("grounding_records", "raw_provider_replies", "s2c_trusted_inputs")}
    return {"status": "S2C_PERSISTED_REAL_CHAINS_REAUTHORIZED_AND_REPLAYED", "database": factory.source.engine.dialect.name,
            "new_table_foreign_keys": tables, "api_health": health.json(), "cases": rows,
            "new_model_calls_by_this_verification": 0, "external_business_writes": False}


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
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    print(json.dumps({"status": result["status"], "scenarios": len(result["cases"]), "new_model_calls": 0}))


if __name__ == "__main__":
    main()
