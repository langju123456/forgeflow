"""A running service can say which code it started with, so a stale process is detectable (drift guard)."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_fabric_store import db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def module():
    from forgeflow import code_fingerprint

    return code_fingerprint


def tree(base, **extra):
    for name, text in {"src/forgeflow/a.py": "x = 1\n", "src/forgeflow/pkg/b.py": "y = 2\n",
                       "fixtures/f.json": "{}\n", "skills/s/SKILL.md": "skill\n", "pyproject.toml": "[x]\n",
                       "requirements.lock": "lock\n", **extra}.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return base


def test_the_fingerprint_is_a_sha256_over_the_files_that_define_behaviour(tmp_path):
    result = module().code_fingerprint(tree(tmp_path / "one"))
    assert len(result["fingerprint"]) == 64 and result["files"] == 6


def test_the_fingerprint_is_deterministic_and_does_not_depend_on_where_the_tree_lives(tmp_path):
    first = tree(tmp_path / "one")
    second = tmp_path / "two"
    shutil.copytree(first, second)
    assert module().code_fingerprint(first) == module().code_fingerprint(second) == module().code_fingerprint(first)


@pytest.mark.parametrize("name", ["src/forgeflow/a.py", "src/forgeflow/pkg/b.py", "fixtures/f.json",
                                  "skills/s/SKILL.md", "pyproject.toml", "requirements.lock"])
def test_one_changed_byte_in_any_covered_file_changes_the_fingerprint(tmp_path, name):
    base = tree(tmp_path / "one")
    before = module().code_fingerprint(base)
    (base / name).write_text((base / name).read_text() + " ")
    assert module().code_fingerprint(base)["fingerprint"] != before["fingerprint"]


def test_a_renamed_or_added_covered_file_changes_the_fingerprint(tmp_path):
    base = tree(tmp_path / "one")
    before = module().code_fingerprint(base)["fingerprint"]
    (base / "src/forgeflow/a.py").rename(base / "src/forgeflow/c.py")
    assert module().code_fingerprint(base)["fingerprint"] != before


@pytest.mark.parametrize("name", ["tests/test_x.py", "deliveries/day/DELIVERY.md", ".runtime/dev.env", "README.md",
                                  "src/forgeflow/__pycache__/a.cpython-312.pyc", "docs/17.md"])
def test_files_that_do_not_define_behaviour_never_change_it(tmp_path, name):
    base = tree(tmp_path / "one")
    before = module().code_fingerprint(base)
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("anything")
    assert module().code_fingerprint(base) == before


def test_the_real_tree_has_a_stable_fingerprint_that_never_reads_the_runtime_directory():
    first, second = module().code_fingerprint(ROOT), module().code_fingerprint(ROOT)
    assert first == second and first["files"] > 60
    assert not any(part.startswith(".runtime") for part in module().covered_files(ROOT))


def test_health_reports_the_fingerprint_taken_when_the_service_started(db, monkeypatch):  # noqa: F811
    from forgeflow import fabric_api

    started = {"fingerprint": "a" * 64, "files": 7}
    monkeypatch.setattr(module(), "code_fingerprint", lambda *args, **kwargs: started)
    client = TestClient(fabric_api.create_fabric_app(db().source, {}, None, test_harness=True))
    assert client.get("/health").json()["code_fingerprint"] == started
    monkeypatch.setattr(module(), "code_fingerprint", lambda *args, **kwargs: {"fingerprint": "b" * 64, "files": 9})
    assert client.get("/health").json()["code_fingerprint"] == started  # fixed at start, not recomputed
