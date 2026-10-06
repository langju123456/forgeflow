"""TEST_DRIVER HTTP authority checks over the real Store and analysis workflow."""

import pytest
from fastapi.testclient import TestClient
from test_analysis_workflow import TestDriverProvider, checked
from test_analysis_workflow import env as env
from test_analysis_workflow import workflow as build_workflow

from forgeflow.analysis_api import create_analysis_app
from forgeflow.contracts import Scope


@pytest.fixture
def api(env):
    source, ledger, scope, caller, _, _ = env
    parent = checked(env)
    provider = TestDriverProvider()
    workflow = build_workflow(env, provider)
    other = Scope(tenant_id="other", workspace_id="engineering", project_id="aurora")
    intruder = caller.model_copy(update={"subject_id": "intruder", "scopes": (other,)})
    agent = caller.model_copy(update={"subject_id": "model", "kind": "agent"})
    client = TestClient(
        create_analysis_app(
            source, {"owner": caller, "other": intruder, "agent": agent}, workflow, test_harness=True
        )
    )
    return client, source, ledger, scope, caller, parent, provider


def post(client, scope, parent, *, token="owner", **extra):
    return client.post(
        "/analyses",
        json={"scope": scope.model_dump(), "run_id": parent["id"], **extra},
        headers={"Authorization": "Bearer " + token, "Idempotency-Key": "api-analysis"},
    )


def test_analysis_routes_share_parent_and_replay_without_new_calls(api):
    client, source, _, scope, caller, parent, provider = api
    first = post(client, scope, parent)
    assert first.status_code == 200
    result = first.json()
    assert result["state"] == "COMPLETE"
    assert len(result["calls"]) == len(provider.calls) == 4
    second = post(client, scope, parent)
    assert second.status_code == 200 and second.json()["id"] == result["id"]
    assert len(provider.calls) == 4
    headers = {"Authorization": "Bearer owner"}
    assert (
        client.get(f"/analyses/{result['id']}", params=scope.model_dump(), headers=headers).json() == result
    )
    events = client.get(f"/analyses/{result['id']}/events", params=scope.model_dump(), headers=headers)
    assert events.status_code == 200 and events.json()
    assert client.get(f"/runs/{parent['id']}", params=scope.model_dump(), headers=headers).json() == parent
    assert source.get_run(caller, scope, parent["id"]) == parent


@pytest.mark.parametrize(
    "extra", [{"role": "supervisor"}, {"provider_url": "https://attacker.invalid"}, {"approval": True}]
)
def test_body_cannot_supply_authority_provider_or_approval(api, extra):
    client, _, _, scope, _, parent, provider = api
    assert post(client, scope, parent, **extra).status_code == 422
    assert not provider.calls


def test_analysis_rejects_forged_headers_and_other_scope_before_dispatch(api):
    client, _, _, scope, _, parent, provider = api
    assert post(client, scope, parent, token="other").status_code == 404
    forged = client.post(
        "/analyses",
        json={"scope": scope.model_dump(), "run_id": parent["id"]},
        headers={"X-Role": "supervisor", "X-Tenant-ID": scope.tenant_id, "Idempotency-Key": "forged"},
    )
    assert forged.status_code == 401
    assert post(client, scope, parent, token="agent").status_code in {403, 404}
    assert not provider.calls


def test_analysis_read_and_cancel_are_scope_bound(api):
    client, _, _, scope, _, parent, _ = api
    analysis = post(client, scope, parent).json()
    headers = {"Authorization": "Bearer other"}
    for path in (f"/analyses/{analysis['id']}", f"/analyses/{analysis['id']}/events"):
        assert client.get(path, params=scope.model_dump(), headers=headers).status_code == 404
    assert (
        client.post(
            f"/analyses/{analysis['id']}/cancel", json=scope.model_dump(), headers=headers
        ).status_code
        == 404
    )


def test_analysis_public_identity_guard_and_truthful_health(api):
    client, source, _, _, _, _, _ = api
    health = client.get("/analysis/health").json()
    assert health["identity"] == "DEV_ONLY" and health["automatic_business_actions"] is False
    assert health["provider_live_status"] == "CHECK_VIA_PREFLIGHT_AND_ACTUAL_CALL_EVIDENCE"
    public = TestClient(create_analysis_app(source, {}, object()))
    assert public.get("/analysis/health").status_code == 403
