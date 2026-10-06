"""Replay five recorded real chains through the unified API; no new inference."""

import json
from pathlib import Path

import httpx
from sqlalchemy import inspect, select

from forgeflow.analysis_store import AnalysisCall, AnalysisRun
from forgeflow.contracts import digest
from forgeflow.domain_store import DomainOperations
from forgeflow.local import SCOPE, load_identities, load_store
from forgeflow.store import filters, values

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliveries/2026-10-06/s3-domain/evidence"
URL = "http://127.0.0.1:8773"
ANALYSES = {
    "M01": "f8133625-68c9-41ef-8e0d-ca4054682b31",
    "M01-blocked": "e1cde7ad-64ef-4dc9-80e0-3779f97b3154",
    "M02": "27047b96-9f37-481e-b288-182275231ca4",
    "M03": "f3c3f10c-867d-428d-9ec9-73f6a73e7b6c",
    "M04": "faeb6660-c8bd-4b89-87cd-833f38e6690f",
}


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    identities, source = load_identities(), load_store()
    token, owner = next((token, principal) for token, principal in identities.items() if "admin" in principal.roles)
    params = values(SCOPE)
    headers = {"Authorization": "Bearer " + token}
    cases = json.loads((ROOT / "deliveries/2026-10-06/s3-sre/evidence/live-summary.json").read_text())["cases"]
    result = {"status": "IN_PROGRESS", "cases": [], "scope": params,
              "execution": "RECORDED_REAL_CHAIN_REPLAY_PLUS_NEW_STATIC_ROLE_REVIEWS",
              "new_model_calls": None, "new_sandbox_executions": 0, "external_business_writes": False}

    def save():
        (OUTPUT / "live-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    try:
        with source._tx() as session:
            before_calls = [row.id for row in session.scalars(select(AnalysisCall).where(
                *filters(AnalysisCall, SCOPE), AnalysisCall.analysis_id.in_(list(ANALYSES.values())))).all()]
            keys = {sid: source._get(session, AnalysisRun, SCOPE, aid).idempotency_key for sid, aid in ANALYSES.items()}
        assert len(before_calls) == 20
        fresh = DomainOperations(source)
        with httpx.Client(base_url=URL, trust_env=False, timeout=30, headers=headers) as client:
            def get(path, query=params):
                response = client.get(path, params=query)
                response.raise_for_status()
                return response.json()

            def post(path, body, key):
                response = client.post(path, json=body, headers={"Idempotency-Key": key})
                response.raise_for_status()
                return response.json()

            result["health"] = get("/health", {})
            paths = get("/openapi.json", {})["paths"]
            assert all(path in paths for path in ["/runs", "/s2c/parents", "/runs/{run_id}/grounded-analysis",
                                                 "/runs/{run_id}/sre-review", "/runs/{run_id}/domain-review"])
            for case in cases:
                sid, run_id = case["scenario"], case["run_id"]
                before, events = get(f"/runs/{run_id}"), get(f"/runs/{run_id}/events")
                analysis = get(f"/grounded-analyses/{ANALYSES[sid]}")
                records = get(f"/grounded-analyses/{ANALYSES[sid]}/records")
                assert len(analysis["calls"]) == len(records) == 4
                assert all(call["delivery_status"] == "DELIVERED" and call["state"] == "VALIDATED"
                           for call in analysis["calls"])
                replay = post(f"/runs/{run_id}/grounded-analysis", {"scope": params, "scenario_id": sid}, keys[sid])
                assert replay == analysis
                assert post(f"/runs/{run_id}/sre-review", params, case["key"])["id"] == case["review_id"]
                domain_results = []
                for fixture in ["aligned", "bom-mismatch", "missing-exported"]:
                    key = f"s3-domain:{sid}:{fixture}:01"
                    review = post(f"/runs/{run_id}/domain-review", {"scope": params, "fixture_id": fixture}, key)
                    assert review == post(f"/runs/{run_id}/domain-review", {"scope": params, "fixture_id": fixture}, key)
                    assert review == get(f"/domain-reviews/{review['id']}") == fresh.get(owner, SCOPE, review["id"])
                    report = review["report_json"]
                    assert report["android"]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
                    assert report["stage_transition"] == "NOT_EXECUTED"
                    assert report["supervisor_handoff"] == ("NEEDS_ANDROID_BUILD_EVIDENCE" if fixture == "aligned"
                                                             else "DOMAIN_CONFLICT")
                    assert report["android"]["readiness"] == ("UNKNOWN" if fixture == "aligned" else "BLOCKED")
                    (OUTPUT / f"{sid}-{fixture}.json").write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n")
                    domain_results.append({"fixture": fixture, "key": key, "review_id": review["id"],
                                           "architect": report["architect"]["static_status"],
                                           "android_static": report["android"]["static_status"],
                                           "android_readiness": report["android"]["readiness"],
                                           "handoff": report["supervisor_handoff"],
                                           "replay": "IDENTICAL_HTTP_AND_FRESH_SERVICE"})
                assert before == get(f"/runs/{run_id}") and events == get(f"/runs/{run_id}/events")
                assert before["project"]["current_stage"] == "PROTOTYPE" and not before["receipts"]
                assert get(f"/runs/{run_id}/report")["truth"]["android_build"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
                result["cases"].append({"scenario": sid, "run_id": run_id, "analysis_id": ANALYSES[sid],
                                        "historical_actual_model_calls": 4, "linked_model_roles": ["supervisor", "pm"],
                                        "technical_gate": before["assessment_json"]["status"],
                                        "source_run_hash": digest(before), "parent_and_events": "UNCHANGED",
                                        "model_replay": "IDENTICAL", "sre_replay": "IDENTICAL",
                                        "business_receipts": 0, "domain_reviews": domain_results})
                save()
            first = result["cases"][0]
            review_id = first["domain_reviews"][0]["review_id"]
            foreign = {**params, "tenant_id": "foreign"}
            assert client.get(f"/domain-reviews/{review_id}", params=foreign).status_code == 404
            assert client.get(f"/domain-reviews/{review_id}", params=params,
                              headers={"Authorization": "Bearer invalid"}).status_code == 401
            path = f"/runs/{first['run_id']}/domain-review"
            assert client.post(path, json={"scope": params, "fixture_id": "bom-mismatch"},
                headers={"Idempotency-Key": first["domain_reviews"][0]["key"]}).status_code == 409
            result["negative_http"] = {"foreign_tenant": 404, "invalid_identity": 401, "changed_fixture_same_key": 409}
        with source._tx() as session:
            after_calls = [row.id for row in session.scalars(select(AnalysisCall).where(
                *filters(AnalysisCall, SCOPE), AnalysisCall.analysis_id.in_(list(ANALYSES.values())))).all()]
        assert set(before_calls) == set(after_calls)
        result["new_model_calls"] = 0
        result["domain_foreign_keys"] = inspect(source.engine).get_foreign_keys("domain_reviews")
        result["status"] = "PASS_FIVE_REAL_CHAIN_REPLAYS_FIFTEEN_STATIC_REVIEWS"
        save()
        print(json.dumps({"status": result["status"], "cases": len(result["cases"]),
                          "domain_reviews": 15, "new_model_calls": 0, "api_pid": result["health"]["process_id"]}))
    except Exception as error:
        result["status"] = "FAILED"
        result["error_type"] = type(error).__name__
        result["error_code"] = getattr(error, "code", None)
        save()
        raise


if __name__ == "__main__":
    main()
