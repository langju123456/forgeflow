import hashlib
import json
import subprocess
import sys

from forgeflow.contracts import ExecutionResult, SandboxArtifact
from forgeflow.workflow import verify_checker_artifact


def artifact_result(manifest, *, wrong_hash=False):
    value = json.dumps(
        {
            "schema_version": "evt-check-v1",
            "manifest": manifest,
            "runtime": {"uid": 10001, "gid": 10001, "seccomp": "2"},
        }
    )
    return ExecutionResult(
        handle="h",
        status="SUCCEEDED",
        cleanup="REMOVED",
        artifacts=(
            SandboxArtifact(
                path="result.json",
                content=value,
                size=len(value.encode()),
                sha256="bad" if wrong_hash else hashlib.sha256(value.encode()).hexdigest(),
            ),
        ),
    )


def test_independent_artifact_verifier_rejects_same_status_wrong_provenance():
    assert verify_checker_artifact(artifact_result({"original": 1}), {"original": 1})
    assert not verify_checker_artifact(artifact_result({"original": 1}, wrong_hash=True), {"original": 1})
    assert not verify_checker_artifact(artifact_result({"changed": 1}), {"original": 1})


def test_optimized_python_keeps_artifact_checks():
    code = (
        "from forgeflow.contracts import ExecutionResult; from forgeflow.workflow import verify_checker_artifact; "
        "r=ExecutionResult(handle='h',status='SUCCEEDED',cleanup='REMOVED'); "
        "raise SystemExit(9 if verify_checker_artifact(r,{}) else 0)"
    )
    result = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True, timeout=10, check=False)
    assert result.returncode == 0
