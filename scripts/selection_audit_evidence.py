"""Verify archived analysis audit chains already matched to live PostgreSQL records."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from forgeflow.contracts import digest
from forgeflow.local import SCOPE


def verify(events):
    previous = "0" * 64
    for index, event in enumerate(events, 1):
        if event["sequence"] != index or event["prev_hash"] != previous:
            raise RuntimeError("AUDIT_SEQUENCE_INVALID")
        if event["payload_hash"] != digest(event["payload_json"]):
            raise RuntimeError("AUDIT_PAYLOAD_INVALID")
        if event.get("material_json") is not None:
            if event["payload_json"]["context_material_hash"] != digest(event["material_json"]):
                raise RuntimeError("AUDIT_CONTEXT_INVALID")
        metadata = {
            "scope": SCOPE.model_dump(mode="json"), "analysis_id": event["analysis_id"],
            "actor": event["actor"], "kind": event["kind"], "sequence": event["sequence"],
            "payload_hash": event["payload_hash"], "prev_hash": event["prev_hash"],
            "created_at": datetime.fromisoformat(event["created_at"]).isoformat(),
        }
        if event["event_hash"] != digest(metadata):
            raise RuntimeError("AUDIT_METADATA_INVALID")
        previous = event["event_hash"]
    return previous


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Use a new evidence output path")
    summary = json.loads((args.experiment / "live/summary.json").read_text())
    results = []
    for result in summary["results"]:
        archived = json.loads((args.experiment / "raw-chains" / (result["scenario_id"] + ".json")).read_text())
        events = archived["events"]
        results.append({"scenario": result["scenario_id"], "events": len(events),
                        "audit_head": verify(events), "status": "VERIFIED"})
    with args.output.open("x") as file:
        json.dump({"status": "ARCHIVED_ANALYSIS_AUDIT_VERIFIED", "results": results,
                   "new_model_calls": 0, "external_writes": False,
                   "trust_limit": "Hash chains do not defend against a privileged database writer."}, file, indent=2)
        file.write("\n")
    print(json.dumps({"status": "ARCHIVED_ANALYSIS_AUDIT_VERIFIED", "scenarios": len(results)}))


if __name__ == "__main__":
    main()
