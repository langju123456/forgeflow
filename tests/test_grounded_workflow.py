"""TEST_DRIVER checks for the new constrained application over the real ledger."""

import copy
import json

import pytest
from test_analysis_workflow import ROOT, checked
from test_analysis_workflow import env as base_environment

from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.analysis_workflow import ROLE_TOOLS
from forgeflow.grounded_context import GroundedContextBuilder
from forgeflow.grounded_contracts import QUESTION
from forgeflow.grounded_store import GroundedStore
from forgeflow.grounded_workflow import GroundedWorkflow
from forgeflow.skills import SkillRegistry


@pytest.fixture
def env(tmp_path):
    return base_environment.__wrapped__(tmp_path)


class EvidenceSelectionDriver:
    """Catalog selection driver, explicitly not model quality evidence."""
    def __init__(self, *, corrupt=False):
        self.calls = []
        self.corrupt = corrupt

    async def generate(self, call):
        self.calls.append(copy.deepcopy(call))
        data = json.loads(call["messages"][1]["content"])
        phase = len(self.calls)
        if phase == 1:
            output = {"assignment": "pm", "task_question": QUESTION, "source_refs": [data["source_ref"]]}
        elif phase == 2:
            output = {"requests": [{"tool": "knowledge.search", "query": "Aurora EVT", "top_k": 1}]}
        else:
            output = {
                "status": "COMPLETE" if all(row[3] == "SATISFIED" and row[4] == "MEASURED"
                                            for row in data["observations"]) else "INCOMPLETE",
                "observations": [row[0] for row in data["observations"]],
                "concerns": [row[0] for row in data["concerns"]],
            }
            if phase == 3:
                output["recommendation"] = "HUMAN_REVIEW_REQUIRED"
                if self.corrupt:
                    output["concerns"].append("c_" + "f" * 16)
            else:
                output.update(report_refs=["report_pm"], human_approval_required=True)
        return {
            "status": "SUCCEEDED", "content": output, "content_utf8": json.dumps(output),
            "actual_model_revision": MODEL_DIGEST, "finish_reason": "stop", "dispatched": True,
            "completion_confirmed": True, "transport": "TEST_DRIVER",
            "usage": {"state": "CONFIRMED", "input_tokens": 200, "output_tokens": 100,
                      "cost_microusd": None},
        }


def workflow(env, scenario, provider, freeze=None):
    source, ledger, _, _, _, _ = env
    evidence = GroundedStore(ledger)
    evidence.create_schema()
    return GroundedWorkflow(
        ledger, provider, GroundedContextBuilder(source, scenario),
        SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS), grounding_store=evidence, freeze_call=freeze,
    )


@pytest.mark.parametrize("scenario,fixture,status,concerns", [
    ("M01", "aurora-pass", "COMPLETE", 0),
    ("M01-blocked", "aurora-blocked", "INCOMPLETE", 1),
    ("M02", "aurora-unknown", "INCOMPLETE", 1),
    ("M03", "aurora-pass", "INCOMPLETE", 2),
    ("M04", "aurora-pass", "COMPLETE", 1),
])
async def test_grounded_selection_exactly_covers_all_frozen_behavior(env, scenario, fixture, status, concerns):
    source, _, scope, caller, _, _ = env
    parent = checked(env, fixture)
    driver = EvidenceSelectionDriver()
    freezes = []
    engine = workflow(env, scenario, driver, freezes.append)
    result = await engine.analyze(caller, scope, parent["id"], "grounded:" + scenario)
    assert result["state"] == status
    assert len(driver.calls) == len(freezes) == 4
    assert all(call["state"] == "VALIDATED" for call in result["calls"])
    records = engine.grounding_store.list(caller, scope, result["id"], reauthorize=engine._reauthorize)
    raw_records = engine.grounding_store.raw_list(caller, scope, result["id"], reauthorize=engine._reauthorize)
    assert len(records) == 4
    assert len(raw_records) == 4
    assert all(record["raw_journal_hash"] is not None for record in records)
    assert all(record["structure_valid"] and record["semantic_valid"] for record in records)
    assert len(result["calls"][2]["output_json"]["concerns"]) == concerns
    report = next(record["rendered_report"] for record in records if record["rendered_report"] is not None)
    assert len(report["observations"]) == 5
    assert report["human_approval_required"] is True
    assert source.get_run(caller, scope, parent["id"]) == parent
    replay = await workflow(env, scenario, driver).analyze(caller, scope, parent["id"], "grounded:" + scenario)
    assert replay == result and len(driver.calls) == 4


async def test_valid_structure_unfounded_unknown_is_retained_and_denied(env):
    source, _, scope, caller, _, _ = env
    parent = checked(env)
    driver = EvidenceSelectionDriver(corrupt=True)
    engine = workflow(env, "M01", driver)
    result = await engine.analyze(caller, scope, parent["id"], "unfounded")
    assert result["state"] == "FAILED"
    assert len(driver.calls) == 3
    records = engine.grounding_store.list(caller, scope, result["id"], reauthorize=engine._reauthorize)
    record = next(item for item in records if item["semantic_valid"] is False)
    assert record["structure_valid"] is True
    assert record["codes"] == ["EVIDENCE_COVERAGE_CONCERNS"]
    assert json.loads(record["raw_reply"])["concerns"] == ["c_" + "f" * 16]
    assert record["rendered_report"] is None
    raw = engine.grounding_store.raw(caller, scope, result["id"], record["call_id"], reauthorize=engine._reauthorize)
    assert raw["provider_reply"]["content_utf8"] == record["raw_reply"]
    assert source.get_run(caller, scope, parent["id"]) == parent
