"""Renewing the SOP document (v1 expires 2026-11-05) is rehearsed on database copies only.

A new version carries the same content and a later expiry; the old version stays. Because the new version
becomes current, runs that cite the old one stop passing the freshness check from the moment it is written, so
the plan lists them first and nothing is written without --apply and an explicit database URL.
"""

import ast
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import select

from forgeflow import local
from forgeflow.analysis_context import SOP_CONTENT, SOP_HASH, ExportGrant
from forgeflow.contracts import DomainError, Principal, RunRequest
from forgeflow.evt_store import EVTBoundStore
from forgeflow.store import Document, DocumentVersion, Run, filters

ROOT = Path(__file__).resolve().parents[1]
SCOPE = local.SCOPE
OLD, NEW = "2026-11-05T00:00:00Z", "2027-04-05T00:00:00Z"
ROLES = {"hardware", "pm", "supervisor", "admin", "reviewer", "submitter", "worker"}
ADMIN = Principal(subject_id="seed-admin", kind="user", roles=frozenset({"admin", "submitter"}), scopes=(SCOPE,),
                  clearance=3)
WORKER = Principal(subject_id="seed-worker", kind="service", roles=frozenset({"worker"}), scopes=(SCOPE,))


def renewal():
    from forgeflow import sop_renewal

    return sop_renewal


def seed(path):
    store = EVTBoundStore(f"sqlite:///{path}")
    store.create_schema()
    store.put_document(ADMIN, SCOPE, "aurora-evt-sop", "v1", SOP_CONTENT, ROLES, 0, valid_until=OLD)
    run = store.submit(ADMIN, RunRequest(scope=SCOPE, fixture_id="aurora-pass"), "pending-run")
    claim = store.claim(WORKER, SCOPE, run["id"], "worker")
    snapshot = {"policy_version": "1", "skill_version": "1", "rule_version": "1", "classification": 0,
                "input_hash": "a" * 64, "valid_until": OLD,
                "documents": [{"id": "aurora-evt-sop", "version": "v1", "source_hash": SOP_HASH}]}
    result = {"status": "SUCCEEDED", "cleanup": "REMOVED", "artifacts": [], "stdout": "synthetic"}
    store.complete_check(WORKER, SCOPE, run["id"], claim["token"], claim["epoch"], result,
                         {"status": "PASS", "evidence": []}, snapshot)
    return store, run["id"]


def versions(store):
    with store._tx() as session:
        document = session.scalar(select(Document).where(*filters(Document, SCOPE), Document.id == "aurora-evt-sop"))
        rows = session.scalars(select(DocumentVersion).where(
            *filters(DocumentVersion, SCOPE), DocumentVersion.document_id == "aurora-evt-sop"
        ).order_by(DocumentVersion.version)).all()
        return (document.current_version, {row.version: row.source_hash for row in rows},
                [str(row.valid_until)[:10] for row in rows])


def test_a_dry_run_lists_the_affected_runs_and_writes_nothing(tmp_path):
    store, run_id = seed(tmp_path / "live-copy.sqlite")
    before = versions(store)
    plan = renewal().renew(store, SCOPE, "aurora-evt-sop", valid_until=NEW)
    assert plan["status"] == "DRY_RUN" and plan["from_version"] == "v1" and plan["to_version"] == "v2"
    assert [item["run_id"] for item in plan["affected"]] == [run_id]
    assert plan["affected"][0]["pending_approvals"] and "SOURCE_CHANGED" in plan["effect"]
    assert versions(store) == before


