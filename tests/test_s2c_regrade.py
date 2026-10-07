"""Offline tests against actual archived synthetic evidence, never inference.

The archive contains real runtime ledger shapes. Every mutation is detached in
memory or a temporary copy; original experiments and evaluator remain intact.

These run on the working tree: they are regression tests that the current code can still reproduce
the archived results, not a frozen replay. The frozen replay, which executes the anchor commit's own
code, is tests/test_frozen_replay_at_anchor.py.
"""

import copy
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from forgeflow.contracts import Scope, digest
from forgeflow.grounded_store import FIELDS, GroundedStore

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01"
SPEC = importlib.util.spec_from_file_location("s2c_regrade_tests", ROOT / "scripts/s2c_regrade.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def _case(sid):
    return next(case for case in review.original.load_scenarios()[1] if case["scenario_id"] == sid)


def _sample(sid="M01"):
    return json.loads((ARCHIVE / "raw-chains" / (sid + ".json")).read_bytes())


def _rehash(sample, index):
    """Use the actual persistence binding, never the frozen fake fixture."""
    call, record, journal = (sample["analysis"]["calls"][index], sample["grounding_records"][index],
                             sample["raw_provider_journal"][index])
    reply = sample["raw_provider_replies"][call["id"]]
    scope = Scope(**review._scope(call))
    journal.update(provider_reply=copy.deepcopy(reply), provider_reply_hash=digest(reply),
                   claim_catalog=copy.deepcopy(record["claim_catalog"]), claim_catalog_hash=digest(record["claim_catalog"]))
    journal["payload_hash"] = digest(GroundedStore._journal_binding(
        scope, sample["analysis"]["id"], call["id"], reply, call["input_hash"], journal["claim_catalog"]))
    record.update(raw_reply_hash=digest(record["raw_reply"]), raw_journal_hash=journal["payload_hash"],
                  claim_catalog_hash=digest(record["claim_catalog"]))
    record["payload_hash"] = digest(GroundedStore._binding(
        scope, sample["analysis"]["id"], call["id"], {name: record[name] for name in FIELDS}))


@pytest.mark.parametrize("sid", review.MODEL_SCENARIOS)
def test_actual_archived_production_binding_and_frozen_bug(sid):
    sample = _sample(sid)
    scope = review._scope(sample["analysis"])
    for record in sample["grounding_records"]:
        expected = GroundedStore._binding(Scope(**scope), sample["analysis"]["id"], record["call_id"],
                                           {field: record[field] for field in FIELDS})
        assert review.grade_binding(scope, sample["analysis"]["id"], record["call_id"], record) == expected
        assert record["payload_hash"] == digest(expected)
        assert record["payload_hash"] != digest({"scope": scope, "analysis_id": sample["analysis"]["id"],
                                                 "call_id": record["call_id"], **expected["record"]})
    result = review.assess_live(_case(sid), sample)
    assert result["original_full_frozen_gold_status"] == result["original_structural_validation"] == "FAIL"
    assert result["structural_validation"] == result["semantic_validation"] == result["full_frozen_gold_status"] == "PASS"
    assert result["actual_check_gold"]["status"] == result["canonical_typed_validation"] == "PASS"
    assert result["confirmed_model_calls"] == 4 and result["model_calls_added"] == 0
    assert result["model_predicts_completion_status"] is False


@pytest.mark.parametrize("content_mode", ["RAW_UTF8", "PARSED_MAP"])
def test_raw_utf8_and_parsed_content_are_independently_graded(content_mode):
    sample = _sample()
    for index, call in enumerate(sample["analysis"]["calls"]):
        reply = sample["raw_provider_replies"][call["id"]]
        if content_mode == "PARSED_MAP":
            reply.pop("content_utf8")
            reply["content"] = copy.deepcopy(call["output_json"])
            sample["grounding_records"][index]["raw_reply"] = copy.deepcopy(call["output_json"])
        else:
            assert type(reply["content_utf8"]) is str
            # Parser preference must preserve original bytes, even when the
            # optional parsed-content convenience field disagrees.
            reply["content"] = {"tampered_convenience_projection": True}
        _rehash(sample, index)
    assert review.assess_live(_case("M01"), sample)["full_frozen_gold_status"] == "PASS"


@pytest.mark.parametrize("binding_tamper", ["BAD_NESTED_HASH", "LEGACY_FLATTENED", "WRONG_CALL_ID", "RAW_FIELD"])
def test_grade_envelope_tampering_cannot_pass(binding_tamper):
    sample = _sample()
    record = sample["grounding_records"][0]
    if binding_tamper == "BAD_NESTED_HASH":
        record["payload_hash"] = "0" * 64
    elif binding_tamper == "LEGACY_FLATTENED":
        record["payload_hash"] = digest({"scope": review._scope(record), "analysis_id": record["analysis_id"],
                                         "call_id": record["call_id"], **{field: record[field] for field in FIELDS}})
    elif binding_tamper == "WRONG_CALL_ID":
        record["payload_hash"] = digest(review.grade_binding(review._scope(record), record["analysis_id"], "OTHER", record))
    else:
        record["raw_reply"] += " "  # Valid JSON still has distinct raw evidence bytes.
    result = review.assess_live(_case("M01"), sample)
    assert result["structural_validation"] == result["full_frozen_gold_status"] == "FAIL"


@pytest.mark.parametrize("value", [1, 1.0])
def test_rehashed_bool_scalar_alias_rejected_against_frozen_catalog_gold(value):
    sample = _sample()
    record = sample["grounding_records"][2]
    call = sample["analysis"]["calls"][2]
    next(row for row in record["claim_catalog"]["observations"] if row["metric"] == "ci_smoke")["value"] = value
    call["material_json"]["claim_catalog_hash"] = digest(record["claim_catalog"])
    # The source snapshot can appear several times in expanded material.
    for snapshot in [call["material_json"]["source_snapshot"], *(step.get("material", {}).get("source_snapshot")
                     for step in call["material_json"].get("context_steps", []))]:
        if snapshot:
            snapshot["catalog_hash"] = digest(record["claim_catalog"])
    call.update(material_hash=digest(call["material_json"]), context_hash=digest(call["material_json"]["source_snapshot"]))
    record["rendered_report"]["catalog_hash"] = digest(record["claim_catalog"])
    _rehash(sample, 2)
    result = review.assess_live(_case("M01"), sample)
    assert result["canonical_typed_validation"] == result["full_frozen_gold_status"] == "FAIL"
    assert result["strict_guards"]["per_call"][2]["checks"]["catalog_canonical_gold"] is False


@pytest.mark.parametrize("kind", ["MANIFEST_INT_FLOAT", "WITNESS_BOOL_INT", "SUMMARY_BOOL_INT", "RAW_REPORT_IDS"])
def test_check_input_witness_projection_and_raw_selection_fail_closed(kind):
    sid = "M03" if kind == "WITNESS_BOOL_INT" else "M01"
    sample = _sample(sid)
    if kind == "MANIFEST_INT_FLOAT":
        manifest = sample["actual_check_witness"]["input_manifest"]
        manifest["metrics"]["boot_cycles"]["value"] = float(manifest["metrics"]["boot_cycles"]["value"])
    elif kind == "WITNESS_BOOL_INT":
        for witness in (sample["actual_check_witness"]["trusted_input"], sample["parent_before"]["request_json"]["trusted_input"],
                        sample["parent_before"]["snapshot_json"]["trusted_input"], sample["parent_before"]["result_json"]["trusted_input"]):
            witness["synthetic"] = 1
    elif kind == "SUMMARY_BOOL_INT":
        sample["analysis"]["summary_json"]["selection"]["human_approval_required"] = 1
    else:
        record = sample["grounding_records"][2]
        call = sample["analysis"]["calls"][2]
        reply = sample["raw_provider_replies"][call["id"]]
        output = copy.deepcopy(call["output_json"])
        output["observations"][0] = "c_" + "0" * 16
        reply["content_utf8"] = json.dumps(output)
        record["raw_reply"] = reply["content_utf8"]
        call["output_json"] = output
        _rehash(sample, 2)
    result = review.assess_live(_case(sid), sample)
    assert result["full_frozen_gold_status"] == "FAIL"


def _copy_archive(target):
    shutil.copytree(ARCHIVE, target)


def _hashes(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


def test_entire_real_archive_replay_keeps_original_eight_faults_and_thirteen_denominator(tmp_path):
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    before = _hashes(original)
    result = review.review_experiment(original, output)
    assert result["original_frozen_acceptance_counts"] == {"PASS": 8, "FAIL": 5, "NOT_RUN": 0}
    assert result["regraded_frozen_acceptance_counts"] == {"PASS": 13, "FAIL": 0, "NOT_RUN": 0}
    assert result["case_count"] == 12 and result["scenario_count"] == 13
    assert result["original_confirmed_model_calls"] == 20 and result["model_calls_added"] == 0
    assert result["original_fault_passes"] == 8 and result["original_results_preserved"] is True
    assert before == _hashes(original)
    assert len(list(output.glob("*.json"))) == 8
    frozen = json.loads((output / "review-freeze.json").read_bytes())
    assert frozen["review_dependencies"]["scripts/s2c_regrade.py"] == hashlib.sha256((ROOT / "scripts/s2c_regrade.py").read_bytes()).hexdigest()
    assert len([path for path in frozen["source_evidence_files_sha256"] if path.startswith("call-freezes/")]) == 20


@pytest.mark.parametrize("change", ["MISSING_MODEL", "EXTRA_MODEL", "MISSING_FAULT", "FAULT_REAL_ALIAS", "FLATTENED_HASH", "EXTERNAL_FREEZE"])
def test_no_partial_or_mutated_archive_can_publish_success(tmp_path, change):
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    if change == "MISSING_MODEL":
        (original / "raw-chains/M03.json").unlink()
    elif change == "EXTRA_MODEL":
        (original / "raw-chains/M13.json").write_text("{}")
    elif change == "MISSING_FAULT":
        (original / "faults/M05.json").unlink()
    else:
        path = original / ("faults/M05.json" if change == "FAULT_REAL_ALIAS" else "raw-chains/M01.json")
        data = json.loads(path.read_bytes())
        if change == "FAULT_REAL_ALIAS":
            data["real_model_calls"] = False
        elif change == "EXTERNAL_FREEZE":
            path = original / "call-freezes" / (data["analysis"]["calls"][0]["id"] + ".json")
            data = json.loads(path.read_bytes())
            data["frozen_before_provider_dispatch"] = 1
        else:
            row = data["grounding_records"][0]
            row["payload_hash"] = digest({"scope": review._scope(row), "analysis_id": row["analysis_id"],
                                          "call_id": row["call_id"], **{field: row[field] for field in FIELDS}})
        path.write_text(json.dumps(data))
    with pytest.raises((RuntimeError, FileNotFoundError)):
        review.review_experiment(original, output)
    assert not output.exists()


def test_source_edit_during_review_cannot_publish_a_verdict_bound_to_different_bytes(tmp_path, monkeypatch):
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    assess = review.assess_live

    def mutate_after_parse(case, sample):
        result = assess(case, sample)
        if case["scenario_id"] == "M01":
            path = original / "raw-chains/M01.json"
            path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(review, "assess_live", mutate_after_parse)
    with pytest.raises(RuntimeError, match="SOURCE_EVIDENCE_CHANGED_DURING_REGRADE"):
        review.review_experiment(original, output)
    assert not output.exists()


def test_existing_output_never_changes_original_review(tmp_path):
    marker = tmp_path / "KEEP.txt"
    marker.write_text("immutable")
    with pytest.raises(RuntimeError, match="USE_NEW_REGRADE_DIRECTORY"):
        review.review_experiment(ARCHIVE, tmp_path)
    assert _hashes(tmp_path) == {"KEEP.txt": hashlib.sha256(b"immutable").hexdigest()}


def test_working_tree_edits_to_frozen_files_do_not_break_the_replay(tmp_path, monkeypatch):
    """Frozen files are read from the anchor commit; only the review code is checked in the working tree."""
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    real = review._source_hashes
    monkeypatch.setattr(review, "_source_hashes",
                        lambda freeze, sha: {**real(freeze, sha), "src/forgeflow/local.py": "0" * 64})
    assert review.review_experiment(original, output)["regraded_frozen_acceptance_counts"]["PASS"] == 13
