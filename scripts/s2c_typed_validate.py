"""Independent canonical replay of all five frozen S2C model-behavior chains.

The original experiment and evaluator remain immutable. Python True == 1 is
not a typed evidence equality: every critical comparison below uses canonical
JSON bytes. This supplemental review makes no provider/runtime/network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_SCENARIOS = ("M01", "M01-blocked", "M02", "M03", "M04")
FAULT_SCENARIOS = tuple(f"M{index:02d}" for index in range(5, 13))
REVISION = "s2c-canonical-supplement-v1"


def _runner():
    spec = importlib.util.spec_from_file_location("s2c_canonical_original_evaluator", ROOT / "scripts/s2c_evaluate.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("ORIGINAL_EVALUATOR_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_equal(actual, expected):
    from forgeflow.contracts import canonical

    try:
        return canonical(actual).encode() == canonical(expected).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def _require(condition, code):
    if not condition:
        raise RuntimeError(code)


def _scope(row):
    return {key: row[key] for key in ("tenant_id", "workspace_id", "project_id")}


def _expected_catalog(scenario, snapshot, parent):
    """Derive exact scalar types and source authorities from frozen inputs."""
    from forgeflow.contracts import digest
    from forgeflow.gate import RULES, evaluate

    manifest = _runner()._legacy()._checker().load_manifest(scenario)
    instant = datetime.fromisoformat(scenario["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
    assessment = evaluate(manifest, {"status": "SUCCEEDED", "cleanup": "REMOVED"}, now=instant)
    checks = {row["rule"]: row["status"] for row in assessment["checks"]}
    hardware = [source for source in snapshot["sources"] if source["kind"] == "run" and source["id"] == parent["id"]]
    _require(len(hardware) == 1, "CANONICAL_HARDWARE_BINDING_MISSING")
    observations, concerns = [], []
    for metric in RULES["required"]:
        item = manifest["metrics"].get(metric)
        evidence_state = "MISSING" if item is None else (
            "CONFLICT" if item.get("conflict") else
            "EXPIRED" if datetime.fromisoformat(item["valid_until"].replace("Z", "+00:00")) <= instant else "MEASURED")
        row = {"metric": metric, "value": None if item is None else item.get("value"),
               "check_state": checks[metric], "evidence_state": evidence_state,
               "source_ref": hardware[0]["ref"], "authority": "HARDWARE_CHECK"}
        observations.append({"id": "c_" + digest(row)[:16], **row})
        if checks[metric] != "SATISFIED":
            concern = {"source_ref": hardware[0]["ref"],
                       "kind": f"{metric}:{'VIOLATED' if checks[metric] == 'VIOLATED' else evidence_state}"}
            concerns.append({"id": "c_" + digest(concern)[:16], **concern})
    if scenario["scenario_id"] == "M04":
        source = [item for item in snapshot["sources"] if item["id"] == "fixture:untrusted-injection-v1"]
        _require(len(source) == 1, "CANONICAL_INJECTION_SOURCE_MISSING")
        concern = {"source_ref": source[0]["ref"], "kind": "UNTRUSTED_INSTRUCTION"}
        concerns.append({"id": "c_" + digest(concern)[:16], **concern})
    return {"observations": observations, "concerns": concerns}


def _expected_witness(scenario, scope):
    """Independent typed reconstruction of the frozen M03 import witness."""
    from forgeflow.contracts import digest
    from forgeflow.gate import RULE_HASH
    from forgeflow.policy import POLICY_HASH
    from forgeflow.skills import SkillRegistry
    from forgeflow.templates import template_hash

    manifest = _runner()._legacy()._checker().load_manifest(scenario)
    source = scenario["inputs"]["context"][0]
    return {
        "provider_id": "frozen-synthetic-m03", "provider_revision": "s2c-derived-check-v1", "scenario_id": "M03",
        "dataset_sha256": hashlib.sha256((ROOT / "preparation/s2/model-evaluation/cases.json").read_bytes()).hexdigest(),
        "case_hash": digest({key: value for key, value in scenario.items() if key != "scenario_id"}),
        "base_fixture": scenario["inputs"]["fixture_ref"], "overlays": scenario["inputs"]["evidence_overlays"],
        "source_id": source["source_id"], "source_version": source["version"],
        "source_content_hash": hashlib.sha256(source["content"].encode()).hexdigest(), "manifest_hash": digest(manifest),
        "source_ref": manifest["source_ref"], "synthetic": True, "classification": 0,
        "evaluation_clock": scenario["inputs"]["evaluation_clock"], "evaluation_clock_kind": "FROZEN_SYNTHETIC",
        "scope_mapping": {"from": scenario["gold"]["expected_scope"], "to": scope,
                          "purpose": "EXACT_FROZEN_SYNTHETIC_CHECK_IMPORT"},
        "template_id": "evt-check", "resource_profile": "checker-v1",
        "code_hash": template_hash("evt-check"), "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH,
        "skill_hash": SkillRegistry(ROOT / "skills").load("hardware-evt", "hardware").sha256,
        "valid_until": "2026-11-05T00:00:00Z",
    }


def strict_guards(scenario, evidence):
    from forgeflow.analysis_context import SYNTHETIC_SCOPE
    from forgeflow.analysis_contracts import strict_json_object
    from forgeflow.gate import evaluate

    parent = evidence["parent_before"]
    manifest = _runner()._legacy()._checker().load_manifest(scenario)
    clock = datetime.fromisoformat(scenario["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
    expected_assessment = evaluate(manifest, parent["result_json"], now=clock)
    guards = {
        "parent_unchanged_canonical": canonical_equal(parent, evidence["parent_after"]),
        "frozen_input_canonical": canonical_equal(evidence["actual_check_witness"]["input_manifest"], manifest),
        "actual_assessment_canonical": canonical_equal(parent["assessment_json"], expected_assessment),
        "runtime_scope_canonical": canonical_equal(_scope(parent), SYNTHETIC_SCOPE.model_dump(mode="json")),
        "derived_witness_canonical": True,
    }
    if scenario["inputs"]["evidence_overlays"]:
        expected_witness = _expected_witness(scenario, _scope(parent))
        guards["derived_witness_canonical"] = all(canonical_equal(value, expected_witness) for value in (
            evidence["actual_check_witness"]["trusted_input"], parent["snapshot_json"].get("trusted_input"),
            parent["result_json"].get("trusted_input"), parent["request_json"].get("trusted_input")))
    records = {row["call_id"]: row for row in evidence["grounding_records"]}
    journals = {row["call_id"]: row for row in evidence["raw_provider_journal"]}
    per_call = []
    for call in evidence["analysis"]["calls"]:
        record, journal = records.get(call["id"]), journals.get(call["id"])
        if record is None or journal is None:
            per_call.append({"call_id": call["id"], "status": "FAIL", "checks": {"complete_record_binding": False}})
            continue
        snapshot = _runner()._recipient_snapshot(call)
        expected_catalog = _expected_catalog(scenario, snapshot, parent)
        reply = journal["provider_reply"]
        raw = reply.get("content_utf8") if isinstance(reply.get("content_utf8"), str) else reply.get("content")
        try:
            parsed = strict_json_object(raw)
        except Exception:
            parsed = None
        checks = {
            "catalog_canonical_gold": canonical_equal(record["claim_catalog"], expected_catalog),
            "journal_catalog_canonical_gold": canonical_equal(journal["claim_catalog"], expected_catalog),
            "raw_call_output_canonical": parsed is not None and canonical_equal(call.get("output_json"), parsed),
            "raw_reply_map_canonical": canonical_equal(reply, evidence["raw_provider_replies"].get(call["id"])),
            "raw_grade_reply_canonical": canonical_equal(record["raw_reply"], raw),
            "provider_provenance_canonical": canonical_equal(record["provider_provenance"], {
                **{key: reply.get(key) for key in ("transport", "actual_model_revision", "finish_reason", "status",
                                                "dispatched", "completion_confirmed")}, "input_hash": call["input_hash"]}),
            "frozen_source_records_canonical": canonical_equal(snapshot["grounding"]["source_records"], [
                {key: value for key, value in source.items() if key != "content"} for source in scenario["inputs"]["context"]]),
        }
        if call["phase"] in {"FINAL_REPORT", "SUMMARY"}:
            rendered = record.get("rendered_report") or {}
            checks["rendered_observations_canonical"] = canonical_equal(rendered.get("observations"), expected_catalog["observations"])
            checks["rendered_concerns_canonical"] = canonical_equal(rendered.get("concerns"), expected_catalog["concerns"])
            checks["rendered_selection_canonical"] = parsed is not None and canonical_equal(rendered.get("validated_selection"), parsed)
        if call["phase"] == "SUMMARY":
            from forgeflow.contracts import digest

            expected_projection = {
                "status": "COMPLETE" if all(row["check_state"] == "SATISFIED" and row["evidence_state"] == "MEASURED"
                                            for row in expected_catalog["observations"]) else "INCOMPLETE",
                "status_authority": "SERVER_DERIVED_AUTHORIZED_CATALOG", "catalog_hash": digest(expected_catalog),
            }
            persisted_summary = evidence["analysis"].get("summary_json") or {}
            checks["persisted_summary_selection_canonical"] = parsed is not None and canonical_equal(persisted_summary.get("selection"), parsed)
            checks["persisted_server_projection_canonical"] = canonical_equal(persisted_summary.get("server_projection"), expected_projection)
        per_call.append({"call_id": call["id"], "phase": call["phase"],
                         "status": "PASS" if all(checks.values()) else "FAIL", "checks": checks})
    return {"status": "PASS" if all(guards.values()) and len(per_call) == 4
            and all(row["status"] == "PASS" for row in per_call) else "FAIL", "checks": guards, "per_call": per_call}


def assess_live(scenario, evidence):
    """Preserve original grading and publish a separately labeled strict result."""
    original = _runner().assess_live(scenario, evidence)
    strict = strict_guards(scenario, evidence)
    return {
        "schema_version": REVISION, "scenario_id": scenario["scenario_id"], "case_id": scenario["id"],
        "provenance": "POSTHOC_CANONICAL_REVIEW_OF_SAME_REAL_CHAIN", "model_calls_added": 0,
        "original_model_quality_status": original["model_quality_status"],
        "original_full_frozen_gold_status": original["full_frozen_gold_status"],
        "canonical_typed_validation": strict["status"], "strict_guards": strict,
        "full_frozen_gold_status": "PASS" if original["full_frozen_gold_status"] == strict["status"] == "PASS" else "FAIL",
        "model_quality_scope": original["model_quality_scope"], "model_predicts_completion_status": False,
        "confirmed_model_calls_in_original_chain": original["confirmed_model_calls"],
        "ambiguous_dispatch_attempts_in_original_chain": original["ambiguous_dispatch_attempts"],
        "assessment_change": "CANONICAL_TYPED_EQUALITY_SUPPLEMENTS_FROZEN_PYTHON_EQUALITY_CHECKS",
    }


def _read_with_hash(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "DUPLICATE_EVIDENCE_KEY")
            result[key] = value
        return result

    _require(path.is_file() and not path.is_symlink(), "EVIDENCE_REGULAR_FILE_REQUIRED")
    raw = path.read_bytes()
    _require(len(raw) <= 16 * 1024 * 1024, "EVIDENCE_FILE_TOO_LARGE")
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    return data, hashlib.sha256(raw).hexdigest()


def _read(path):
    return _read_with_hash(path)[0]


def _write_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def review_experiment(experiment_dir: Path, output_dir: Path):
    _require(not output_dir.exists(), "USE_NEW_TYPED_REVIEW_DIRECTORY")
    evidence_hashes = {}

    def read_source(path):
        _require(path.resolve().is_relative_to(experiment_dir.resolve()), "EVIDENCE_PATH_ESCAPES_EXPERIMENT")
        data, raw_hash = _read_with_hash(path)
        evidence_hashes[str(path.relative_to(experiment_dir))] = raw_hash
        return data

    actual_files = {path.name for path in (experiment_dir / "raw-chains").iterdir()}
    _require(actual_files == {sid + ".json" for sid in MODEL_SCENARIOS}, "ALL_FIVE_RAW_CHAINS_REQUIRED")
    freeze = read_source(experiment_dir / "experiment-freeze.json")
    expected_runner_hash = freeze["source_files_sha256"]["scripts/s2c_evaluate.py"]
    _require(hashlib.sha256((ROOT / "scripts/s2c_evaluate.py").read_bytes()).hexdigest() == expected_runner_hash,
             "FROZEN_ORIGINAL_EVALUATOR_DRIFTED")
    integrity, scenarios = _runner().load_scenarios()
    faults = read_source(experiment_dir / "faults/summary.json")
    fault_rows = faults["faults"]
    _require(tuple(row["scenario_id"] for row in fault_rows) == FAULT_SCENARIOS
             and all(row["provenance"] == "TEST_DRIVER" and row["real_model_calls"] == 0 for row in fault_rows),
             "ALL_EIGHT_ORIGINAL_FAULTS_REQUIRED")
    rows = []
    for scenario in scenarios:
        sid = scenario["scenario_id"]
        if sid not in MODEL_SCENARIOS:
            continue
        path = experiment_dir / "raw-chains" / (sid + ".json")
        rows.append(assess_live(scenario, read_source(path)))
    summary = {
        "schema_version": REVISION, "status": "POSTHOC_CANONICAL_REVIEW_COMPLETED", "dataset_integrity": integrity,
        "source_experiment": str(experiment_dir.resolve()), "case_count": 12, "scenario_count": 13,
        "real_model_scenarios_reviewed": 5, "original_test_driver_scenarios": 8, "model_calls_added": 0,
        "original_results_preserved": True, "frozen_original_evaluator_sha256": expected_runner_hash,
        "supplemental_checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_evidence_files_sha256": evidence_hashes,
        "canonical_model_scenario_passes": sum(row["full_frozen_gold_status"] == "PASS" for row in rows),
        "original_fault_passes": sum(row["full_frozen_gold_status"] == "PASS" for row in fault_rows),
        "results": rows,
        "frozen_acceptance_counts": {
            "PASS": sum(row["full_frozen_gold_status"] == "PASS" for row in rows + fault_rows),
            "FAIL": sum(row["full_frozen_gold_status"] != "PASS" for row in rows + fault_rows),
        },
        "known_original_evaluator_limitation": "PYTHON_EQUALITY_ACCEPTS_BOOL_INT_FLOAT_ALIASES_IN_CRITICAL_DICTS",
        "limit": "SUPPLEMENTAL_TYPED_REPLAY; NO_NEW_MODEL_STATUS_PREDICTION_OR_GENERAL_REASONING_CLAIM",
    }
    # Bind every verdict to exactly the bytes parsed, before publishing success.
    # A concurrent source edit cannot make an old verdict cite a new file hash.
    for name, raw_hash in evidence_hashes.items():
        _require(hashlib.sha256((experiment_dir / name).read_bytes()).hexdigest() == raw_hash,
                 "SOURCE_EVIDENCE_CHANGED_DURING_REVIEW")
    output_dir.mkdir(parents=True)
    for row in rows:
        _write_new(output_dir / (row["scenario_id"] + ".json"), row)
    _write_new(output_dir / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = review_experiment(args.experiment, args.output)
    print(json.dumps({"model_scenarios_reviewed": 5, "model_calls_added": 0,
                      "frozen_denominator": 13, "frozen_acceptance_counts": result["frozen_acceptance_counts"]}), flush=True)


if __name__ == "__main__":
    main()
