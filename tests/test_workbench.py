"""The review workbench: read-only list and explain routes, and the seeded local server they run on."""

from fastapi.testclient import TestClient

from forgeflow.workbench import IDENTITIES, create_workbench_app, seed


def client_for(tmp_path):
    source, run_id = seed(tmp_path / "workbench.db")
    client = TestClient(create_workbench_app(source, run_id))
    scope = client.get("/workbench-config").json()["scope"]
    return client, run_id, scope


def headers(token, key=None):
    return {"Authorization": "Bearer " + token, **({"Idempotency-Key": key} if key else {})}


def proposed_issue(client, run_id, scope):
    review = client.post(f"/fabric/runs/{run_id}/review", json={"scope": scope},
                         headers=headers("dev-proposer", "review-1")).json()
    approval = client.post(f"/fabric/reviews/{review['id']}/proposals",
                           json={"scope": scope, "review_hash": review["report_hash"], "kind": "ISSUE"},
                           headers=headers("dev-proposer", "propose-1")).json()
    return review, approval


def test_seed_serves_three_separate_identities(tmp_path):
    client, run_id, scope = client_for(tmp_path)
    config = client.get("/workbench-config").json()
    assert config["mode"] == "DEV_ONLY_LOCAL_SYNTHETIC" and config["run_id"] == run_id
    assert {item["token"] for item in config["identities"]} == set(IDENTITIES)
    assert {item["kind"] for item in config["identities"]} == {"user", "service"}


def test_lists_show_reviews_and_approvals_with_state(tmp_path):
    client, run_id, scope = client_for(tmp_path)
    query = {"params": scope}
    assert client.get("/fabric/approvals", headers=headers("dev-reviewer"), **query).json() == []
    review, approval = proposed_issue(client, run_id, scope)

    reviews = client.get("/fabric/reviews", headers=headers("dev-reviewer"), **query).json()
    assert [item["id"] for item in reviews] == [review["id"]]
    assert reviews[0]["flow_gate"] == "PASS" and reviews[0]["report_hash"] == review["report_hash"]

    listed = client.get("/fabric/approvals", headers=headers("dev-reviewer"), **query).json()
    assert [(item["id"], item["decision"], item["receipt_id"]) for item in listed] == [(approval["id"], "PENDING", None)]

    client.post(f"/fabric/approvals/{approval['id']}/decision", headers=headers("dev-reviewer"), json={
        "scope": scope, "proposal_hash": approval["proposal_hash"], "expected_revision": 0, "decision": "APPROVED"})
    receipt = client.post(f"/fabric/approvals/{approval['id']}/execute", headers=headers("dev-executor"),
                          json=scope).json()["receipt"]
    listed = client.get("/fabric/approvals", headers=headers("dev-proposer"), **query).json()
    assert listed[0]["decision"] == "APPROVED" and listed[0]["receipt_id"] == receipt["id"]


def test_explain_tells_the_reviewer_what_they_sign_and_who_may_not(tmp_path):
    client, run_id, scope = client_for(tmp_path)
    review, approval = proposed_issue(client, run_id, scope)
    explain = client.get(f"/fabric/approvals/{approval['id']}/explain", headers=headers("dev-reviewer"),
                         params=scope).json()
    assert explain["parameters"]["proposal_hash"] == approval["proposal_hash"]
    assert explain["parameters"]["review_hash"] == review["report_hash"]
    assert explain["independence"]["may_not_decide"] == ["workbench-proposer"]
    assert explain["can_be_decided"]["now"] is True
    assert explain["if_executed"]["external_writes"] is False
    assert explain["executed"] is None


def test_reads_need_an_identity_and_the_desk_cannot_bypass_roles(tmp_path):
    client, run_id, scope = client_for(tmp_path)
    _, approval = proposed_issue(client, run_id, scope)
    assert client.get("/fabric/approvals", params=scope).status_code == 401
    assert client.get(f"/fabric/approvals/{approval['id']}/explain", params=scope).status_code == 401
    other_scope = {**scope, "project_id": "someone-else"}
    assert client.get("/fabric/approvals", headers=headers("dev-reviewer"), params=other_scope).status_code == 404
    # The proposer cannot approve its own proposal, and a person cannot execute.
    decision = {"scope": scope, "proposal_hash": approval["proposal_hash"], "expected_revision": 0,
                "decision": "APPROVED"}
    assert client.post(f"/fabric/approvals/{approval['id']}/decision", headers=headers("dev-proposer"),
                       json=decision).status_code in (403, 404)
    assert client.post(f"/fabric/approvals/{approval['id']}/execute", headers=headers("dev-reviewer"),
                       json=scope).status_code == 403


def test_a_stale_revision_is_refused_so_two_reviewers_cannot_both_decide(tmp_path):
    client, run_id, scope = client_for(tmp_path)
    _, approval = proposed_issue(client, run_id, scope)
    body = {"scope": scope, "proposal_hash": approval["proposal_hash"], "expected_revision": 0}
    path = f"/fabric/approvals/{approval['id']}/decision"
    assert client.post(path, headers=headers("dev-reviewer"), json={**body, "decision": "APPROVED"}).status_code == 200
    second = client.post(path, headers=headers("dev-reviewer"), json={**body, "decision": "REJECTED"})
    assert second.status_code == 409
