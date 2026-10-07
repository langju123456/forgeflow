"""One shifted clock for the whole test session, and no way for new code to bypass it."""

import ast
import time
from pathlib import Path

import pytest
from clock_contract import expected_now, real_utcnow

from forgeflow import contracts
from forgeflow.store import Store

ROOT = Path(__file__).resolve().parents[1]
CLOCK_FILES = {"clock_contract.py", "conftest.py"}


def close(first, second):
    return abs((first - second).total_seconds()) < 1


def database_now(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'clock.sqlite'}")
    store.create_schema()
    with store._tx() as session:
        return store._now(session)


def test_python_clock_is_the_session_shifted_clock():
    assert close(contracts.utcnow(), expected_now())


def test_database_clock_is_the_session_shifted_clock(tmp_path):
    assert close(database_now(tmp_path), expected_now())


def test_time_time_is_the_session_shifted_clock():
    assert abs(time.time() - expected_now().timestamp()) < 1


def test_the_three_clocks_agree_with_each_other(tmp_path):
    python, database, wall = contracts.utcnow(), database_now(tmp_path), time.time()
    assert close(python, database) and abs(wall - python.timestamp()) < 1


def test_shifted_clock_keeps_moving():
    first = contracts.utcnow()
    time.sleep(0.05)
    assert contracts.utcnow() > first


@pytest.mark.real_clock
def test_real_clock_marker_leaves_every_clock_real(tmp_path):
    assert close(contracts.utcnow(), real_utcnow())
    assert close(database_now(tmp_path), real_utcnow())
    assert abs(time.time() - real_utcnow().timestamp()) < 1


def _clock_reads(path, *, allow_definition=False):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = set()
    if allow_definition:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "utcnow":
                exempt |= {id(child) for child in ast.walk(node)}
    found = []
    for node in ast.walk(tree):
        if id(node) in exempt:
            continue
        if isinstance(node, ast.ImportFrom) and any(alias.name == "utcnow" for alias in node.names):
            found.append((node.lineno, "imports utcnow by name"))
        elif isinstance(node, ast.Name) and node.id == "utcnow":
            found.append((node.lineno, "uses utcnow by name"))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in {"now", "utcnow"}
              and getattr(node.func.value, "id", getattr(node.func.value, "attr", None)) == "datetime"):
            found.append((node.lineno, "reads the datetime clock directly"))
    return found


def test_package_code_reads_time_only_through_contracts_utcnow():
    offenders = [f"{path.relative_to(ROOT)}:{line} {what}"
                 for path in sorted((ROOT / "src/forgeflow").glob("*.py"))
                 for line, what in _clock_reads(path, allow_definition=path.name == "contracts.py")]
    assert offenders == []


def test_tests_read_time_only_through_contracts_utcnow():
    offenders = [f"{path.relative_to(ROOT)}:{line} {what}"
                 for path in sorted((ROOT / "tests").glob("*.py")) if path.name not in CLOCK_FILES
                 for line, what in _clock_reads(path)]
    assert offenders == []
