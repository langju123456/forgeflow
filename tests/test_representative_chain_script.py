"""scripts/representative_chain.py is a thin shell: it must refuse the live database before doing anything."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("representative_chain_under_test", ROOT / "scripts/representative_chain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("command", [["start", "--key", "k", "--out", "x"], ["verify", "--state", "x.json"],
                                     ["crash-execute", "approval-id", "--out", "x"]])
def test_every_command_refuses_a_database_that_is_not_a_copy(script, monkeypatch, capsys, command):
    monkeypatch.setenv("FORGEFLOW_DATABASE_URL", "postgresql+psycopg://user:secret@127.0.0.1:55436/forgeflow")
    assert script.main(command) == 1
    printed = capsys.readouterr().out
    assert json.loads(printed) == {"status": "REFUSED", "code": "CHAIN_NEEDS_A_DATABASE_COPY"}
    assert "secret" not in printed


def test_only_the_crash_recovery_demo_may_execute_and_nothing_may_decide():
    import ast

    tree = ast.parse((ROOT / "scripts/representative_chain.py").read_text())
    offenders = []
    for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
        for call in [node for node in ast.walk(function) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr in {"decide", "execute"}]:
            if call.func.attr == "decide" or not function.name.startswith("crash"):
                offenders.append(f"{function.name}:{call.lineno} {call.func.attr}")
    assert offenders == []
