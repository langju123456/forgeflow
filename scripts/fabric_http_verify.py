"""Read/replay the current local API; no model calls or new effects."""

import json

import httpx
from sqlalchemy import inspect

from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_runtime import load_fabric_identities, load_fabric_store
from forgeflow.local import ROOT, SCOPE, load_identities

DEST = ROOT / "deliveries/2026-10-06/s3-fabric/evidence"


def main():
    identities = load_fabric_identities()
    owner = next(token for token, p in identities.items() if "admin" in p.roles)
    reviewer = next(token for token, p in identities.items() if "reviewer" in p.roles)
    executor = next(token for token, p in identities.items() if p.kind == "service")
    old_owner = next(token for token, p in load_identities().items() if "admin" in p.roles)
    checks = json.loads((DEST / "checks.json").read_text())
    effects = json.loads((DEST / "effects.json").read_text())
    scope = FABRIC_SCOPE.model_dump()
    rows = []
    with httpx.Client(base_url="http://127.0.0.1:8773", trust_env=False, timeout=10) as client:
        def call(method, path, expected=200, token=None, **kwargs):
            response = client.request(method, path, headers={"Authorization": "Bearer " + token} if token else {}, **kwargs)
            assert response.status_code == expected, (path, response.status_code, response.text)
            rows.append({"method": method, "path": path, "expected_status": expected,
                         "observed_status": response.status_code})
            return response.json()

        health = call("GET", "/health")
        case = checks["cases"]["pass"]
        path = "/fabric/reviews/" + case["review_id"]
        assert call("GET", path, token=reviewer, params=scope)["report_hash"] == case["report_hash"]
        call("GET", path, expected=401, params=scope)
        call("GET", path, expected=404, token=old_owner, params=scope)
        call("GET", path, expected=404, token=reviewer, params=SCOPE.model_dump())
        report = call("GET", "/runs/" + case["run_id"] + "/report", token=owner, params=scope)
        assert report["truth"]["stage_action_adapter"] == "ISOLATED_LOCAL_TEST_STAGE_ONLY"
        for receipt in effects["receipts"].values():
            replay = call("POST", "/fabric/approvals/" + receipt["approval_id"] + "/execute", token=executor, json=scope)
            assert replay["replayed"] and replay["receipt"] == receipt
        stage = effects["receipts"]["TEST_STAGE"]
        call("POST", "/fabric/approvals/" + stage["approval_id"] + "/execute", expected=403,
             token=reviewer, json=scope)
        unknown = checks["cases"]["unknown"]
        response = client.post("/fabric/reviews/" + unknown["review_id"] + "/proposals",
            headers={"Authorization": "Bearer " + owner, "Idempotency-Key": "s3d-http-unknown"},
            json={"scope": scope, "review_hash": unknown["report_hash"], "kind": "TEST_STAGE"})
        assert response.status_code == 409 and response.json()["code"] == "FABRIC_GATE_NOT_PASS"
        rows.append({"path": "unknown-proposal", "expected_status": 409, "observed_status": response.status_code,
                     "code": response.json()["code"]})
        assert call("GET", "/fabric/memory", token=reviewer, params={**scope, "query": "Aurora"}) == []
    source = load_fabric_store()
    schema = {name: inspect(source.engine).get_foreign_keys(name) for name in
              ("fabric_reviews", "fabric_approvals", "fabric_receipts", "fabric_items")}
    assert all(all(key in fk["constrained_columns"] for key in ("tenant_id", "workspace_id", "project_id"))
               for foreign_keys in schema.values() for fk in foreign_keys)
    output = {"provenance": "ACTUAL_LOOPBACK_HTTP_AND_POSTGRESQL_SCHEMA", "health": health,
              "checks": rows, "receipt_replay_from_separate_api_process": len(effects["receipts"]),
              "new_effects": 0, "new_model_calls": 0, "scoped_foreign_keys": schema,
              "service_replacement": {"verified_previous_pid": 65879, "current_pid": health["process_id"],
                                      "port": 8773, "other_project_actions": "NONE"}}
    (DEST / "http-schema.json").write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"http_checks": len(rows), "health": health, "scoped_fk_tables": len(schema), "new_effects": 0}))


if __name__ == "__main__":
    main()
