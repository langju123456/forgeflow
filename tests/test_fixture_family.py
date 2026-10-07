"""Renewed Aurora fixtures (-v2): same facts, later expiry, one place that knows the ids (problem 4).

The old fixtures are pinned by hash in the frozen evaluation data and must never change. New runs and the demo
use the -v2 copies, which differ from the originals only in valid_until.
"""

import ast
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from clock_contract import real_utcnow

from forgeflow import contracts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
OLD_EXPIRY, NEW_EXPIRY = "2026-11-05T00:00:00Z", "2027-04-05T00:00:00Z"
NAMES = ("pass", "blocked", "unknown", "timeout")


def family():
    from forgeflow import fixture_family

    return fixture_family


@pytest.mark.parametrize("name", NAMES)
def test_renewed_fixture_differs_from_the_original_only_in_valid_until(name):
    old = (FIXTURES / f"aurora-{name}.json").read_text()
    new = (FIXTURES / f"aurora-{name}-v2.json").read_text()
    assert new == old.replace(OLD_EXPIRY, NEW_EXPIRY) and OLD_EXPIRY not in new and NEW_EXPIRY in new
    old_json, new_json = json.loads(old), json.loads(new)
    for metric in old_json["metrics"]:
        assert new_json["metrics"][metric] == {**old_json["metrics"][metric], "valid_until": NEW_EXPIRY}
    assert {key: value for key, value in new_json.items() if key != "metrics"} == {
        key: value for key, value in old_json.items() if key != "metrics"}


@pytest.mark.real_clock
@pytest.mark.parametrize("name", NAMES)
def test_renewed_fixtures_do_not_expire_within_fourteen_days(name):
    manifest = json.loads((FIXTURES / f"aurora-{name}-v2.json").read_text())
    expiry = min(datetime.fromisoformat(metric["valid_until"].replace("Z", "+00:00"))
                 for metric in manifest["metrics"].values())
    left = expiry - real_utcnow()
    assert left >= timedelta(days=14), f"aurora-{name}-v2 expires {expiry:%Y-%m-%d}; renew it ({left.days} days left)"


def test_the_fixture_family_lists_every_id_the_request_contract_accepts():
    accepted = set(get_args(contracts.RunRequest.model_fields["fixture_id"].annotation))
    assert accepted == set(family().FAMILY) and len(accepted) == 8
    assert {family().family(f"aurora-{name}-v2") for name in NAMES} == {f"aurora-{name}" for name in NAMES}
    assert family().family("aurora-pass") == "aurora-pass"


def test_only_the_timeout_family_selects_the_timeout_template():
    assert [family().template_for(f"aurora-{name}{suffix}") for suffix in ("", "-v2") for name in NAMES] == [
        "evt-check", "evt-check", "evt-check", "timeout"] * 2
    with pytest.raises(Exception, match="SYNTHETIC_SOURCE_REQUIRED"):
        family().template_for("aurora-unlisted")


def test_cli_choice_maps_to_the_renewed_fixture_unless_the_legacy_set_is_asked_for():
    assert [family().cli_fixture(name) for name in NAMES] == [f"aurora-{name}-v2" for name in NAMES]
    assert [family().cli_fixture(name, legacy=True) for name in NAMES] == [f"aurora-{name}" for name in NAMES]


def test_the_snapshot_expiry_is_the_earliest_evidence_expiry_of_the_fixture():
    assert family().evidence_valid_until(json.loads((FIXTURES / "aurora-pass.json").read_text())) == OLD_EXPIRY
    assert family().evidence_valid_until(json.loads((FIXTURES / "aurora-unknown-v2.json").read_text())) == NEW_EXPIRY
    mixed = {"metrics": {"a": {"valid_until": "2027-01-01T00:00:00Z"}, "b": {"valid_until": "2026-12-01T00:00:00Z"}}}
    assert family().evidence_valid_until(mixed) == "2026-12-01T00:00:00Z"


def test_packages_no_longer_hard_code_fixture_ids_or_the_old_expiry():
    offenders = []
    for path in sorted((ROOT / "src/forgeflow").glob("*.py")):
        if path.name in {"fixture_family.py", "contracts.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                    node.value == "aurora-timeout" or node.value == OLD_EXPIRY
                    or node.value == "aurora-"):
                offenders.append(f"{path.name}:{node.lineno} {node.value}")
    # s2c_checks pins the frozen M03 evidence expiry and grounded_cli/s2c_runtime map frozen scenarios to the
    # original fixtures on purpose; they are the only allowed users of the old values.
    allowed = {"s2c_checks.py", "grounded_cli.py", "s2c_runtime.py", "grounded_runtime.py"}
    assert [item for item in offenders if item.split(":")[0] not in allowed] == []


@pytest.fixture
def cli_store(tmp_path, monkeypatch):
    from forgeflow import local
    from forgeflow.contracts import Principal

    owner = Principal(subject_id="local-owner", kind="user", scopes=(local.SCOPE,),
                      roles=frozenset({"submitter", "admin", "reviewer", "hardware", "pm"}))
    monkeypatch.setenv("FORGEFLOW_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.sqlite'}")
    monkeypatch.setattr(local, "load_identities", lambda: {"synthetic-test-token": owner})
    return local


@pytest.mark.parametrize(("argv", "expected"), [
    (["submit", "--key", "k1"], "aurora-pass-v2"),
    (["submit", "--key", "k2", "--fixture", "timeout"], "aurora-timeout-v2"),
    (["submit", "--key", "k3", "--legacy-fixtures"], "aurora-pass"),
])
def test_the_local_cli_submits_the_renewed_fixture_by_default(cli_store, monkeypatch, capsys, argv, expected):
    monkeypatch.setattr(sys, "argv", ["forgeflow.local", *argv])
    cli_store.main()
    assert json.loads(capsys.readouterr().out)["request_json"]["fixture_id"] == expected


def test_the_fabric_cli_submits_the_renewed_fixture_by_default(tmp_path, monkeypatch, capsys):
    from forgeflow import fabric_cli
    from forgeflow.contracts import Principal
    from forgeflow.fabric_contract import FABRIC_SCOPE
    from forgeflow.fabric_runtime import FabricStore

    store = FabricStore(f"sqlite:///{tmp_path / 'fabric.sqlite'}")
    store.create_schema()
    roles = {"owner": {"admin", "submitter", "supervisor", "hardware", "pm"}, "reviewer": {"reviewer"}}
    identities = {name: Principal(subject_id=name, kind="user", roles=frozenset(r), scopes=(FABRIC_SCOPE,))
                  for name, r in roles.items()}
    identities["executor"] = Principal(subject_id="executor", kind="service", roles=frozenset({"fabric_executor"}),
                                       scopes=(FABRIC_SCOPE,))
    monkeypatch.setattr(fabric_cli, "load_fabric_store", lambda: store)
    monkeypatch.setattr(fabric_cli, "load_fabric_identities", lambda: identities)
    monkeypatch.setattr(sys, "argv", ["fabric_cli", "submit", "--key", "k1"])
    fabric_cli.main()
    assert json.loads(capsys.readouterr().out)["output"]["request_json"]["fixture_id"] == "aurora-pass-v2"


def test_the_derived_fixture_id_matches_the_one_the_frozen_s2c_path_uses():
    from forgeflow import s2c_checks

    assert family().DERIVED_FIXTURES == {s2c_checks.DERIVED_FIXTURE_ID}
    assert family().template_for(s2c_checks.DERIVED_FIXTURE_ID) == "evt-check"
