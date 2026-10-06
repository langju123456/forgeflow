import pytest
from fastapi.testclient import TestClient

from forgeflow.api import create_app
from forgeflow.contracts import Principal, Scope
from forgeflow.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "api.db"))
    store.create_schema()
    scope = Scope(tenant_id="t", workspace_id="w", project_id="aurora")
    other = scope.model_copy(update={"project_id": "secret"})
    owner = Principal(
        subject_id="owner", kind="user", roles=frozenset({"submitter", "reviewer"}), scopes=(scope,)
    )
    intruder = owner.model_copy(update={"subject_id": "outsider", "scopes": (other,)})
    agent = owner.model_copy(update={"subject_id": "agent", "kind": "agent"})
    client = TestClient(
        create_app(store, {"owner": owner, "outsider": intruder, "agent": agent}, test_harness=True)
    )
    return store, scope, client


def test_persisted_submit_replay_and_scope_authority(setup):
    store, scope, client = setup
    body = {"scope": scope.model_dump(), "fixture_id": "aurora-pass"}
    headers = {"Authorization": "Bearer owner", "Idempotency-Key": "same"}
    first = client.post("/runs", json=body, headers=headers)
    second = client.post("/runs", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert (
        client.post("/runs", json={**body, "fixture_id": "aurora-blocked"}, headers=headers).status_code
        == 409
    )
    assert (
        client.post(
            "/runs",
            json=body,
            headers={**headers, "Authorization": "Bearer outsider", "X-Tenant-ID": "t", "X-Role": "admin"},
        ).status_code
        == 404
    )
    run_id = first.json()["id"]
    assert (
        client.get(
            f"/runs/{run_id}", params=scope.model_dump(), headers={"Authorization": "Bearer outsider"}
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/runs/{run_id}/events", params=scope.model_dump(), headers={"Authorization": "Bearer owner"}
        ).json()[0]["kind"]
        == "RUN_SUBMITTED"
    )


def test_no_arbitrary_execution_or_claimed_identity(setup):
    _, scope, client = setup
    headers = {"Authorization": "Bearer owner", "Idempotency-Key": "k"}
    body = {"scope": scope.model_dump(), "fixture_id": "aurora-pass", "shell": "curl attacker"}
    assert client.post("/runs", json=body, headers=headers).status_code == 422
    assert (
        client.post(
            "/runs",
            json={"scope": scope.model_dump(), "fixture_id": "aurora-pass"},
            headers={"X-Role": "reviewer", "Idempotency-Key": "k"},
        ).status_code
        == 401
    )


def test_dev_identity_refuses_public_and_production(setup, monkeypatch):
    store, _, _ = setup
    client = TestClient(create_app(store, {}))
    assert client.get("/health").status_code == 403
    monkeypatch.setenv("FORGEFLOW_MODE", "production")
    with pytest.raises(RuntimeError, match="DEV_ONLY"):
        create_app(store, {})
