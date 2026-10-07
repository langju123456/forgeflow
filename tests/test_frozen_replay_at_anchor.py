"""Frozen replay: run the anchor commit's own code against the archived evidence in the repository.

The tests in test_s2c_regrade.py run on the working tree, so they only show that the current code can
still reproduce the archived results. This module exports the anchor commit with ``git archive`` and
runs that commit's own regrade tests there, so editing any working-tree file (contracts.py, the
evaluator, ...) cannot change what the replay executes. It fails, never skips, without git history.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = "deliveries/2026-10-06/s2c-fixes/evidence"
ANCHOR = ROOT / EVIDENCE / "freeze-git-anchor.json"
ARCHIVED_DIRECTORIES = ("experiment-01", "regrade-01")
REVIEW_SCRIPTS = ("scripts/s2c_regrade.py", "scripts/s2c_typed_validate.py", "scripts/s2c_evaluate.py")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def export_anchor(repo, commit, target):
    done = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit],
                          capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"FROZEN_GIT_HISTORY_UNAVAILABLE: cannot export {commit} from {repo}: "
                           + done.stderr.decode(errors="replace").strip()[:200])
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as archive:
        archive.extractall(target, filter="data")


def differences(expected, actual):
    """Relative paths whose presence or bytes differ between two directory trees."""
    def files(folder):
        return {str(path.relative_to(folder)): sha(path.read_bytes())
                for path in sorted(folder.rglob("*")) if path.is_file()}

    left, right = files(expected), files(actual)
    return sorted(name for name in left.keys() | right.keys() if left.get(name) != right.get(name))


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    commit = json.loads(ANCHOR.read_bytes())["git_commit"]
    target = tmp_path_factory.mktemp("anchor-export")
    try:
        export_anchor(ROOT, commit, target)
    except RuntimeError as error:
        pytest.fail(str(error))
    return target


def test_regrade_tests_pass_with_the_anchor_commits_own_code(export):
    environment = {**os.environ, "PYTHONPATH": str(export / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "tests/test_s2c_regrade.py"], cwd=export, env=environment,
                          capture_output=True, text=True, check=False, timeout=600)
    tail = "\n".join(done.stdout.strip().splitlines()[-15:])
    assert done.returncode == 0 and " passed" in tail and "failed" not in tail, tail


def test_archived_evidence_in_the_repository_is_byte_identical_to_the_anchor_commit(export):
    for name in ARCHIVED_DIRECTORIES:
        assert differences(export / EVIDENCE / name, ROOT / EVIDENCE / name) == [], name


def test_review_freeze_hashes_match_the_scripts_at_the_anchor_commit(export):
    recorded = json.loads((ROOT / EVIDENCE / "regrade-01/review-freeze.json").read_bytes())["review_source_files_sha256"]
    for name in REVIEW_SCRIPTS:
        assert recorded[name] == sha((export / name).read_bytes()), name


def test_frozen_source_hashes_match_the_anchor_commit_export(export):
    frozen = json.loads((ROOT / EVIDENCE / "experiment-01/experiment-freeze.json").read_bytes())["source_files_sha256"]
    assert [name for name, expected in frozen.items() if sha((export / name).read_bytes()) != expected] == []


def test_one_changed_byte_or_missing_file_in_archived_evidence_is_detected(export, tmp_path):
    copy = tmp_path / "experiment-01"
    original = export / EVIDENCE / "experiment-01"
    copy.mkdir()
    for path in original.rglob("*"):
        target = copy / path.relative_to(original)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.write_bytes(path.read_bytes())
    assert differences(original, copy) == []
    victim = copy / "experiment-freeze.json"
    victim.write_bytes(victim.read_bytes() + b" ")
    assert differences(original, copy) == ["experiment-freeze.json"]
    (copy / "completion.json").unlink()
    assert differences(original, copy) == ["completion.json", "experiment-freeze.json"]


def test_export_without_git_history_fails_with_a_reason_instead_of_skipping(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="FROZEN_GIT_HISTORY_UNAVAILABLE"):
        export_anchor(plain, "0" * 40, tmp_path / "out")
    with pytest.raises(RuntimeError, match="FROZEN_GIT_HISTORY_UNAVAILABLE"):
        export_anchor(ROOT, "0" * 40, tmp_path / "out2")
