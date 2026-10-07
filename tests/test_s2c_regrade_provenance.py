"""A regrade records which bytes it really executed, apart from the historical experiment's sources (F2).

The experiment's 50 frozen source hashes are verified at the anchor commit and say what produced the archived
evidence. The regrade itself runs the current working tree, so the files it actually loaded are recorded in
their own field and re-checked before anything is published. The archived experiment and the old regrade
output are never rewritten.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest
from test_s2c_regrade import ARCHIVE, _copy_archive, review

ROOT = Path(__file__).resolve().parents[1]
EXECUTED_FILES = {
    "scripts/s2c_regrade.py", "scripts/s2c_typed_validate.py", "scripts/s2c_evaluate.py",
    "scripts/s2b_evaluate.py", "preparation/s2/model-evaluation/checkfixtures.py",
    "src/forgeflow/contracts.py", "src/forgeflow/grounded_store.py",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run_regrade(tmp_path):
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    summary = review.review_experiment(original, output)
    return json.loads((output / "review-freeze.json").read_bytes()), summary, output


def test_anchored_experiment_sources_and_review_dependencies_are_separate_fields(tmp_path):
    freeze, summary, _ = run_regrade(tmp_path)
    anchor = json.loads(review.ANCHOR_PATH.read_bytes())
    experiment = json.loads((ARCHIVE / "experiment-freeze.json").read_bytes())["source_files_sha256"]
    expected = {"git_commit": anchor["git_commit"], "freeze_sha256": anchor["freeze_sha256"], "files_sha256": experiment}
    for record in (freeze, summary):
        assert "review_source_files_sha256" not in record
        assert record["anchored_experiment_sources"] == expected
        assert record["execution_mode"] == "CURRENT_WORKTREE_DEPENDENCIES"
    assert set(freeze["review_dependencies"]).isdisjoint({"git_commit", "freeze_sha256", "files_sha256"})


def test_recorded_dependencies_are_the_bytes_the_regrade_executed(tmp_path):
    freeze, summary, _ = run_regrade(tmp_path)
    dependencies = freeze["review_dependencies"]
    assert summary["review_dependencies"] == dependencies
    assert {name: sha((ROOT / name).read_bytes()) for name in dependencies} == dependencies
    assert EXECUTED_FILES <= set(dependencies)
    loaded = {str(Path(module.__file__).resolve().relative_to(ROOT)) for name, module in sys.modules.items()
              if name.startswith("forgeflow.") and getattr(module, "__file__", None)
              and Path(module.__file__).resolve().is_relative_to(ROOT / "src")}
    assert loaded <= set(dependencies)


def test_a_change_to_an_executed_dependency_during_the_regrade_prevents_publication(tmp_path, monkeypatch):
    original, output = tmp_path / "original", tmp_path / "new-review"
    _copy_archive(original)
    real, calls = review._review_dependencies, []

    def drifting():
        result = real()
        if calls:
            result["src/forgeflow/contracts.py"] = "0" * 64
        calls.append(1)
        return result

    monkeypatch.setattr(review, "_review_dependencies", drifting)
    with pytest.raises(RuntimeError, match="REVIEW_SOURCE_CHANGED_DURING_REGRADE"):
        review.review_experiment(original, output)
    assert not output.exists() and len(calls) >= 2


def test_the_archived_regrade_output_is_not_rewritten(tmp_path):
    before = {path.name: sha(path.read_bytes()) for path in sorted((ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/regrade-01").glob("*.json"))}
    run_regrade(tmp_path)
    after = {path.name: sha(path.read_bytes()) for path in sorted((ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/regrade-01").glob("*.json"))}
    assert before == after and "review_source_files_sha256" in json.loads(
        (ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/regrade-01/review-freeze.json").read_bytes())
