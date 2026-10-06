"""TEST_DRIVER canonical alias probes; no runtime, model or container access."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from forgeflow.contracts import digest

ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


typed = _load("s2c_typed_review_test", ROOT / "scripts/s2c_typed_validate.py")
fixtures = _load("s2c_typed_review_fixtures", ROOT / "tests/test_s2c_evaluate.py")


def _evidence(sid="M01"):
    sample = fixtures.evidence(sid)
    sample["analysis"]["summary_json"]["selection"] = copy.deepcopy(sample["analysis"]["calls"][-1]["output_json"])
    return sample


@pytest.mark.parametrize("actual,expected", [(True, 1), (True, 1.0), (False, 0), (1, 1.0),
                                           ({"value": True}, {"value": 1}), ([1], [1.0])])
def test_canonical_equality_preserves_json_scalar_types(actual, expected):
    assert actual == expected  # The precise original evaluator limitation.
    assert typed.canonical_equal(actual, expected) is False


@pytest.mark.parametrize("sid", typed.MODEL_SCENARIOS)
def test_all_five_real_behavior_shapes_get_supplemental_typed_review(sid):
    sample = _evidence(sid)
    result = typed.assess_live(fixtures.scenario(sid), sample)
    assert result["original_full_frozen_gold_status"] == result["canonical_typed_validation"] == result["full_frozen_gold_status"] == "PASS"
    assert result["model_calls_added"] == 0 and result["model_predicts_completion_status"] is False


@pytest.mark.parametrize("wrong_value", [1, 1.0])
def test_original_alias_is_preserved_and_new_typed_guard_rejects(wrong_value):
    sample = _evidence("M01")
    call = sample["analysis"]["calls"][2]
    record = sample["grounding_records"][2]
    record["claim_catalog"] = copy.deepcopy(record["claim_catalog"])
    next(row for row in record["claim_catalog"]["observations"] if row["metric"] == "ci_smoke")["value"] = wrong_value
    call["material_json"] = copy.deepcopy(call["material_json"])
    call["material_json"]["claim_catalog_hash"] = call["material_json"]["source_snapshot"]["catalog_hash"] = digest(record["claim_catalog"])
    record["rendered_report"] = copy.deepcopy(record["rendered_report"])
    record["rendered_report"]["catalog_hash"] = digest(record["claim_catalog"])
    call.update(material_hash=digest(call["material_json"]), context_hash=digest(call["material_json"]["source_snapshot"]))
    fixtures._bind_record(record, sample["raw_provider_journal"][2], sample["raw_provider_replies"][call["id"]], call, sample["analysis"]["id"])
    # The frozen evaluator still has its documented P2. This supplemental file
    # cannot edit it or hide the original verdict by replacing its results.
    original = fixtures.runner.assess_live(fixtures.scenario("M01"), sample)
    assert original["full_frozen_gold_status"] == "PASS"
    result = typed.assess_live(fixtures.scenario("M01"), sample)
    assert result["original_full_frozen_gold_status"] == "PASS"
    assert result["canonical_typed_validation"] == result["full_frozen_gold_status"] == "FAIL"
    assert result["strict_guards"]["per_call"][2]["checks"]["catalog_canonical_gold"] is False


def test_integer_float_manifest_measurement_alias_rejected():
    sample = _evidence()
    actual_input = sample["actual_check_witness"]["input_manifest"]
    actual_input["metrics"]["boot_cycles"]["value"] = float(actual_input["metrics"]["boot_cycles"]["value"])
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["checks"]["frozen_input_canonical"] is False


@pytest.mark.parametrize("field,value", [("classification", False), ("synthetic", 1), ("classification", 0.0)])
def test_three_equal_aliased_import_witnesses_are_rejected(field, value):
    sample = _evidence("M03")
    sample["actual_check_witness"]["trusted_input"][field] = value
    assert typed.strict_guards(fixtures.scenario("M03"), sample)["checks"]["derived_witness_canonical"] is False


def test_assessment_bool_int_alias_rejected():
    sample = _evidence()
    next(row for row in sample["parent_before"]["assessment_json"]["evidence"] if row["id"] == "ci_smoke")["value"] = 1
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["checks"]["actual_assessment_canonical"] is False


def test_human_approval_output_alias_does_not_match_actual_raw_selection():
    sample = _evidence()
    sample["analysis"]["calls"][3]["output_json"] = copy.deepcopy(sample["analysis"]["calls"][3]["output_json"])
    sample["analysis"]["calls"][3]["output_json"]["human_approval_required"] = 1
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["per_call"][3]["checks"]["raw_call_output_canonical"] is False


def test_source_record_classification_alias_cannot_launder_trust_metadata():
    sample = _evidence()
    snapshot = sample["analysis"]["calls"][2]["material_json"]["source_snapshot"]
    snapshot["grounding"]["source_records"][0]["classification"] = False
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["per_call"][2]["checks"]["frozen_source_records_canonical"] is False


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))


def _experiment(path):
    _write(path / "experiment-freeze.json", {"source_files_sha256": {
        "scripts/s2c_evaluate.py": hashlib.sha256((ROOT / "scripts/s2c_evaluate.py").read_bytes()).hexdigest()}})
    for sid in typed.MODEL_SCENARIOS:
        _write(path / "raw-chains" / (sid + ".json"), _evidence(sid))
    _write(path / "faults/summary.json", {"faults": [
        {"scenario_id": sid, "provenance": "TEST_DRIVER", "real_model_calls": 0, "full_frozen_gold_status": "PASS"}
        for sid in typed.FAULT_SCENARIOS]})


def test_complete_posthoc_review_preserves_original_files_and_denominator(tmp_path):
    experiment, output = tmp_path / "original-experiment", tmp_path / "new-typed-review"
    _experiment(experiment)
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in experiment.rglob("*.json")}
    result = typed.review_experiment(experiment, output)
    assert result["real_model_scenarios_reviewed"] == 5 and result["original_test_driver_scenarios"] == 8
    assert result["case_count"] == 12 and result["scenario_count"] == 13
    assert result["frozen_acceptance_counts"] == {"PASS": 13, "FAIL": 0}
    assert result["model_calls_added"] == 0 and result["original_results_preserved"] is True
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in experiment.rglob("*.json")}
    assert len(list(output.glob("*.json"))) == 6


@pytest.mark.parametrize("mutation", ["MISSING", "EXTRA", "BAD_OLD_RUNNER_HASH", "MISSING_FAULT", "FAULT_REAL_LABEL"])
def test_review_requires_entire_frozen_denominator_and_exact_original_runner(tmp_path, mutation):
    experiment, output = tmp_path / "original", tmp_path / "supplement"
    _experiment(experiment)
    if mutation == "MISSING":
        (experiment / "raw-chains/M03.json").unlink()
    elif mutation == "EXTRA":
        _write(experiment / "raw-chains/UNUSED.json", {})
    elif mutation == "BAD_OLD_RUNNER_HASH":
        _write(experiment / "experiment-freeze.json", {"source_files_sha256": {"scripts/s2c_evaluate.py": "0" * 64}})
    else:
        faults = json.loads((experiment / "faults/summary.json").read_text())
        if mutation == "MISSING_FAULT":
            faults["faults"].pop()
        else:
            faults["faults"][0]["provenance"] = "REAL_LOCAL_MODEL"
        _write(experiment / "faults/summary.json", faults)
    with pytest.raises(RuntimeError):
        typed.review_experiment(experiment, output)
    assert not output.exists()


def test_existing_review_directory_is_never_mutated(tmp_path):
    marker = tmp_path / "sealed.json"
    marker.write_text("original")
    with pytest.raises(RuntimeError, match="USE_NEW_TYPED_REVIEW_DIRECTORY"):
        typed.review_experiment(tmp_path / "nonexistent", tmp_path)
    assert list(tmp_path.iterdir()) == [marker]


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_evidence_json_duplicate_and_nonfinite_values_are_rejected(tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    with pytest.raises((RuntimeError, ValueError)):
        typed._read(path)


def test_parent_change_cannot_hide_under_python_scalar_alias():
    sample = _evidence()
    sample["parent_after"]["revision"] = float(sample["parent_after"]["revision"])
    assert sample["parent_before"] == sample["parent_after"]
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["checks"]["parent_unchanged_canonical"] is False


def test_provider_provenance_requires_exact_bool_type():
    sample = _evidence()
    sample["grounding_records"][2]["provider_provenance"]["dispatched"] = 1
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["per_call"][2]["checks"]["provider_provenance_canonical"] is False


def test_server_summary_selection_is_not_replaced_by_an_aliased_projection():
    sample = _evidence()
    sample["analysis"]["summary_json"]["selection"]["human_approval_required"] = 1
    assert typed.strict_guards(fixtures.scenario("M01"), sample)["per_call"][3]["checks"]["persisted_summary_selection_canonical"] is False


def test_changed_evidence_after_parse_cannot_publish_old_verdict_with_new_hash(tmp_path, monkeypatch):
    experiment, output = tmp_path / "original", tmp_path / "supplement"
    _experiment(experiment)
    original_assess = typed.assess_live

    def change_after_parse(case, sample):
        result = original_assess(case, sample)
        if case["scenario_id"] == "M01":
            path = experiment / "raw-chains/M01.json"
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(typed, "assess_live", change_after_parse)
    with pytest.raises(RuntimeError, match="SOURCE_EVIDENCE_CHANGED_DURING_REVIEW"):
        typed.review_experiment(experiment, output)
    assert not output.exists()


def test_symlink_evidence_cannot_read_an_unrelated_path(tmp_path):
    experiment, output = tmp_path / "original", tmp_path / "supplement"
    _experiment(experiment)
    external = tmp_path / "unrelated.json"
    external.write_text("{}")
    victim = experiment / "raw-chains/M03.json"
    victim.unlink()
    victim.symlink_to(external)
    with pytest.raises(RuntimeError, match="EVIDENCE_PATH_ESCAPES_EXPERIMENT"):
        typed.review_experiment(experiment, output)
    assert not output.exists()
