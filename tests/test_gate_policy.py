from datetime import UTC, datetime

import pytest

from forgeflow.contracts import DomainError, ExecutionSpec, Principal, Scope, digest
from forgeflow.gate import evaluate
from forgeflow.policy import execution_policy

S = Scope(tenant_id="a", workspace_id="w", project_id="p")
NOW = datetime(2026, 10, 5, tzinfo=UTC)


def manifest():
    return {
        "version": "v1",
        "source_ref": "synthetic",
        "metrics": {
            key: {
                "value": value,
                "status": "MEASURED",
                "observed_at": "2026-10-04T00:00:00Z",
                "valid_until": "2026-11-05T00:00:00Z",
            }
            for key, value in {
                "boot_cycles": 100,
                "boot_failures": 0,
                "bom_revision": "A3",
                "ci_smoke": True,
                "thermal_peak_c": 68,
            }.items()
        },
    }


def test_gate_independent_unknown_never_passes():
    good = {"status": "SUCCEEDED", "cleanup": "REMOVED"}
    assert evaluate(manifest(), good, now=NOW)["status"] == "PASS"
    m = manifest()
    del m["metrics"]["ci_smoke"]
    assert evaluate(m, good, now=NOW)["status"] == "UNKNOWN"
    m["metrics"]["thermal_peak_c"]["value"] = 90
    assert evaluate(m, good, now=NOW)["status"] == "BLOCKED"
    assert evaluate(manifest(), {"status": "TIMED_OUT", "cleanup": "REMOVED"}, now=NOW)["status"] == "UNKNOWN"
    assert evaluate(manifest(), {"status": "SUCCEEDED", "cleanup": "FAILED"}, now=NOW)["status"] == "UNKNOWN"


def test_gate_expiry_conflict_and_types():
    m = manifest()
    m["metrics"]["boot_cycles"]["valid_until"] = "2026-10-04T23:00:00Z"
    assert evaluate(m, {"status": "SUCCEEDED", "cleanup": "REMOVED"}, now=NOW)["status"] == "UNKNOWN"
    m = manifest()
    m["metrics"]["boot_cycles"]["value"] = True
    assert evaluate(m, {"status": "SUCCEEDED", "cleanup": "REMOVED"}, now=NOW)["status"] == "BLOCKED"


def test_policy_hash_and_role_are_mandatory():
    spec = ExecutionSpec(
        scope=S,
        run_id="r",
        step_id="s",
        attempt=1,
        template_id="evt-check",
        input_manifest={},
        input_hash=digest({}),
        code_hash="abc",
        image="python@sha256:" + "a" * 64,
    )
    p = Principal(subject_id="h", kind="agent", roles=frozenset({"hardware"}), scopes=(S,))
    execution_policy(p, spec, "abc", "PASS")
    with pytest.raises(DomainError):
        execution_policy(p, spec, "def", "PASS")
    with pytest.raises(DomainError):
        execution_policy(p, spec, "abc", "UNAVAILABLE")
    with pytest.raises(DomainError):
        execution_policy(p.model_copy(update={"roles": frozenset({"pm"})}), spec, "abc", "PASS")
