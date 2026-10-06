"""Deterministic synthetic EVT rules; never a real hardware certification."""

from datetime import UTC, datetime

from forgeflow.contracts import digest

RULES = {
    "id": "aurora-synthetic-evt-v1",
    "source": "ForgeFlow synthetic engineering example, not hardware certification",
    "stage_from": "PROTOTYPE",
    "stage_to": "EVT",
    "required": ["boot_cycles", "boot_failures", "bom_revision", "ci_smoke", "thermal_peak_c"],
    "thresholds": {
        "boot_cycles_min": 100,
        "boot_failures_max": 0,
        "bom_revision": "A3",
        "thermal_peak_max_c": 70,
    },
}
RULE_HASH = digest(RULES)


def evaluate(manifest: dict, execution: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    rows = []
    evidence = []
    trustworthy = execution.get("status") == "SUCCEEDED" and execution.get("cleanup") == "REMOVED"
    metrics = manifest.get("metrics", {})
    for name in RULES["required"]:
        item = metrics.get(name)
        state, reason = "UNKNOWN", "Required evidence missing"
        if not trustworthy:
            reason = "Check did not complete with confirmed cleanup"
        elif item is not None:
            try:
                valid = datetime.fromisoformat(item["valid_until"].replace("Z", "+00:00"))
                observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
                if observed > now or valid <= now or valid <= observed:
                    reason = "Evidence is future-dated, expired or has an invalid validity interval"
                elif item.get("conflict") or item.get("status") != "MEASURED":
                    reason = "Evidence is conflicted or unmeasured"
                else:
                    value = item["value"]
                    numeric = type(value) in (int, float)
                    passed = {
                        "boot_cycles": numeric and value >= 100,
                        "boot_failures": numeric and value == 0,
                        "bom_revision": type(value) is str and value == "A3",
                        "ci_smoke": value is True,
                        "thermal_peak_c": numeric and 0 <= value <= 70,
                    }[name]
                    state, reason = (
                        ("SATISFIED", "Synthetic rule satisfied")
                        if passed
                        else ("VIOLATED", "Synthetic rule violated or value has the wrong type")
                    )
            except (KeyError, TypeError, ValueError):
                reason = "Malformed evidence"
            evidence.append(
                {
                    "id": name,
                    "source_ref": manifest.get("source_ref"),
                    "source_version": manifest.get("version"),
                    "content_hash": digest(item),
                    "observed_at": item.get("observed_at"),
                    "valid_until": item.get("valid_until"),
                    "status": state,
                    "value": item.get("value"),
                }
            )
        rows.append({"rule": name, "status": state, "reason": reason})
    status = (
        "BLOCKED"
        if any(row["status"] == "VIOLATED" for row in rows)
        else ("UNKNOWN" if any(row["status"] == "UNKNOWN" for row in rows) else "PASS")
    )
    return {
        "status": status,
        "rule_id": RULES["id"],
        "rule_hash": RULE_HASH,
        "rule_source": RULES["source"],
        "checks": rows,
        "evidence": evidence,
        "stage_transition": "NOT_EXECUTED",
        "checker_status": execution.get("status"),
    }
