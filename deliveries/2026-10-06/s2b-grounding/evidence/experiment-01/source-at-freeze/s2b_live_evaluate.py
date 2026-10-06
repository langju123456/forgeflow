"""Execute the authorized local frozen evaluation, preserving a new experiment.

Only existing local PostgreSQL, scanner/Docker/MCP and the pinned Ollama model
are used. No model download, cloud credential, approval or business dispatch.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from s2b_evaluate import frozen_plan, run_live

from forgeflow.analysis_provider import MODEL, MODEL_DIGEST, OllamaProvider
from forgeflow.contracts import RunRequest, canonical, digest
from forgeflow.grounded_contracts import REVISION, schema
from forgeflow.grounded_runtime import GroundedRuntimeFactory
from forgeflow.local import SCOPE, load_identities, load_workflow

ROOT = Path(__file__).resolve().parents[1]


def write_new(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


async def execute(output: Path, key: str):
    if output.exists():
        raise RuntimeError("USE_NEW_EXPERIMENT_DIRECTORY")
    plan = frozen_plan()  # Verify old bytes before any runtime or model operation.
    output.mkdir(parents=True)
    provider = OllamaProvider()
    preflight = await provider.preflight()
    write_new(output / "preflight.json", preflight)
    if preflight.get("status") != "VERIFIED":
        raise RuntimeError("PINNED_LOCAL_MODEL_NOT_READY")

    def freeze_call(call):
        material = {"messages": call["messages"], "schema": call["schema"]}
        write_new(output / "call-freezes" / (call["call_id"] + ".json"), {
            "frozen_before_provider_dispatch": True,
            "input_hash": digest(material),
            "input_conservative_byte_bound": len(canonical(material).encode()) + 384,
            "call": call,
        })

    factory = GroundedRuntimeFactory(provider=provider, freeze_call=freeze_call)
    source = factory.source
    caller = next(p for p in load_identities().values() if "admin" in p.roles)
    files = sorted((ROOT / "src/forgeflow").glob("*.py")) + sorted((ROOT / "skills").glob("*-grounded/SKILL.md"))
    files += [ROOT / "scripts/s2b_live_evaluate.py", ROOT / "scripts/s2b_evaluate.py"]
    experiment = {
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "application_revision": REVISION,
        "input_dataset": plan,
        "actual_scope": SCOPE.model_dump(mode="json"),
        "frozen_scope_mapping": "EXPLICIT_LOCAL_SYNTHETIC_EXPORT_GRANT_WITH_SOURCE_WITNESSES",
        "model": MODEL, "model_revision": MODEL_DIGEST,
        "profile": {"calls_max": 4, "input_conservative_upper_bound": 3500, "output_max": 800,
                    "call_seconds_max": 60, "run_seconds_max": 180, "concurrency": 1,
                    "automatic_retries": 0, "billing": "LOCAL_NO_BILLING"},
        "authorization": {"request": "继续 不要停 把项目跑通",
                          "source_thread_id": "01a0f16e-26d9-769a-95f5-3aa61095503b",
                          "scope": "EXISTING_LOCAL_MODEL_AND_SYNTHETIC_FIXTURES_ONLY"},
        "source_files_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in files},
        "output_schemas": {phase: schema(phase) for phase in ("plan", "context", "report", "summary")},
        "semantic_task": "CONSTRAINED_EVIDENCE_SELECTION_NOT_OPEN_ENDED_REASONING_BENCHMARK",
        "M03_hardware_gold_limit": "DERIVED_OVERLAY_SANDBOX_NOT_EXECUTED; BASE_ACTUAL_GATE_REMAINS_PASS",
        "external_business_writes": False,
    }
    write_new(output / "experiment-freeze.json", experiment)

    async def callback(scenario):
        sid = scenario["scenario_id"]
        print(json.dumps({"scenario": sid, "status": "STARTING_REAL_CHAIN"}), flush=True)
        fixture = Path(scenario["inputs"]["fixture_ref"]["path"]).stem
        parent = source.submit(caller, RunRequest(scope=SCOPE, fixture_id=fixture), f"s2b-parent:{key}:{sid}")
        if parent["assessment_json"] is None:
            await load_workflow(source).check(SCOPE, parent["id"])
        before = source.get_run(caller, SCOPE, parent["id"])
        workflow = factory(sid)
        bundle = workflow.context.build(caller, SCOPE, parent["id"], role="supervisor")
        case_freeze = {"scenario_id": sid, "parent_run_id": parent["id"],
                       "parent_input_hash": before["snapshot_json"]["input_hash"],
                       "context_snapshot": bundle.to_material(), "claim_catalog": workflow.context.catalog(bundle),
                       "source_origin_refs": workflow.context.frozen_source_refs(bundle),
                       "experiment_freeze_hash": digest(experiment)}
        write_new(output / "context-freezes" / (sid + ".json"), case_freeze)
        analysis = await workflow.analyze(caller, SCOPE, parent["id"], f"s2b-analysis:{key}:{sid}")
        after = source.get_run(caller, SCOPE, parent["id"])
        records = workflow.grounding_store.list(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        raw_records = workflow.grounding_store.raw_list(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        events = workflow.store.events(caller, SCOPE, analysis["id"], reauthorize=workflow._reauthorize)
        evidence = {"parent_before": before, "parent_after": after, "analysis": analysis,
                    "events": events, "grounding_records": records, "freeze": case_freeze,
                    "source_origin_refs": case_freeze["source_origin_refs"],
                    "provider_provenance": "OLLAMA_LOCAL_HTTP",
                    "raw_provider_replies": {record["call_id"]: record["provider_reply"] for record in raw_records},
                    "raw_provider_journal": raw_records,
                    "call_freeze_refs": {call["id"]: json.loads((output / "call-freezes" / (call["id"] + ".json")).read_text())
                                         for call in analysis["calls"]}}
        write_new(output / "raw-chains" / (sid + ".json"), evidence)
        print(json.dumps({"scenario": sid, "status": analysis["state"], "real_calls": len(analysis["calls"])}), flush=True)
        return evidence

    result = await run_live(callback, output / "live")
    # Confirm that neither trusted code nor skills changed during inference.
    for name, expected in experiment["source_files_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("EXPERIMENT_CODE_DRIFTED")
    write_new(output / "completion.json", {"status": "COMPLETED_WITH_REPORTED_RESULTS",
                                          "executed_scenarios": result["executed_scenario_count"],
                                          "real_model_calls": result["real_model_calls"],
                                          "semantic_passes": result["semantic_passes"],
                                          "code_and_skills_unchanged": True})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args.output, args.key))
    except Exception as error:
        print(json.dumps({"status": "STOPPED_WITH_FAILURE_EVIDENCE", "error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps({key: result[key] for key in (
        "executed_scenario_count", "structural_passes", "semantic_passes", "real_model_calls"
    )}), flush=True)


if __name__ == "__main__":
    main()
