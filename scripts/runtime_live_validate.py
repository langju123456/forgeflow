"""Local conformance evidence. The network/flood probes are trusted test templates."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from forgeflow.contracts import ExecutionSpec, Principal, Scope, digest
from forgeflow.mcp_bridge import MCPBridge
from forgeflow.sandbox import DockerSandbox
from forgeflow.templates import template_hash


async def main():
    from forgeflow.scanner import Scanner

    config = json.loads((Path(__file__).resolve().parents[1] / ".runtime" / "runtime.json").read_text())
    image = os.environ.get("FORGEFLOW_SCANNER_IMAGE") or config["scanner_image"]
    scanner, provider = Scanner(image=image), DockerSandbox()
    scope = Scope(tenant_id="conformance", workspace_id="runtime", project_id="aurora")
    run_id = "live-" + uuid.uuid4().hex
    principal = Principal(subject_id="sre-conformance", kind="agent", roles={"hardware"}, scopes=(scope,))
    results = []

    def spec(template: str, step: str, timeout: float = 20):
        value = {"source": "SYNTHETIC_FIXTURE", "case": step}
        return ExecutionSpec(
            scope=scope,
            run_id=run_id,
            step_id=step,
            attempt=1,
            template_id=template,
            input_manifest=value,
            input_hash=digest(value),
            code_hash=template_hash(template),
            image=provider.resolve_image(),
            timeout_seconds=timeout,
        )

    request = spec("evt-check", "real-mcp")
    identity = {
        "scope": scope.model_dump(mode="json"),
        "run_id": run_id,
        "step_id": request.step_id,
        "attempt": 1,
    }
    scan_handle = scanner.handle(scope, run_id, request.step_id, 1)
    scan = scanner.scan("evt-check", handle=scan_handle, identity=identity, lease_deadline=time.time() + 90)
    assert scan["status"] == "PASS", scan
    assert scan["cleanup"] == "REMOVED", scan
    result, transcript = await MCPBridge(provider).execute(
        principal, request, scan_receipt=scan, lease_deadline=time.time() + 60
    )
    assert result.status == "SUCCEEDED" and result.cleanup == "REMOVED", result
    assert json.loads(result.artifacts[0].content)["runtime"] == {"uid": 10001, "gid": 10001, "seccomp": "2"}
    results.append(
        {
            "case": "same-hash-real-semgrep-mcp-docker",
            "scan": scan,
            "execution": result.model_dump(mode="json"),
            "mcp": transcript,
        }
    )

    for template, expected, timeout in (
        ("timeout", "TIMED_OUT", 1.0),
        ("output-flood", "OUTPUT_LIMIT", 10),
        ("network-probe", "SUCCEEDED", 20),
    ):
        result = provider.execute(spec(template, template, timeout))
        assert result.status == expected and result.cleanup == "REMOVED", result
        if template == "network-probe":
            probe = json.loads(result.artifacts[0].content)
            assert all(probe[x] == "DENIED" for x in ("tcp", "metadata", "udp", "dns", "rootfs")), probe
            assert probe["uid"] == 10001 and probe["socket_present"] is False and probe["seccomp"] == "2", (
                probe
            )
        else:
            assert not result.artifacts
        assert len(result.stdout.encode()) <= 256 * 1024
        results.append({"case": template, "execution": result.model_dump(mode="json")})

    request = spec("timeout", "cancel", 20)
    started = time.monotonic()
    result = provider.execute(request, cancel_requested=lambda: time.monotonic() - started > 0.8)
    assert result.status == "CANCELLED" and result.cleanup == "REMOVED", result
    results.append({"case": "midcheck-cancel", "execution": result.model_dump(mode="json")})

    request = spec("timeout", "mcp-cancel", 20)
    identity = {
        "scope": scope.model_dump(mode="json"),
        "run_id": run_id,
        "step_id": request.step_id,
        "attempt": 1,
    }
    scan = scanner.scan(
        "timeout",
        handle=scanner.handle(scope, run_id, request.step_id, 1),
        identity=identity,
        lease_deadline=time.time() + 90,
    )
    assert scan["status"] == "PASS" and scan["cleanup"] == "REMOVED", scan
    started = time.monotonic()
    result, transcript = await MCPBridge(provider).execute(
        principal,
        request,
        scan_receipt=scan,
        lease_deadline=time.time() + 60,
        cancel_requested=lambda: time.monotonic() - started > 1.0,
    )
    assert result.status == "CANCELLED" and result.cleanup == "REMOVED", result
    results.append(
        {
            "case": "real-mcp-midcheck-cancel",
            "scan": scan,
            "execution": result.model_dump(mode="json"),
            "mcp": transcript,
        }
    )

    request = spec("timeout", "crash-recover", 20)
    child_code = (
        "import json,sys; from forgeflow.contracts import ExecutionSpec; "
        "from forgeflow.sandbox import DockerSandbox; "
        "spec=ExecutionSpec.model_validate(json.loads(sys.stdin.read())); "
        "print(DockerSandbox().execute(spec).model_dump_json())"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(request.model_dump_json().encode())
    process.stdin.close()
    handle, before, deadline = provider.handle(request), None, time.monotonic() + 8
    try:
        while time.monotonic() < deadline:
            try:
                candidate = provider._inspect(handle)
                if candidate["State"]["Running"]:
                    before = candidate["State"]
                    break
            except Exception:
                pass
            time.sleep(0.05)
        assert before is not None, "Child checker did not start"
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
        assert process.stdout and process.stderr
        process.stdout.close()
        process.stderr.close()
    assert provider.cleanup(handle), "Recovery must kill, remove and purge exact orphan"
    old = provider.execute(request)
    assert old.status == "CANCELLED" and old.cleanup == "NOT_STARTED"
    recovered = request.model_copy(
        update={"attempt": 2, "template_id": "evt-check", "code_hash": template_hash("evt-check")}
    )
    new = provider.execute(recovered)
    assert new.status == "SUCCEEDED" and new.cleanup == "REMOVED", new
    results.append(
        {
            "case": "real-worker-process-crash-and-exact-resource-recovery",
            "orphan_state": before,
            "old_attempt": old.model_dump(mode="json"),
            "new_attempt": new.model_dump(mode="json"),
        }
    )

    request = spec("evt-check", "revoked-before-create")
    assert provider.cleanup(provider.handle(request))
    result = provider.execute(request)
    assert result.status == "CANCELLED" and result.cleanup == "NOT_STARTED", result
    results.append(
        {"case": "recovery-prevents-late-old-attempt", "execution": result.model_dump(mode="json")}
    )

    target = (
        Path(__file__).resolve().parents[1]
        / "deliveries"
        / "2026-10-05"
        / "implementation-evidence"
        / "runtime-security-canonical.json"
    )
    target.write_text(
        json.dumps(
            {
                "source": "REAL_LOCAL_RUNTIME_CONFORMANCE",
                "not_claimed": ["LLM", "production", "hardware certification", "external issue"],
                "run_id": run_id,
                "results": results,
            },
            indent=2,
        )
    )
    print(json.dumps({"evidence": str(target), "cases": [r["case"] for r in results], "status": "PASS"}))


if __name__ == "__main__":
    asyncio.run(main())
