"""Persist and replay EVT contracts over recorded actual chains; no inference/actions."""

import json
from pathlib import Path

import httpx
from prototype_regression import ANALYSES
from sqlalchemy import inspect, select

from forgeflow.analysis_store import AnalysisCall
from forgeflow.contracts import digest
from forgeflow.evt_contract import DEFAULT_POLICY, SOURCE_POLICY, UNRESOLVED_POLICY
from forgeflow.evt_review import EVTReviews
from forgeflow.local import SCOPE, load_identities, load_store
from forgeflow.store import filters, values

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliveries/2026-10-06/s3-evt/evidence"
URL = "http://127.0.0.1:8773"


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source, identities = load_store(), load_identities()
    token, owner = next((key, p) for key, p in identities.items() if "admin" in p.roles)
    params, headers = values(SCOPE), {"Authorization": "Bearer " + token}
    domains = json.loads((ROOT / "deliveries/2026-10-06/s3-domain/evidence/live-summary.json").read_text())["cases"]
    sres = {row["scenario"]: row for row in json.loads(
        (ROOT / "deliveries/2026-10-06/s3-sre/evidence/live-summary.json").read_text())["cases"]}
    result = {"status": "IN_PROGRESS", "cases": [], "scope": params, "new_model_calls": None,
              "new_sandbox_executions": 0, "actual_human_decisions": 0, "external_business_writes": False}
    fresh = EVTReviews(source)

    def save():
        (OUTPUT / "live-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    with source._tx() as session:
        before_calls = [row.id for row in session.scalars(select(AnalysisCall).where(
            *filters(AnalysisCall, SCOPE), AnalysisCall.analysis_id.in_(list(ANALYSES.values())))).all()]
    assert len(before_calls) == 20
    try:
        with httpx.Client(base_url=URL, trust_env=False, headers=headers, timeout=30) as client:
            def get(path):
                response = client.get(path, params=params)
                response.raise_for_status()
                return response.json()

            def post(path, body, key):
                response = client.post(path, json=body, headers={"Idempotency-Key": key})
                response.raise_for_status()
                return response.json()

            result["health"] = get("/health")
            assert result["health"]["revision"] == "evt-bound-review-v1"
            for case in domains:
                sid, run_id = case["scenario"], case["run_id"]
                before, events = get(f"/runs/{run_id}"), get(f"/runs/{run_id}/events")
                assert digest(before) == case["source_run_hash"]
                analysis = get(f"/grounded-analyses/{ANALYSES[sid]}")
                assert len(analysis["calls"]) == 4 and all(call["delivery_status"] == "DELIVERED" for call in analysis["calls"])
                combinations = [(case["domain_reviews"][0], policy) for policy in [DEFAULT_POLICY, SOURCE_POLICY, UNRESOLVED_POLICY]]
                if sid == "M01":
                    combinations += [(domain, policy) for domain in case["domain_reviews"][1:]
                                     for policy in [DEFAULT_POLICY, SOURCE_POLICY]]
                reviews = []
                for domain, policy in combinations:
                    fixture = domain["fixture"]
                    body = {"scope": params, "domain_review_id": domain["review_id"],
                            "sre_review_id": sres[sid]["review_id"], "policy_id": policy}
                    key = f"s3-evt:{sid}:{fixture}:{policy}:01"
                    review = post(f"/runs/{run_id}/evt-review", body, key)
                    assert review == post(f"/runs/{run_id}/evt-review", body, key)
                    assert review == get(f"/evt-reviews/{review['id']}") == fresh.get(owner, SCOPE, review["id"])
                    report = review["report_json"]
                    expected = ("BLOCKED" if fixture != "aligned" or case["technical_gate"] == "BLOCKED" else
                                "PASS" if policy == SOURCE_POLICY and case["technical_gate"] == "PASS" else "UNKNOWN")
                    assert report["status"] == expected
                    assert report["stage_transition_allowed"] is False and report["full_evt_eligibility"] is False
                    for check in report["checks"]:
                        if check["id"] in {"android_apk", "android_device"}:
                            assert check["observed_status"] == "UNKNOWN" and check["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
                            assert check["status"] == ("NOT_APPLICABLE" if policy == SOURCE_POLICY else "UNKNOWN")
                    acceptance_body = {"scope": params, "review_hash": review["report_hash"]}
                    approval_key = "accept:" + key
                    if expected == "PASS":
                        approval = post(f"/evt-reviews/{review['id']}/approval", acceptance_body, approval_key)
                        assert approval == post(f"/evt-reviews/{review['id']}/approval", acceptance_body, approval_key)
                        assert approval == get(f"/evt-approvals/{approval['id']}")
                        assert approval["decision"] == "PENDING" and approval["proposal"]["stage_transition_allowed"] is False
                        (OUTPUT / f"{sid}-source-pending-acceptance.json").write_text(json.dumps(approval, indent=2) + "\n")
                        acceptance = {"id": approval["id"], "decision": "PENDING", "business_action": "NONE"}
                    else:
                        denied = client.post(f"/evt-reviews/{review['id']}/approval", json=acceptance_body,
                                             headers={"Idempotency-Key": approval_key})
                        assert denied.status_code == 403 and denied.json()["code"] == "EVT_GATE_NOT_PASS"
                        acceptance = {"status": "DENIED", "http": 403}
                    (OUTPUT / f"{sid}-{fixture}-{policy}.json").write_text(json.dumps(review, indent=2) + "\n")
                    reviews.append({"fixture": fixture, "policy": policy, "review_id": review["id"],
                                    "review_hash": review["report_hash"], "status": expected, "key": key,
                                    "replay": "IDENTICAL_HTTP_AND_FRESH_SERVICE", "acceptance": acceptance})
                assert before == get(f"/runs/{run_id}") and events == get(f"/runs/{run_id}/events")
                assert before["project"]["current_stage"] == "PROTOTYPE" and not before["receipts"]
                result["cases"].append({"scenario": sid, "run_id": run_id, "historical_model_calls": 4,
                                        "hardware_gate": case["technical_gate"], "reviews": reviews,
                                        "parent_and_events": "UNCHANGED", "stage": "PROTOTYPE", "business_receipts": 0})
                save()
            first = result["cases"][0]
            rid = first["reviews"][0]["review_id"]
            assert client.get(f"/evt-reviews/{rid}", params={**params, "tenant_id": "foreign"}).status_code == 404
            assert client.get(f"/evt-reviews/{rid}", params=params, headers={"Authorization": "Bearer invalid"}).status_code == 401
            body = {"scope": params, "domain_review_id": domains[0]["domain_reviews"][0]["review_id"],
                    "sre_review_id": sres["M01"]["review_id"]}
            path = f"/runs/{first['run_id']}/evt-review"
            assert client.post(path, json={**body, "applicability": {}}, headers={"Idempotency-Key": "no-override"}).status_code == 422
            assert client.post(path, json={**body, "policy_id": SOURCE_POLICY},
                               headers={"Idempotency-Key": first["reviews"][0]["key"]}).status_code == 409
            assert client.post(path, json={**body, "domain_review_id": domains[-1]["domain_reviews"][0]["review_id"]},
                               headers={"Idempotency-Key": "wrong-run-reference"}).status_code == 404
            parent = get(f"/runs/{first['run_id']}")
            old_approval = parent["approvals"][0]
            legacy = client.post(f"/approvals/{old_approval['id']}/decision", json={"scope": params,
                "proposal_hash": old_approval["proposal_hash"], "expected_run_revision": parent["revision"], "decision": "APPROVED"})
            assert legacy.status_code == 409 and legacy.json()["code"] == "EVT_BOUND_APPROVAL_REQUIRED"
            assert get(f"/runs/{first['run_id']}") == parent
            result["negative_http"] = {"foreign_scope": 404, "invalid_identity": 401, "applicability_override": 422,
                                       "policy_changed_same_key": 409, "wrong_run_role_ref": 404,
                                       "legacy_hardware_only_grant": {"http": 409, "code": legacy.json()["code"]}}
        with source._tx() as session:
            after_calls = [row.id for row in session.scalars(select(AnalysisCall).where(
                *filters(AnalysisCall, SCOPE), AnalysisCall.analysis_id.in_(list(ANALYSES.values())))).all()]
        assert set(before_calls) == set(after_calls)
        result["new_model_calls"] = 0
        result["foreign_keys"] = {table: inspect(source.engine).get_foreign_keys(table)
                                  for table in ["evt_reviews", "evt_review_approvals"]}
        result["status"] = "PASS_19_BOUND_EVT_REVIEWS_2_PENDING_SOURCE_ACCEPTANCES"
        save()
        print(json.dumps({"status": result["status"], "cases": len(result["cases"]), "new_model_calls": 0,
                          "actual_human_decisions": 0, "api_pid": result["health"]["process_id"]}))
    except Exception as error:
        result["status"], result["error_type"] = "FAILED", type(error).__name__
        result["error_code"] = getattr(error, "code", None)
        save()
        raise


if __name__ == "__main__":
    main()
