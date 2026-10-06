"""The frozen-source check reads the anchor commit with git and never the working tree.

Negative cases use a throwaway git repository, so no archived evidence is touched.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01/experiment-freeze.json"
SPEC = importlib.util.spec_from_file_location("s2c_regrade_anchor_tests", ROOT / "scripts/s2c_regrade.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
CONTENT = b"committed bytes\n"
CONTENT_SHA = hashlib.sha256(CONTENT).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    done = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, check=True)
    return done.stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "a.txt").write_bytes(CONTENT)
    git(folder, "add", "a.txt")
    git(folder, "commit", "-q", "-m", "frozen state")
    return folder, git(folder, "rev-parse", "HEAD")


def anchor(tmp_path, commit, freeze_sha, schema="forgeflow-freeze-git-anchor-v1"):
    path = tmp_path / "anchor.json"
    path.write_text(json.dumps({"schema": schema, "git_commit": commit, "freeze_sha256": freeze_sha}))
    return path


def freeze_of(expected):
    freeze = {"source_files_sha256": {"a.txt": expected}}
    return freeze, sha(json.dumps(freeze).encode())


def check(tmp_path, repo_info, *, expected=CONTENT_SHA, commit=None, freeze_sha=None, repo=None, schema=None):
    folder, head = repo_info
    freeze, real_sha = freeze_of(expected)
    path = anchor(tmp_path, commit or head, freeze_sha or real_sha, *([schema] if schema else []))
    return review._source_hashes(freeze, real_sha, anchor_path=path, repo=repo or folder)


def rejected(code, call):
    with pytest.raises(RuntimeError) as raised:
        call()
    assert str(raised.value) == code


def test_matching_commit_passes_even_after_the_working_tree_changes(tmp_path, repo):
    (repo[0] / "a.txt").write_bytes(b"edited after the freeze\n")
    result = check(tmp_path, repo)
    assert result["a.txt"] == sha(CONTENT)


def test_frozen_hash_that_differs_from_the_commit_content_fails(tmp_path, repo):
    rejected("FROZEN_EXPERIMENT_SOURCE_CHANGED", lambda: check(tmp_path, repo, expected=sha(b"something else")))


def test_anchor_to_a_commit_that_does_not_exist_fails_with_a_reason(tmp_path, repo):
    rejected("FROZEN_GIT_HISTORY_UNAVAILABLE", lambda: check(tmp_path, repo, commit="0" * 40))


def test_missing_git_history_fails_instead_of_silently_skipping(tmp_path, repo, monkeypatch):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    rejected("FROZEN_GIT_HISTORY_UNAVAILABLE", lambda: check(tmp_path, repo, repo=bare))


def test_anchor_for_a_different_freeze_file_fails(tmp_path, repo):
    rejected("FROZEN_ANCHOR_FREEZE_MISMATCH", lambda: check(tmp_path, repo, freeze_sha="f" * 64))


@pytest.mark.parametrize("schema", ["something-else", None])
def test_malformed_anchor_fails(tmp_path, repo, schema):
    folder, head = repo
    freeze, real_sha = freeze_of(sha(CONTENT))
    path = anchor(tmp_path, head, real_sha, schema or "forgeflow-freeze-git-anchor-v1")
    if schema is None:
        path.write_text("{not json")
    rejected("FROZEN_GIT_ANCHOR_INVALID", lambda: review._source_hashes(freeze, real_sha, anchor_path=path, repo=folder))


def test_missing_anchor_file_fails(tmp_path, repo):
    freeze, real_sha = freeze_of(sha(CONTENT))
    rejected("FROZEN_GIT_ANCHOR_MISSING", lambda: review._source_hashes(
        freeze, real_sha, anchor_path=tmp_path / "absent.json", repo=repo[0]))


def test_commit_reference_must_be_a_full_hash(tmp_path, repo):
    rejected("FROZEN_GIT_ANCHOR_INVALID", lambda: check(tmp_path, repo, commit="HEAD"))


def test_file_absent_from_the_anchor_commit_fails(tmp_path, repo):
    folder, head = repo
    freeze = {"source_files_sha256": {"missing.txt": sha(CONTENT)}}
    freeze_sha = sha(json.dumps(freeze).encode())
    path = anchor(tmp_path, head, freeze_sha)
    rejected("FROZEN_SOURCE_NOT_IN_ANCHOR",
             lambda: review._source_hashes(freeze, freeze_sha, anchor_path=path, repo=folder))


def test_symlink_in_the_anchor_commit_is_not_a_regular_file(tmp_path, repo):
    folder, _ = repo
    os.symlink("a.txt", folder / "link.txt")
    git(folder, "add", "link.txt")
    git(folder, "commit", "-q", "-m", "add symlink")
    head = git(folder, "rev-parse", "HEAD")
    freeze = {"source_files_sha256": {"link.txt": sha(b"a.txt")}}
    freeze_sha = sha(json.dumps(freeze).encode())
    path = anchor(tmp_path, head, freeze_sha)
    rejected("FROZEN_SOURCE_REGULAR_FILE_REQUIRED",
             lambda: review._source_hashes(freeze, freeze_sha, anchor_path=path, repo=folder))


def test_shipped_anchor_matches_the_untouched_freeze_and_is_reachable_from_head():
    shipped = json.loads(review.ANCHOR_PATH.read_bytes())
    raw = FREEZE.read_bytes()
    assert shipped["freeze_sha256"] == sha(raw)
    assert shipped["frozen_source_files_checked"] == len(json.loads(raw)["source_files_sha256"])
    result = review._source_hashes(json.loads(raw), sha(raw))
    assert result == {**json.loads(raw)["source_files_sha256"], **{
        name: sha((ROOT / name).read_bytes()) for name in ("scripts/s2c_regrade.py", "scripts/s2c_typed_validate.py")}}
    ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", shipped["git_commit"], "HEAD"],
                              capture_output=True, check=False)
    assert ancestor.returncode == 0, "anchor commit is not in this repository's history"
