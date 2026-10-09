"""Model analysis on a Fabric parent run (same run as the Gate and the approvals), with a Fabric-only grant.

The default export grant only knows the Aurora scope and its SOP. The Fabric project has its own SOP text, so it
needs its own grant; nothing is widened for the Aurora scope, and a Fabric grant cannot reach Aurora runs.
"""

import hashlib
import json
from pathlib import Path

import pytest
from test_analysis_workflow import TestDriverProvider

from forgeflow import contracts, fabric_runtime, local
from forgeflow.analysis_context import SYNTHETIC_SCOPE, AnalysisContextBuilder
from forgeflow.analysis_store import AnalysisStore
from forgeflow.analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from forgeflow.contracts import DomainError, Principal, RunRequest, digest
from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_runtime import FabricStore
from forgeflow.fabric_store import FabricOperations
from forgeflow.fixture_family import evidence_valid_until
from forgeflow.gate import RULE_HASH, evaluate
from forgeflow.policy import POLICY_HASH
from forgeflow.skills import SkillRegistry
from forgeflow.templates import template_hash

ROOT = Path(__file__).resolve().parents[1]
ROLES = {"admin", "submitter", "supervisor", "hardware", "pm", "reviewer", "worker", "fabric_executor", "memory_writer"}


def runtime():
    from forgeflow import analysis_runtime

    return analysis_runtime


@pytest.fixture
def env(tmp_path):
    store = FabricStore(f"sqlite:///{tmp_path / 'fabric-analysis.sqlite'}")
    store.create_schema()
    caller = Principal(subject_id="fabric-analysis-owner", kind="user", scopes=(FABRIC_SCOPE,),
                       roles=frozenset({"submitter", "pm", "supervisor", "hardware", "admin", "reviewer"}))
    worker = Principal(subject_id="TEST_DRIVER_HARDWARE", kind="service", scopes=(FABRIC_SCOPE,),
                       roles=frozenset({"hardware", "worker"}))
    store.ensure_project(FABRIC_SCOPE)
    store.put_document(caller, FABRIC_SCOPE, "aurora-evt-sop", "v1", fabric_runtime.FABRIC_SOP_CONTENT,
                       {"hardware", "pm", "supervisor", "admin", "reviewer", "submitter"}, 0,
                       valid_until="2027-04-05T00:00:00Z")
    return store, caller, worker


def checked(env, fixture="aurora-pass-v2"):
    store, caller, worker = env
    run = store.submit(caller, RunRequest(scope=FABRIC_SCOPE, fixture_id=fixture), fixture)
    lease = store.claim(worker, FABRIC_SCOPE, run["id"], "TEST_DRIVER", ttl=90)
    manifest = json.loads((ROOT / "fixtures" / f"{fixture}.json").read_text())
    result = {"status": "SUCCEEDED", "cleanup": "REMOVED", "synthetic": True, "orchestration": "TEST_DRIVER",
              "scan": {"status": "TEST_DRIVER"}, "mcp": {"status": "TEST_DRIVER"}}
    snapshot = {"fixture_version": "v1", "input_hash": digest(manifest), "input_manifest_hash": digest(manifest),
                "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH, "code_hash": template_hash("evt-check"),
                "documents": [{"id": "aurora-evt-sop", "version": "v1",
                               "source_hash": hashlib.sha256(fabric_runtime.FABRIC_SOP_CONTENT.encode()).hexdigest()}],
                "classification": 0, "valid_until": evidence_valid_until(manifest)}
    return store.complete_check(worker, FABRIC_SCOPE, run["id"], lease["token"], lease["epoch"], result,
                                evaluate(manifest, result, now=contracts.utcnow()), snapshot)


def test_the_fabric_sop_text_is_one_constant_and_the_grant_names_exactly_its_hash():
    digest_of = hashlib.sha256(fabric_runtime.FABRIC_SOP_CONTENT.encode()).hexdigest()
    assert fabric_runtime.FABRIC_SOP_HASH == digest_of
    grant = runtime().fabric_grant()
    assert grant.scope == FABRIC_SCOPE
    assert [(item.id, item.version, item.source_hash) for item in grant.documents] == [
        ("aurora-evt-sop", "v1", digest_of)]


def test_initialize_fabric_seeds_exactly_that_text(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGEFLOW_DATABASE_URL", f"sqlite:///{tmp_path / 'init.sqlite'}")
    monkeypatch.setattr(fabric_runtime, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(local, "RUNTIME", tmp_path / "runtime")
    fabric_runtime.initialize_fabric()
    store = FabricStore(f"sqlite:///{tmp_path / 'init.sqlite'}")
    rows = store.document_rows(Principal(subject_id="x", kind="user", roles=frozenset({"admin"}),
                                         scopes=(FABRIC_SCOPE,)), FABRIC_SCOPE)
    assert [row["source_hash"] for row in rows] == [fabric_runtime.FABRIC_SOP_HASH]


@pytest.mark.asyncio
async def test_four_model_calls_complete_on_a_fabric_parent_run_and_leave_it_unchanged(env):
    store, caller, _ = env
    parent = checked(env)
    review_before = FabricOperations(store)
    review_before.create_schema()
    technical_before = review_before.create(caller, FABRIC_SCOPE, parent["id"], "before")["report_json"]["technical_hash"]
    provider = TestDriverProvider()
    _, workflow = runtime().build_fabric_runtime(store, provider)
    result = await workflow.analyze(caller, FABRIC_SCOPE, parent["id"], "fabric-normal")
    assert result["state"] == "COMPLETE" and len(provider.calls) == 4
    assert store.get_run(caller, FABRIC_SCOPE, parent["id"]) == parent
    assert review_before.create(caller, FABRIC_SCOPE, parent["id"], "after")["report_json"]["technical_hash"] == technical_before


@pytest.mark.asyncio
async def test_the_default_grant_does_not_open_the_fabric_scope(env, tmp_path):
    store, caller, _ = env
    parent = checked(env)
    ledger = AnalysisStore(store)
    ledger.create_schema()
    registry = SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS)
    workflow = AnalysisWorkflow(ledger, TestDriverProvider(), AnalysisContextBuilder(store), registry)
    with pytest.raises(DomainError) as denied:
        await workflow.analyze(caller, FABRIC_SCOPE, parent["id"], "default-grant")
    assert denied.value.code == "EXPORT_DENIED"


@pytest.mark.asyncio
async def test_the_fabric_grant_cannot_analyze_an_aurora_run(env):
    store, caller, _ = env
    provider = TestDriverProvider()
    _, workflow = runtime().build_fabric_runtime(store, provider)
    aurora_caller = Principal(subject_id="aurora-owner", kind="user", scopes=(SYNTHETIC_SCOPE,),
                              roles=frozenset({"submitter", "pm", "supervisor", "hardware", "admin"}))
    with pytest.raises(DomainError) as denied:
        await workflow.analyze(aurora_caller, SYNTHETIC_SCOPE, "no-such-run", "cross")
    assert denied.value.code in {"EXPORT_DENIED", "NOT_FOUND"} and provider.calls == []
