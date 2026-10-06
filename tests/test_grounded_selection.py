"""V2 TEST_DRIVER enum, budget, persistence and async catalog isolation."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from test_analysis_workflow import ROOT, checked
from test_analysis_workflow import env as base_environment
from test_grounded_workflow import EvidenceSelectionDriver

from forgeflow.analysis_workflow import ROLE_TOOLS
from forgeflow.contracts import DomainError, canonical, digest
from forgeflow.grounded_context import GroundedContextBuilder
from forgeflow.grounded_contracts import REVISION, semantic_check
from forgeflow.grounded_selection import (
    SELECTION_REVISION,
    GroundedSelectionWorkflow,
    schema_for_catalog,
)
from forgeflow.grounded_store import GroundedStore
from forgeflow.skills import SkillRegistry


@pytest.fixture
def env(tmp_path):
    return base_environment.__wrapped__(tmp_path)


def workflow(environment, scenario, provider, freeze=None):
    source, ledger, _, _, _, _ = environment
    evidence = GroundedStore(ledger)
    evidence.create_schema()
    return GroundedSelectionWorkflow(
        ledger, provider, GroundedContextBuilder(source, scenario),
        SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS),
        grounding_store=evidence, freeze_call=freeze,
    )


def fail(code, operation):
    with pytest.raises(DomainError) as error:
        operation()
    assert error.value.code == code


@pytest.mark.parametrize("scenario,fixture,status,concerns", [
    ("M01", "aurora-pass", "COMPLETE", 0),
    ("M01-blocked", "aurora-blocked", "INCOMPLETE", 1),
    ("M02", "aurora-unknown", "INCOMPLETE", 1),
    ("M03", "aurora-pass", "INCOMPLETE", 2),
    ("M04", "aurora-pass", "COMPLETE", 1),
])
async def test_v2_all_frozen_behavior_has_bound_enum_and_unchanged_budget(
    env, scenario, fixture, status, concerns
):
    source, _, scope, caller, _, _ = env
    parent = checked(env, fixture)
    provider = EvidenceSelectionDriver()
    frozen = []
    engine = workflow(env, scenario, provider, frozen.append)
    result = await engine.analyze(caller, scope, parent["id"], "selection-v2:" + scenario)
    assert result["state"] == status
    assert len(provider.calls) == len(frozen) == 4
    assert all(call["state"] == "VALIDATED" for call in result["calls"])
    records = engine.grounding_store.list(caller, scope, result["id"], reauthorize=engine._reauthorize)
    assert len(records) == 4 and all(record["semantic_valid"] for record in records)
    assert len(result["calls"][2]["output_json"]["concerns"]) == concerns
    for index, provider_call in enumerate(provider.calls):
        bound = len(canonical({"messages": provider_call["messages"], "schema": provider_call["schema"]}).encode()) + 384
        assert bound <= 3500, (scenario, index, bound)
        if index < 2:
            continue
        catalog = records[index]["claim_catalog"]
        for field in ("observations", "concerns"):
            definition = provider_call["schema"]["properties"][field]
            if catalog[field]:
                assert definition["items"]["enum"] == [item["id"] for item in catalog[field]]
            else:
                assert definition["maxItems"] == 0 and "enum" not in definition["items"]
        assert "Pending human approval does not make analysis INCOMPLETE" in provider_call["messages"][0]["content"]
    assert source.get_run(caller, scope, parent["id"]) == parent
    restarted = workflow(env, scenario, provider)
    assert await restarted.analyze(caller, scope, parent["id"], "selection-v2:" + scenario) == result
    assert len(provider.calls) == 4
    fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda: engine._schema("report"))


def test_material_binds_full_catalog_revision_and_all_dynamic_schemas(env):
    _, _, scope, caller, _, _ = env
    parent = checked(env)
    engine = workflow(env, "M01", EvidenceSelectionDriver())
    bundle = engine.context.build(caller, scope, parent["id"])
    material = engine._material(engine._activate(), bundle)
    catalog = engine.context.catalog(bundle)
    assert material["schema_catalog"] == catalog
    assert material["prompt_revision"] == material["analysis_revision"] == SELECTION_REVISION
    assert material["claim_catalog_hash"] == digest(catalog)
    assert material["output_schema_hash"] == digest({
        phase: schema_for_catalog(phase, catalog) for phase in ("plan", "context", "report", "summary")
    })
    with engine.context.store._tx() as session:
        engine._reauthorize(session, caller, scope, material)
    fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda: engine._schema("summary"))


@pytest.mark.parametrize("field,new_value,code", [
    ("analysis_revision", REVISION, "GROUNDING_REVISION_CHANGED"),
    ("prompt_revision", "other", "GROUNDING_REVISION_CHANGED"),
    ("claim_catalog_hash", "a" * 64, "GROUNDING_CATALOG_CHANGED"),
    ("output_schema_hash", "a" * 64, "GROUNDING_REVISION_CHANGED"),
])
def test_reauthorization_rejects_other_revisions_or_catalog_hash(env, field, new_value, code):
    _, _, scope, caller, _, _ = env
    parent = checked(env)
    engine = workflow(env, "M01", EvidenceSelectionDriver())
    bundle = engine.context.build(caller, scope, parent["id"])
    material = engine._material(engine._activate(), bundle)
    material[field] = new_value
    with engine.context.store._tx() as session:
        fail(code, lambda: engine._reauthorize(session, caller, scope, material))


@pytest.mark.parametrize("mutation", ["value", "source_ref", "claim_id"])
def test_full_catalog_forgery_is_rederived_from_sources_even_after_rehash(env, mutation):
    _, _, scope, caller, _, _ = env
    parent = checked(env)
    engine = workflow(env, "M01", EvidenceSelectionDriver())
    bundle = engine.context.build(caller, scope, parent["id"])
    material = engine._material(engine._activate(), bundle)
    catalog = material["schema_catalog"]
    key, value = {
        "value": ("value", 999),
        "source_ref": ("source_ref", "src_" + "a" * 32),
        "claim_id": ("id", "c_" + "a" * 16),
    }[mutation]
    catalog["observations"][0][key] = value
    material["claim_catalog_hash"] = digest(catalog)
    material["source_snapshot"]["catalog_hash"] = digest(catalog)
    material["output_schema_hash"] = digest({
        phase: schema_for_catalog(phase, catalog) for phase in ("plan", "context", "report", "summary")
    })
    with engine.context.store._tx() as session:
        fail("GROUNDING_CATALOG_CHANGED", lambda: engine._reauthorize(session, caller, scope, material))


def test_schema_does_not_supply_status_or_fixed_completed_report(env):
    _, _, scope, caller, _, _ = env
    parent = checked(env)
    engine = workflow(env, "M01", EvidenceSelectionDriver())
    bundle = engine.context.build(caller, scope, parent["id"])
    catalog = engine.context.catalog(bundle)
    for phase in ("report", "summary"):
        result = schema_for_catalog(phase, catalog)
        assert result["properties"]["status"]["enum"] == ["COMPLETE", "INCOMPLETE"]
        assert "const" not in result["properties"]["status"]
        for field in ("observations", "concerns"):
            assert "const" not in result["properties"][field]


@pytest.mark.parametrize("corruption,code", [
    ("duplicate", "DUPLICATE_OBSERVATIONS"),
    ("wrong_hash", "EVIDENCE_COVERAGE_OBSERVATIONS"),
    ("wrong_status", "UNSUPPORTED_COMPLETION_STATUS"),
])
async def test_v2_keeps_semantic_rejection_and_raw_reply_for_bad_provider_selection(env, corruption, code):
    _, _, scope, caller, _, _ = env
    parent = checked(env)

    class CorruptProvider(EvidenceSelectionDriver):
        async def generate(self, call):
            reply = await super().generate(call)
            if len(self.calls) == 3:
                output = reply["content"]
                if corruption == "duplicate":
                    output["observations"][0] = output["observations"][1]
                elif corruption == "wrong_hash":
                    output["observations"][0] = "c_" + "a" * 16
                else:
                    output["status"] = "INCOMPLETE"
                reply["content_utf8"] = json.dumps(output)
            return reply

    provider = CorruptProvider()
    engine = workflow(env, "M01", provider)
    result = await engine.analyze(caller, scope, parent["id"], "corrupt:" + corruption)
    assert result["state"] == "FAILED" and len(provider.calls) == 3
    records = engine.grounding_store.list(caller, scope, result["id"], reauthorize=engine._reauthorize)
    report_call_id = next(call["id"] for call in result["calls"] if call["phase"] == "FINAL_REPORT")
    record = next(item for item in records if item["call_id"] == report_call_id)
    assert record["structure_valid"] is True and record["semantic_valid"] is False
    assert code in record["codes"]
    assert record["rendered_report"] is None
    journal = engine.grounding_store.raw(caller, scope, result["id"], record["call_id"], reauthorize=engine._reauthorize)
    assert journal["provider_reply"]["content_utf8"] == record["raw_reply"]
    fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda: engine._schema("report"))


async def test_async_calls_isolate_catalogs_and_reset_after_return(env, tmp_path):
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_env = base_environment.__wrapped__(other_path)
    _, _, scope, caller, _, _ = env
    _, _, other_scope, other_caller, _, _ = other_env
    parent, other_parent = checked(env), checked(other_env)
    paused, released = asyncio.Event(), asyncio.Event()
    engine_holder = {}

    class PausingProvider(EvidenceSelectionDriver):
        async def generate(self, call):
            reply = await super().generate(call)
            if len(self.calls) == 3:
                # The other engine must not borrow this task's ambient catalog.
                fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda: engine_holder["other"]._schema("report"))
                paused.set()
                await released.wait()
            return reply

    first_provider, second_provider = PausingProvider(), EvidenceSelectionDriver()
    first = workflow(env, "M01", first_provider)
    second = workflow(other_env, "M03", second_provider)
    engine_holder["other"] = second
    first_task = asyncio.create_task(first.analyze(caller, scope, parent["id"], "first-v2"))
    await asyncio.wait_for(paused.wait(), 5)
    second_result = await second.analyze(other_caller, other_scope, other_parent["id"], "second-v2")
    released.set()
    first_result = await asyncio.wait_for(first_task, 5)
    assert first_result["state"] == "COMPLETE" and second_result["state"] == "INCOMPLETE"
    first_enum = first_provider.calls[2]["schema"]["properties"]["observations"]["items"]["enum"]
    second_enum = second_provider.calls[2]["schema"]["properties"]["observations"]["items"]["enum"]
    assert set(first_enum).isdisjoint(second_enum)
    for engine in (first, second):
        fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda engine=engine: engine._schema("report"))
    other_env[0].engine.dispose()


async def test_contextvar_resets_after_provider_exception(env):
    _, _, scope, caller, _, _ = env
    parent = checked(env)

    class ExplodingProvider:
        async def generate(self, call):
            raise RuntimeError("TEST_DRIVER_PROVIDER_FAILURE")

    engine = workflow(env, "M01", ExplodingProvider())
    with pytest.raises(RuntimeError, match="TEST_DRIVER_PROVIDER_FAILURE"):
        await engine.analyze(caller, scope, parent["id"], "provider-failure-v2")
    fail("GROUNDING_SCHEMA_CONTEXT_REQUIRED", lambda: engine._schema("summary"))


def test_semantic_validator_still_checks_all_five_claims_with_enum(env):
    _, _, scope, caller, _, _ = env
    parent = checked(env)
    engine = workflow(env, "M01", EvidenceSelectionDriver())
    bundle = engine.context.build(caller, scope, parent["id"])
    catalog = engine.context.catalog(bundle)
    ids = [item["id"] for item in catalog["observations"]]
    output = {"status": "COMPLETE", "observations": ids, "concerns": [], "recommendation": "HUMAN_REVIEW_REQUIRED"}
    assert semantic_check(catalog, "report", output)["semantic_valid"] is True
    duplicated = copy.deepcopy(output)
    duplicated["observations"][0] = duplicated["observations"][1]
    assert semantic_check(catalog, "report", duplicated)["semantic_valid"] is False