def test_apply_adds_a_later_version_with_the_same_content_and_keeps_the_old_one(tmp_path):
    store, _ = seed(tmp_path / "live-copy.sqlite")
    done = renewal().renew(store, SCOPE, "aurora-evt-sop", valid_until=NEW, apply=True)
    assert done["status"] == "RENEWED" and done["to_version"] == "v2"
    current, hashes, expiries = versions(store)
    assert current == "v2" and hashes == {"v1": SOP_HASH, "v2": SOP_HASH}
    assert expiries == ["2026-11-05", "2027-04-05"]
    again = renewal().renew(store, SCOPE, "aurora-evt-sop", valid_until=NEW, apply=True)
    assert again["status"] == "ALREADY_CURRENT" and len(versions(store)[1]) == 2


def test_runs_that_cite_the_old_version_fail_the_freshness_check_and_new_ones_pass(tmp_path):
    store, run_id = seed(tmp_path / "live-copy.sqlite")
    renewal().renew(store, SCOPE, "aurora-evt-sop", valid_until=NEW, apply=True)
    with store._tx() as session:
        run = store._get(session, Run, SCOPE, run_id)
        with pytest.raises(DomainError) as stale:
            store._fresh(session, ADMIN, SCOPE, run)
        assert stale.value.code == "SOURCE_CHANGED"
        run.snapshot_json = {**run.snapshot_json, "documents": [
            {"id": "aurora-evt-sop", "version": "v2", "source_hash": SOP_HASH}], "valid_until": NEW}
        store._fresh(session, ADMIN, SCOPE, run)


def test_the_renewal_must_be_later_and_the_document_must_exist(tmp_path):
    store, _ = seed(tmp_path / "live-copy.sqlite")
    with pytest.raises(DomainError, match="RENEWAL_NOT_LATER"):
        renewal().renew(store, SCOPE, "aurora-evt-sop", valid_until=OLD, apply=True)
    with pytest.raises(DomainError) as missing:
        renewal().renew(store, SCOPE, "no-such-document", valid_until=NEW)
    assert missing.value.code == "NOT_FOUND"


def test_a_copy_can_be_rehearsed_while_the_original_stays_untouched(tmp_path):
    original, _ = seed(tmp_path / "original.sqlite")
    shutil.copy(tmp_path / "original.sqlite", tmp_path / "rehearsal.sqlite")
    rehearsal = EVTBoundStore(f"sqlite:///{tmp_path / 'rehearsal.sqlite'}")
    renewal().renew(rehearsal, SCOPE, "aurora-evt-sop", valid_until=NEW, apply=True)
    assert versions(rehearsal)[0] == "v2" and versions(original)[0] == "v1"


def test_the_default_export_grant_allows_both_versions_of_the_sop():
    allowed = {(grant.id, grant.version, grant.source_hash) for grant in ExportGrant().documents}
    assert allowed == {("aurora-evt-sop", "v1", SOP_HASH), ("aurora-evt-sop", "v2", SOP_HASH)}


def test_the_command_needs_an_explicit_database_and_is_a_dry_run_unless_told_otherwise(tmp_path, monkeypatch, capsys):
    store, _ = seed(tmp_path / "target.sqlite")
    url = f"sqlite:///{tmp_path / 'target.sqlite'}"
    base = ["--scope", "synthetic-lab/engineering/aurora", "--document", "aurora-evt-sop", "--valid-until", NEW]
    with pytest.raises(SystemExit):
        renewal().main(base)  # no --database-url: it must never fall back to the configured database
    renewal().main(["--database-url", url, *base])
    assert json.loads(capsys.readouterr().out)["status"] == "DRY_RUN" and versions(store)[0] == "v1"
    renewal().main(["--database-url", url, *base, "--apply"])
    assert json.loads(capsys.readouterr().out)["status"] == "RENEWED" and versions(store)[0] == "v2"


def test_the_renewal_module_never_reads_the_configured_database_or_runtime_files():
    tree = ast.parse((ROOT / "src/forgeflow/sop_renewal.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not ({"database_url", "RUNTIME", "load_store", "load_identities"} & (names | imported))
    assert ".runtime" not in (ROOT / "src/forgeflow/sop_renewal.py").read_text(encoding="utf-8")
