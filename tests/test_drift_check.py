"""scripts/drift_check.py: a read-only, mechanical answer to "is anything out of step with the code?"."""

import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = "deliveries/anchor.json"
CURRENT = {"fingerprint": "c" * 64, "files": 70}


@pytest.fixture(scope="module")
def drift():
    spec = importlib.util.spec_from_file_location("drift_check_under_test", ROOT / "scripts/drift_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    git(folder, "init", "-q")
    (folder / "a.txt").write_text("one\n")
    git(folder, "add", "a.txt")
    git(folder, "commit", "-q", "-m", "baseline")
    base = git(folder, "rev-parse", "HEAD")
    (folder / "deliveries").mkdir()
    (folder / ANCHOR).write_text(json.dumps({"git_commit": base}))
    git(folder, "add", ANCHOR)
    git(folder, "commit", "-q", "-m", "anchor")
    git(folder, "tag", "freeze-baseline", base)
    return folder, base


def test_tracked_changes_are_drift_but_untracked_files_are_not(drift, repo):
    folder, _ = repo
    (folder / "scratch.txt").write_text("not tracked")
    assert drift.check_git_clean(folder)["status"] == "OK"
    (folder / "a.txt").write_text("edited\n")
    result = drift.check_git_clean(folder)
    assert result["status"] == "DRIFT" and result["detail"]["changed"] == ["a.txt"]


def test_the_baseline_tag_must_point_at_the_anchor_commit(drift, repo):
    folder, base = repo
    assert drift.check_baseline_tag(folder, ANCHOR)["status"] == "OK"
    git(folder, "tag", "-f", "freeze-baseline", "HEAD")
    result = drift.check_baseline_tag(folder, ANCHOR)
    assert result["status"] == "DRIFT" and result["detail"]["anchor"] == base


def test_a_service_with_the_current_fingerprint_is_in_step(drift):
    asked = []

    def fetch(url):
        asked.append(url)
        return {"revision": "r", "process_id": 41, "code_fingerprint": CURRENT}

    result = drift.check_service(fetch, CURRENT)
    assert result["status"] == "OK" and asked == ["http://127.0.0.1:8773/health"]


def test_a_service_started_from_other_code_is_drift_and_both_fingerprints_are_shown(drift):
    other = {"fingerprint": "d" * 64, "files": 60}
    result = drift.check_service(lambda url: {"process_id": 41, "code_fingerprint": other}, CURRENT)
    assert result["status"] == "DRIFT" and result["detail"] == {"running": other, "current": CURRENT, "process_id": 41}


def test_a_service_that_cannot_say_which_code_it_runs_is_drift(drift):
    result = drift.check_service(lambda url: {"revision": "r", "process_id": 41}, CURRENT)
    assert result["status"] == "DRIFT" and result["detail"]["reason"] == "HEALTH_HAS_NO_CODE_FINGERPRINT"


def test_no_service_on_the_port_is_reported_but_is_not_drift(drift):
    def fetch(url):
        raise ConnectionRefusedError("refused")

    assert drift.check_service(fetch, CURRENT)["status"] == "INFO"


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("http://127.0.0.1:8773/health", 500, "boom", {}, None),
    ValueError("not json"),
    TimeoutError("no answer"),
])
def test_something_listening_that_does_not_answer_properly_is_drift_not_absence(drift, error):
    def fetch(url):
        raise error

    result = drift.check_service(fetch, CURRENT)
    assert result["status"] == "DRIFT" and result["detail"]["reason"] == "SERVICE_DID_NOT_ANSWER_PROPERLY"
    assert result["detail"]["error"] == type(error).__name__


def test_only_the_loopback_health_url_is_ever_requested(drift):
    assert drift.HEALTH_URL == "http://127.0.0.1:8773/health"


def mirror(tmp_path, repo, *, public_behind=False, edited=False):
    folder, _ = repo
    public = tmp_path / "public"
    public.mkdir()
    git(public, "init", "-q")
    (public / "a.txt").write_text("one\n")
    git(public, "add", "a.txt")
    git(public, "commit", "-q", "-m", "published")
    old = git(folder, "rev-parse", "HEAD~1" if public_behind else "HEAD")
    new = git(public, "rev-parse", "HEAD")
    Path(str(public) + ".history-map.txt").write_text(f"{old} {new}\n")
    if edited:
        (public / "b.txt").write_text("x")
        git(public, "add", "b.txt")
        git(public, "commit", "-q", "-m", "edited in place")
    return public


def test_a_public_copy_built_from_the_current_head_is_in_step(drift, repo, tmp_path):
    assert drift.check_public_mirror(repo[0], mirror(tmp_path, repo))["status"] == "OK"


def test_a_public_copy_built_from_an_older_head_is_behind(drift, repo, tmp_path):
    result = drift.check_public_mirror(repo[0], mirror(tmp_path, repo, public_behind=True))
    assert result["status"] == "DRIFT" and result["detail"]["reason"] == "PUBLIC_COPY_BEHIND"


def test_a_public_copy_edited_after_publishing_is_not_the_published_history(drift, repo, tmp_path):
    result = drift.check_public_mirror(repo[0], mirror(tmp_path, repo, edited=True))
    assert result["status"] == "DRIFT" and result["detail"]["reason"] == "PUBLIC_COPY_CHANGED_AFTER_PUBLISHING"


def test_the_report_fails_on_any_drift_and_passes_otherwise(drift, repo, capsys):
    folder, _ = repo
    quiet = ["--repo", str(folder), "--anchor", ANCHOR, "--skip-service"]
    assert drift.main(quiet) == 0
    assert {item["check"] for item in json.loads(capsys.readouterr().out)["checks"]} >= {"git_clean", "baseline_tag"}
    (folder / "a.txt").write_text("edited\n")
    assert drift.main(quiet) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "DRIFT"
