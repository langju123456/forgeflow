"""TEST_DRIVER: model selection bytes stay separate from server-derived state."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_analysis_workflow import checked
from test_analysis_workflow import env as base_environment
from test_s2c_checks import env as derived_environment
from test_s2c_checks import submitted

from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.contracts import DomainError
from forgeflow.grounded_contracts import QUESTION
from forgeflow.s2c_api import create_s2c_app
from forgeflow.s2c_runtime import S2CRuntimeFactory


@pytest.fixture
def env(tmp_path):
    return base_environment.__wrapped__(tmp_path)


class SelectionDriver:
    """Explicit fault/test selection generator; never model quality evidence."""
    def __init__(self, fault=None):
        self.calls = []
        self.fault = fault

    async def generate(self, call):
        self.calls.append(copy.deepcopy(call))
        body = json.loads(call["messages"][1]["content"])
        properties = call["schema"]["properties"]
        if "assignment" in properties:
            output = {"assignment": "pm", "task_question": QUESTION, "source_refs": [body["source_ref"]]}
        elif "requests" in properties:
            output = {"requests": []}
        else:
            output = {"observations": [row[0] for row in body["observations"]],
                      "concerns": [row[0] for row in body["concerns"]]}
            if "recommendation" in properties:
                output["recommendation"] = "HUMAN_REVIEW_REQUIRED"
                if self.fault == "model_status":
                    output["status"] = "INCOMPLETE"
                elif self.fault == "duplicate":
                    output["observations"][0] = output["observations"][1]
                elif self.fault == "omit_concern":
                    output["concerns"] = []
                elif self.fault == "approve":
                    output["recommendation"] = "APPROVED"
            else:
                output.update(report_refs=["report_pm"], human_approval_required=True)
        return {"status": "SUCCEEDED", "content": output, "content_utf8": json.dumps(output),
                "actual_model_revision": MODEL_DIGEST, "finish_reason": "stop", "dispatched": True,
                "completion_confirmed": True, "transport": "TEST_DRIVER", "elapsed_seconds": 0,
                "usage": {"state": "CONFIRMED", "input_tokens": 200, "output_tokens": 100, "cost_microusd": None}}


@pytest.mark.parametrize("scenario,fixture,status", [
    ("M01", "aurora-pass", "COMPLETE"), ("M01-blocked", "aurora-blocked", "INCOMPLETE"),
    ("M02", "aurora-unknown", "INCOMPLETE"), ("M04", "aurora-pass", "COMPLETE"),
])
async def test_real_selection_is_unchanged_and_state_is_separately_derived(env, scenario, fixture, status):
    source, _, scope, caller, _, _ = env
    parent = checked(env, fixture)
    driver = SelectionDriver()
    frozen = []
    factory = S2CRuntimeFactory(source, driver, freeze_call=frozen.append)
    result = await factory(scenario).analyze(caller, scope, parent["id"], "s2c:" + scenario)
    assert result["state"] == status and len(driver.calls) == len(frozen) == 4
    assert result["summary_json"]["server_projection"]["status"] == status
    assert result["summary_json"]["server_projection"]["status_authority"] == "SERVER_DERIVED_AUTHORIZED_CATALOG"
    assert "status" not in result["summary_json"]["selection"]
    records = factory.grounding.list(caller, scope, result["id"], reauthorize=factory.reauthorize)
    by_call = {item["call_id"]: item for item in records}
    for call in result["calls"]:
        assert call["state"] == "VALIDATED"
        assert "status" not in call["output_json"]
        assert json.loads(by_call[call["id"]]["raw_reply"]) == call["output_json"]
        if call["phase"] in {"FINAL_REPORT", "SUMMARY"}:
            assert "status" not in next(item for item in frozen if item["call_id"] == call["id"])["schema"]["properties"]
            assert by_call[call["id"]]["rendered_report"]["status"] == status
    assert source.get_run(caller, scope, parent["id"]) == parent
    fresh = S2CRuntimeFactory(source, driver)
    replay = await fresh(scenario).analyze(caller, scope, parent["id"], "s2c:" + scenario)
    assert replay == result and len(driver.calls) == 4
    assert fresh.grounding.list(caller, scope, result["id"], reauthorize=fresh.reauthorize) == records


@pytest.mark.parametrize("fault", ["model_status", "duplicate", "omit_concern", "approve"])
async def test_model_cannot_decide_state_omit_evidence_or_approve(env, fault):
    source, _, scope, caller, _, _ = env
    parent = checked(env)
    driver = SelectionDriver(fault)
    factory = S2CRuntimeFactory(source, driver)
    result = await factory("M04").analyze(caller, scope, parent["id"], "bad:" + fault)
    assert result["state"] == "FAILED" and len(driver.calls) == 3
    records = factory.grounding.list(caller, scope, result["id"], reauthorize=factory.reauthorize)
    failed_call = next(call for call in result["calls"] if call["phase"] == "FINAL_REPORT")
    record = next(item for item in records if item["call_id"] == failed_call["id"])
    assert record["rendered_report"] is None
    assert source.get_run(caller, scope, parent["id"]) == parent


def test_api_replay_and_authority_inputs_fail_closed(env):
    source, _, scope, caller, _, _ = env
    parent = checked(env)
    driver = SelectionDriver()
    factory = S2CRuntimeFactory(source, driver)
    foreign_scope = scope.model_copy(update={"tenant_id": "foreign"})
    foreign = caller.model_copy(update={"scopes": (foreign_scope,)})
    client = TestClient(create_s2c_app(source, {"owner": caller, "foreign": foreign}, factory, test_harness=True))
    headers = {"Authorization": "Bearer owner", "Idempotency-Key": "api-v3"}
    route = f"/runs/{parent['id']}/grounded-analysis"
    body = {"scope": scope.model_dump(mode="json"), "scenario_id": "M04"}
    response = client.post(route, json=body, headers=headers)
    assert response.status_code == 200 and response.json()["state"] == "COMPLETE"
    assert client.post(route, json=body, headers=headers).json() == response.json()
    assert len(driver.calls) == 4
    aid = response.json()["id"]
    for extra in ({"status": "COMPLETE"}, {"role": "hardware"}, {"manifest": {}}):
        assert client.post(route, json={**body, **extra}, headers=headers).status_code == 422
    assert client.get(f"/grounded-analyses/{aid}", params=scope.model_dump()).status_code == 401
    assert client.get(f"/grounded-analyses/{aid}", params=scope.model_dump(),
                      headers={"Authorization": "Bearer foreign"}).status_code in {403, 404}
    assert len(driver.calls) == 4
    with pytest.raises(DomainError):
        factory("M05")


def test_check_api_authorizes_human_hardware_before_worker_boundary(env, monkeypatch):
    source, _, scope, caller, _, _ = env
    parent = checked(env)
    factory = S2CRuntimeFactory(source, SelectionDriver())
    pm = caller.model_copy(update={"roles": frozenset({"pm"})})
    agent = caller.model_copy(update={"kind": "agent"})
    calls = []

    async def check(scope, run):
        calls.append(run["id"])
        return run

    monkeypatch.setattr(factory, "check_parent", check)
    client = TestClient(create_s2c_app(source, {"owner": caller, "pm": pm, "agent": agent}, factory, test_harness=True))
    route = f"/s2c/parents/{parent['id']}/check"
    body = scope.model_dump(mode="json")
    for token in ("pm", "agent"):
        assert client.post(route, json=body, headers={"Authorization": "Bearer " + token}).status_code in {403, 404}
    assert calls == []
    assert client.post(route, json=body, headers={"Authorization": "Bearer owner"}).status_code == 200
    assert calls == [parent["id"]]


async def test_m03_derived_check_then_four_call_selection_and_restart(tmp_path):
    generator = derived_environment.__wrapped__(tmp_path)
    derived = next(generator)
    try:
        source, _, scope, caller, checker, *_ = derived
        parent = submitted(derived, "whole-derived-chain")
        await checker.check(scope, parent["id"])
        before = source.get_run(caller, scope, parent["id"])
        assert before["assessment_json"]["status"] == "UNKNOWN"
        driver = SelectionDriver()
        factory = S2CRuntimeFactory(source, driver)
        result = await factory("M03").analyze(caller, scope, parent["id"], "derived-selection")
        assert result["state"] == "INCOMPLETE" and len(driver.calls) == 4
        rendered = result["summary_json"]["rendered_report"]
        assert len(rendered["observations"]) == 5 and len(rendered["concerns"]) == 2
        assert all(item["authority"] == "HARDWARE_CHECK" for item in rendered["observations"])
        assert {item["evidence_state"] for item in rendered["observations"]} == {"MEASURED", "CONFLICT", "EXPIRED"}
        assert source.get_run(caller, scope, parent["id"]) == before
        fresh = S2CRuntimeFactory(source, driver)
        assert await fresh("M03").analyze(caller, scope, parent["id"], "derived-selection") == result
        assert len(driver.calls) == 4
    finally:
        generator.close()
