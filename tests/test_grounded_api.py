"""TEST_DRIVER HTTP authority and durable grounded publication checks.

The parent check and provider outputs are explicit local test fixtures. These
tests do not count as real Docker/MCP checks or real model-quality evaluations.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_analysis_workflow import checked
from test_analysis_workflow import env as env

from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.analysis_store import AnalysisRun
from forgeflow.contracts import DomainError, RunRequest, Scope
from forgeflow.grounded_api import create_grounded_app
from forgeflow.grounded_contracts import QUESTION, REVISION
from forgeflow.grounded_runtime import GroundedRuntimeFactory, build_runtime, resolve_scenario
from forgeflow.store import Document, filters


class GroundedTestDriver:
    __test__ = False

    def __init__(self):
        self.calls = []

    async def generate(self, call):
        self.calls.append(copy.deepcopy(call))
        body = json.loads(call["messages"][-1]["content"])
        properties = call["schema"]["properties"]
        if "assignment" in properties:
            output = {"assignment": "pm", "task_question": QUESTION, "source_refs": [body["source_ref"]]}
        elif "requests" in properties:
            output = {"requests": []}
        elif "human_approval_required" in properties:
            report = body["pm_report"]
            output = {
                "status": report["status"], "observations": report["observations"],
                "concerns": report["concerns"], "report_refs": ["report_pm"],
                "human_approval_required": True,
            }
        else:
            observations = [dict(zip(body["observations_columns"], item, strict=True)) for item in body["observations"]]
            concerns = [dict(zip(body["concerns_columns"], item, strict=True)) for item in body["concerns"]]
            complete = all(
                item["check_state"] == "SATISFIED" and item["evidence_state"] == "MEASURED"
                for item in observations
            )
            output = {
                "status": "COMPLETE" if complete else "INCOMPLETE",
                "observations": [item["id"] for item in observations],
                "concerns": [item["id"] for item in concerns], "recommendation": "HUMAN_REVIEW_REQUIRED",
            }
        return {
            "status": "SUCCEEDED", "content": output, "content_utf8": json.dumps(output),
            "actual_model_revision": MODEL_DIGEST, "finish_reason": "stop", "dispatched": True,
            "completion_confirmed": True, "transport": "TEST_DRIVER", "elapsed_seconds": 0,
            "usage": {"state": "CONFIRMED", "input_tokens": 200, "output_tokens": 100, "cost_microusd": None},
        }


@pytest.fixture
def api(env):
    source, _, scope, caller, _, _ = env
    provider = GroundedTestDriver()
    factory = GroundedRuntimeFactory(source, provider)
    other = Scope(tenant_id="other", workspace_id="engineering", project_id="aurora")
    intruder = caller.model_copy(update={"subject_id": "intruder", "scopes": (other,)})
    agent = caller.model_copy(update={"subject_id": "model", "kind": "agent"})
    identities = {"owner": caller, "other": intruder, "agent": agent}
    client = TestClient(create_grounded_app(source, identities, factory, test_harness=True))
    return client, factory, env, provider, identities


def post(api, *, scenario="M01", fixture="aurora-pass", token="owner", key="grounded-api", **extra):
    client, _, environment, _, _ = api
    parent = environment[0].submit(
        environment[3], RunRequest(scope=environment[2], fixture_id=fixture), fixture
    )
    if parent.get("assessment_json") is None:
        parent = checked(environment, fixture=fixture)
    response = client.post(
        f"/runs/{parent['id']}/grounded-analysis",
        json={"scope": environment[2].model_dump(), "scenario_id": scenario, **extra},
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": key},
    )
    return response, parent


def test_four_call_replay_records_separate_raw_and_rendered_output(api):
    client, factory, environment, provider, _ = api
    first, parent = post(api)
    assert first.status_code == 200
    analysis = first.json()
    assert analysis["state"] == "COMPLETE" and len(provider.calls) == 4
    second, _ = post(api)
    assert second.status_code == 200 and second.json() == analysis
    assert len(provider.calls) == 4
    scope, caller = environment[2:4]
    headers = {"Authorization": "Bearer owner"}
    records = client.get(
        f"/grounded-analyses/{analysis['id']}/records", params=scope.model_dump(), headers=headers
    )
    assert records.status_code == 200
    data = records.json()
    assert len(data) == 4 and all(item["semantic_valid"] is True for item in data)
    report = next(item for item in data if item["rendered_report"] is not None)
    raw = json.loads(report["raw_reply"]) if isinstance(report["raw_reply"], str) else report["raw_reply"]
    assert raw["observations"] == report["rendered_report"]["validated_selection"]["observations"]
    assert report["rendered_report"]["renderer"] == REVISION
    assert report["rendered_report"]["concerns"] == []
    assert report["provider_provenance"]["transport"] == "TEST_DRIVER"
    assert factory.source.get_run(caller, scope, parent["id"]) == parent
    events = client.get(
        f"/grounded-analyses/{analysis['id']}/events", params=scope.model_dump(), headers=headers
    )
    assert events.status_code == 200 and events.json()


@pytest.mark.parametrize("scenario,fixture", [("M01-blocked", "aurora-blocked"), ("M02", "aurora-unknown")])
def test_read_resolves_persisted_scenario_after_fresh_composition(api, scenario, fixture):
    _, factory, environment, provider, identities = api
    response, _ = post(api, scenario=scenario, fixture=fixture)
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["state"] == "INCOMPLETE"
    fresh = GroundedRuntimeFactory(factory.source, provider)
    client = TestClient(create_grounded_app(factory.source, identities, fresh, test_harness=True))
    assert not fresh.workflows
    headers = {"Authorization": "Bearer owner"}
    params = environment[2].model_dump()
    read = client.get(f"/grounded-analyses/{analysis['id']}", params=params, headers=headers)
    records = client.get(f"/grounded-analyses/{analysis['id']}/records", params=params, headers=headers)
    assert read.status_code == records.status_code == 200
    assert read.json() == analysis and list(fresh.workflows) == [scenario]
    assert len(provider.calls) == 4


@pytest.mark.parametrize("extra", [
    {"role": "hardware"}, {"provider_url": "https://attacker.invalid"}, {"approval": True},
    {"fault": "timeout"}, {"run_id": "forged"},
])
def test_body_cannot_grant_authority_change_provider_or_inject_faults(api, extra):
    response, _ = post(api, **extra)
    assert response.status_code == 422 and not api[3].calls


@pytest.mark.parametrize("scenario", ["M05", "M12", "M01-blocked-other", "../../M01"])
def test_public_scenario_allowlist_excludes_fault_cases_and_paths(api, scenario):
    response, _ = post(api, scenario=scenario)
    assert response.status_code == 422 and not api[3].calls


def test_forged_headers_agent_and_other_tenant_are_rejected_before_dispatch(api):
    client, _, environment, provider, _ = api
    assert post(api, token="other")[0].status_code == 404
    assert post(api, token="agent")[0].status_code in {403, 404}
    parent = environment[0].submit(
        environment[3], RunRequest(scope=environment[2], fixture_id="aurora-pass"), "aurora-pass"
    )
    response = client.post(
        f"/runs/{parent['id']}/grounded-analysis", json={"scope": environment[2].model_dump()},
        headers={"X-Role": "supervisor", "X-Tenant-ID": environment[2].tenant_id, "Idempotency-Key": "forged"},
    )
    assert response.status_code == 401 and not provider.calls


def test_read_records_events_cancel_cannot_cross_scope(api):
    client, _, environment, _, _ = api
    analysis = post(api)[0].json()
    params = environment[2].model_dump()
    headers = {"Authorization": "Bearer other"}
    for suffix in ("", "/records", "/events"):
        response = client.get(f"/grounded-analyses/{analysis['id']}{suffix}", params=params, headers=headers)
        assert response.status_code == 404
    response = client.post(f"/grounded-analyses/{analysis['id']}/cancel", json=params, headers=headers)
    assert response.status_code == 404


def test_source_revocation_rechecks_records_and_read_after_publication(api):
    client, factory, environment, provider, _ = api
    analysis = post(api)[0].json()
    scope = environment[2]
    with factory.source._tx() as session:
        document = session.scalar(select(Document).where(*filters(Document, scope), Document.id == "aurora-evt-sop"))
        document.roles = ["restricted"]
    headers = {"Authorization": "Bearer owner"}
    for suffix in ("", "/records"):
        response = client.get(f"/grounded-analyses/{analysis['id']}{suffix}", params=scope.model_dump(), headers=headers)
        assert response.status_code in {403, 404}
    assert len(provider.calls) == 4


def test_persisted_scenario_tampering_fails_closed(api):
    client, factory, environment, _, _ = api
    analysis = post(api)[0].json()
    scope = environment[2]
    with factory.source._tx() as session:
        row = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope), AnalysisRun.id == analysis["id"]))
        material = copy.deepcopy(row.material_json)
        material["source_snapshot"]["grounding"]["scenario_id"] = "M05"
        row.material_json = material
    response = client.get(
        f"/grounded-analyses/{analysis['id']}/records", params=scope.model_dump(),
        headers={"Authorization": "Bearer owner"},
    )
    assert response.status_code == 403 and response.json()["code"] == "FROZEN_SCENARIO_DENIED"


def test_public_guard_and_truthful_health(api, monkeypatch):
    client, factory, _, _, _ = api
    health = client.get("/grounded/health").json()
    assert health["identity"] == "DEV_ONLY" and health["automatic_business_actions"] is False
    assert health["model_task"] == "CONSTRAINED_EVIDENCE_SELECTION"
    assert health["accepted_prose"] == "DETERMINISTIC_SERVER_RENDERER"
    public = TestClient(create_grounded_app(factory.source, {}, factory))
    assert public.get("/grounded/health").status_code == 403
    monkeypatch.setenv("FORGEFLOW_MODE", "production")
    with pytest.raises(RuntimeError, match="DEV_ONLY"):
        create_grounded_app(factory.source, {}, factory)


def test_factory_shares_provider_and_rejects_cross_case_variant(env):
    provider = GroundedTestDriver()
    factory = GroundedRuntimeFactory(env[0], provider)
    assert factory("M01").provider is factory("M02").provider is provider
    assert factory("M01").store is factory("M02").store
    source, workflow = build_runtime(env[0], provider, case_id="M01", scenario="M01-blocked")
    assert source is env[0] and workflow.context.frozen_grant.scenario_id == "M01-blocked"
    for case, scenario in (("M02", "M01-blocked"), ("unknown", None)):
        with pytest.raises(DomainError, match="FROZEN_SCENARIO_DENIED"):
            resolve_scenario(case, scenario)
    with pytest.raises(DomainError, match="FROZEN_SCENARIO_DENIED"):
        factory("M05")


def _starter(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/start_grounded_api.py"
    spec = importlib.util.spec_from_file_location("grounded_starter_test", path)
    starter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(starter)
    monkeypatch.setattr(starter, "ROOT", tmp_path)
    # Unit-only generated files avoid the real runtime namespace, allowing the
    # portable review audit guard to continue rejecting every .runtime open.
    monkeypatch.setattr(starter, "RUNTIME", tmp_path / "synthetic-runtime")
    return starter


def test_starter_never_reclaims_an_unowned_listener(tmp_path, monkeypatch):
    starter = _starter(tmp_path, monkeypatch)
    monkeypatch.setattr(starter, "listening", lambda: True)
    monkeypatch.setattr(starter, "health", lambda: {"process_id": 99999})
    monkeypatch.setattr(starter, "own_pid", lambda: None)

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("Occupied port must not launch or stop a process")

    monkeypatch.setattr(starter.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(SystemExit, match="occupied; no process was stopped"):
        starter.main()


def test_starter_failure_terminates_only_its_new_child(tmp_path, monkeypatch):
    starter = _starter(tmp_path, monkeypatch)
    monkeypatch.setattr(starter, "listening", lambda: False)
    monkeypatch.setattr(starter, "health", lambda: None)
    clock = iter([0, 20])
    monkeypatch.setattr(starter.time, "monotonic", lambda: next(clock))
    actions = []

    class Child:
        pid = 99999

        def poll(self):
            return None

        def terminate(self):
            actions.append(("terminate", self.pid))

        def wait(self, timeout):
            actions.append(("wait", self.pid))

    def spawn(command, **options):
        assert command[-2:] == ["-m", "forgeflow.grounded_api"]
        assert options["cwd"] == tmp_path and options["start_new_session"] is True
        return Child()

    monkeypatch.setattr(starter.subprocess, "Popen", spawn)
    with pytest.raises(SystemExit, match="did not become ready"):
        starter.main()
    assert actions == [("terminate", 99999), ("wait", 99999)]
    assert not (starter.RUNTIME / "grounded-api.pid").exists()
