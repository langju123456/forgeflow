"""Read the existing local API with a task-generated identity; never send model calls."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from forgeflow.local import SCOPE, load_identities

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--analysis-id", required=True)
parser.add_argument("--run-id", required=True)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
identities = load_identities()
token = next(key for key, principal in identities.items() if "admin" in principal.roles)
headers = {"Authorization": "Bearer " + token}
with httpx.Client(base_url="http://127.0.0.1:8769", trust_env=False, timeout=10) as client:
    health = client.get("/analysis/health")
    health.raise_for_status()
    analysis = client.get("/analyses/" + args.analysis_id, params=SCOPE.model_dump(), headers=headers)
    analysis.raise_for_status()
    events = client.get(
        "/analyses/" + args.analysis_id + "/events", params=SCOPE.model_dump(), headers=headers
    )
    events.raise_for_status()
    parent = client.get("/runs/" + args.run_id, params=SCOPE.model_dump(), headers=headers)
    parent.raise_for_status()
    anonymous = client.get("/analyses/" + args.analysis_id, params=SCOPE.model_dump())
    forged = client.get(
        "/analyses/" + args.analysis_id,
        params=SCOPE.model_dump(),
        headers={"X-Role": "supervisor", "X-Tenant-ID": SCOPE.tenant_id},
    )
    if anonymous.status_code != 401 or forged.status_code != 401:
        raise SystemExit("AUTHENTICATION_SMOKE_FAILED")
    data, run = analysis.json(), parent.json()
    if data["run_id"] != run["id"] or run["id"] != args.run_id:
        raise SystemExit("RUN_BINDING_SMOKE_FAILED")
    if run["project"]["current_stage"] != "PROTOTYPE":
        raise SystemExit("STAGE_SMOKE_FAILED")
    if run["receipts"] or any(item["decision"] != "PENDING" for item in run["approvals"]):
        raise SystemExit("BUSINESS_AUTHORITY_SMOKE_FAILED")
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "transport": "ACTUAL_LOOPBACK_HTTP",
        "base_url": "http://127.0.0.1:8769",
        "health": health.json(),
        "analysis_id": data["id"],
        "analysis_state": data["state"],
        "analysis_call_count": len(data["calls"]),
        "event_count": len(events.json()),
        "technical_gate": run["assessment_json"]["status"],
        "stage": run["project"]["current_stage"],
        "pending_approvals": len(run["approvals"]),
        "action_receipt_count": len(run["receipts"]),
        "anonymous_status": anonymous.status_code,
        "forged_headers_status": forged.status_code,
        "generation_or_business_mutations": False,
    }
args.output.parent.mkdir(parents=True, exist_ok=True)
if args.output.exists():
    raise SystemExit("Use a new output path; preserve old evidence")
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(report, ensure_ascii=False))
