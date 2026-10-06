"""Trusted local S2C experiment adapter; never overwrite earlier experiments.

Execution is explicit and uses the already authorized local model, PostgreSQL,
scanner, Docker and official MCP chain. This script does not download a model,
read private keys, approve a stage, or dispatch a business action.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from s2c_evaluate import QUALITY_SCOPE, acceptance_summary, frozen_plan, run_faults, run_live

from forgeflow.analysis_provider import MODEL, MODEL_DIGEST, OllamaProvider
from forgeflow.contracts import canonical, digest
from forgeflow.local import SCOPE, load_identities
from forgeflow.s2c_contracts import REVISION, schema
from forgeflow.s2c_runtime import S2CRuntimeFactory
from forgeflow.s2c_workflow import PROMPT_REVISION

ROOT = Path(__file__).resolve().parents[1]


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


async def execute(output: Path, key: str, *, factory_type=S2CRuntimeFactory, provider=None):
    if output.exists():
        raise RuntimeError("USE_NEW_EXPERIMENT_DIRECTORY")
    plan = frozen_plan()
    output.mkdir(parents=True)
    provider = provider if provider is not None else OllamaProvider()
    preflight = await provider.preflight()
    write_new(output / "preflight.json", preflight)
    if preflight.get("status") != "VERIFIED":
        raise RuntimeError("PINNED_LOCAL_MODEL_NOT_READY")

    def freeze_call(call):
        material = {"messages": call["messages"], "schema": call["schema"]}
        write_new(output / "call-freezes" / (call["call_id"] + ".json"), {
            "frozen_before_provider_dispatch": True, "input_hash": digest(material),
            "input_conservative_byte_bound": len(canonical(material).encode()) + 384, "call": call,
        })

    factory = factory_type(provider=provider, freeze_call=freeze_call)
    source = factory.source
    caller = next(principal for principal in load_identities().values() if "admin" in principal.roles)
    files = sorted((ROOT / "src/forgeflow").glob("*.py")) + sorted((ROOT / "skills").glob("*/SKILL.md"))
    files += [ROOT / "scripts/s2c_evaluate.py", ROOT / "scripts/s2c_live_evaluate.py"]
    experiment = {
        "schema_version": "forgeflow-s2c-experiment-freeze-v1", "frozen_at_utc": datetime.now(UTC).isoformat(),
        "application_revision": REVISION, "prompt_revision": PROMPT_REVISION, "input_dataset": plan,
        "actual_scope": SCOPE.model_dump(mode="json"), "model": MODEL, "model_revision": MODEL_DIGEST,
        "quality_scope": QUALITY_SCOPE, "model_predicts_completion_status": False,
        "scope_change_from_s2b": "MODEL_STATUS_FIELD_REMOVED; SERVER_DERIVES_STATE_FROM_AUTHORIZED_CATALOG",
        "runtime_phase_extension": {
            "actual_phases": ["PLAN", "CONTEXT_PROPOSAL", "FINAL_REPORT", "SUMMARY"],
            "original_M01_including_blocked_variant": "FOUR_PLANNED_PHASES_IN_FROZEN_CASE",
            "original_M02_M03_M04": "FROZEN_PM_CONTEXT_REPORT_OBLIGATIONS_PLUS_AUTHORIZED_RUNTIME_SUPERVISOR_PLAN_SUMMARY",
            "original_dataset_and_gold_changed": False,
        },
        "M03_hardware_precondition": "NEW_DERIVED_PARENT_ACTUAL_MCP_HASH_VERIFIED_SANDBOX_CHECK_AT_FROZEN_CLOCK",
        "profile": {"calls_max": 4, "input_conservative_upper_bound": 3500, "output_max": 800,
                    "call_seconds_max": 60, "run_seconds_max": 180, "concurrency": 1,
                    "automatic_retries": 0, "billing": "LOCAL_NO_BILLING"},
        "authorization": {"request": "继续 不要停 把项目跑通", "scope": "EXISTING_LOCAL_MODEL_AND_SYNTHETIC_FIXTURES_ONLY"},
        "source_files_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
        "base_output_schemas": {phase: schema(phase) for phase in ("plan", "context", "report", "summary")},
        "actual_output_schemas": "EXACT_SCHEMA_IN_EACH_PRE_DISPATCH_CALL_FREEZE",
        "automatic_repair_or_retry": False, "external_business_writes": False,
    }
    write_new(output / "experiment-freeze.json", experiment)

    async def callback(scenario):
        sid = scenario["scenario_id"]
        print(json.dumps({"scenario": sid, "status": "STARTING_NEW_S2C_REAL_CHAIN"}), flush=True)
        parent = factory.submit_parent(caller, sid, f"s2c-parent:{key}:{sid}")
        await factory.check_parent(SCOPE, parent)
        before = source.get_run(caller, SCOPE, parent["id"])
        if sid == "M03":
            loaded = factory.input_provider.load_run(caller, SCOPE, parent["id"])
            actual_input, trusted_input = loaded["manifest"], loaded["witness"]
        else:
            fixture = before["request_json"]["fixture_id"]
            actual_input = json.loads((ROOT / "fixtures" / (fixture + ".json")).read_text())
            trusted_input = None
        actual_check_witness = {"input_manifest": actual_input, "evaluation_clock": scenario["inputs"]["evaluation_clock"],
                                "trusted_input": trusted_input}
        workflow = factory(sid)
        bundle = workflow.context.build(caller, SCOPE, parent["id"], role="supervisor")
        case_freeze = {
            "scenario_id": sid, "parent_run_id": parent["id"], "parent_input_hash": before["snapshot_json"]["input_hash"],
            "context_snapshot": bundle.to_material(), "claim_catalog": workflow.context.catalog(bundle),
            "source_origin_refs": workflow.context.frozen_source_refs(bundle), "experiment_freeze_hash": digest(experiment),
            "actual_check_witness": actual_check_witness,
        }
        write_new(output / "context-freezes" / (sid + ".json"), case_freeze)
        # The parent is already checked before inference; analysis has no sandbox
        # permission and cannot alter its input, Gate, approvals or actions.
        analysis = await workflow.analyze(caller, SCOPE, parent["id"], f"s2c-analysis:{key}:{sid}")
        after = source.get_run(caller, SCOPE, parent["id"])
        records = workflow.grounding_store.list(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        raw_records = workflow.grounding_store.raw_list(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        events = workflow.store.events(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        evidence = {
            "parent_before": before, "parent_after": after, "parent_events": source.events(caller, SCOPE, parent["id"]),
            "actual_check_witness": actual_check_witness, "analysis": analysis, "events": events,
            "grounding_records": records, "freeze": case_freeze, "source_origin_refs": case_freeze["source_origin_refs"],
            "provider_provenance": "OLLAMA_LOCAL_HTTP", "raw_provider_journal": raw_records,
            "raw_provider_replies": {record["call_id"]: record["provider_reply"] for record in raw_records},
            "call_freeze_refs": {call["id"]: json.loads((output / "call-freezes" / (call["id"] + ".json")).read_text())
                                 for call in analysis["calls"]},
        }
        write_new(output / "raw-chains" / (sid + ".json"), evidence)
        print(json.dumps({"scenario": sid, "status": analysis["state"], "real_calls": len(analysis["calls"])}), flush=True)
        return evidence

    live = await run_live(callback, output / "live")
    faults = await run_faults(output / "faults")
    result = acceptance_summary(live, faults)
    write_new(output / "frozen-acceptance-summary.json", result)
    for name, expected in experiment["source_files_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("EXPERIMENT_CODE_DRIFTED")
    write_new(output / "completion.json", {
        "status": "COMPLETED_WITH_REPORTED_RESULTS", "executed_real_scenarios": live["executed_scenario_count"],
        "confirmed_model_calls": live["confirmed_model_calls"], "ambiguous_dispatch_attempts": live["ambiguous_dispatch_attempts"],
        "selection_passes": live["semantic_passes"], "server_state_passes": live["server_state_passes"],
        "full_frozen_counts": result["counts"], "code_and_skills_unchanged": True,
        "automatic_retries": 0, "case_count": 12, "scenario_count": 13,
    })
    return {"live": live, "faults": faults, "acceptance": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    output_was_present = args.output.exists()
    try:
        result = asyncio.run(execute(args.output, args.key))
    except Exception as error:
        failure = {"status": "STOPPED_WITH_FAILURE_EVIDENCE", "error_type": type(error).__name__,
                   "automatic_retries": 0, "existing_evidence_preserved": True}
        if not output_was_present and args.output.exists() and not (args.output / "failure.json").exists():
            write_new(args.output / "failure.json", failure)
        print(json.dumps(failure), flush=True)
        raise SystemExit(1) from None
    print(json.dumps({"confirmed_model_calls": result["live"]["confirmed_model_calls"],
                      "frozen_denominator": 13, "frozen_counts": result["acceptance"]["counts"]}), flush=True)


if __name__ == "__main__":
    main()
