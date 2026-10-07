"""Real local Postgres + Semgrep + MCP + Docker chain. Explicit opt-in, no external writes."""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from forgeflow.api import create_app
from forgeflow.contracts import DomainError, ExecutionSpec, Principal, RunRequest, Scope, digest
from forgeflow.local import load_workflow
from forgeflow.store import Store
from forgeflow.templates import template_hash

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("FORGEFLOW_LIVE") != "1", reason="Opt-in requires task-local Docker and PostgreSQL"
    ),
    pytest.mark.real_clock,  # real Docker, PostgreSQL and subprocesses compare timestamps with their own clock
]


def setup_chain():
    store = Store(os.environ["FORGEFLOW_TEST_DATABASE_URL"])
    store.create_schema()
    scope = Scope(tenant_id="test-lab", workspace_id="engineering", project_id="aurora-" + uuid4().hex[:12])
    human = Principal(
        subject_id="scripted-reviewer",
        kind="user",
        roles=frozenset({"submitter", "reviewer", "admin", "hardware"}),
        scopes=(scope,),
    )
    store.ensure_project(scope)
    store.put_document(
        human,
        scope,
        "aurora-evt-sop",
        "v1",
        "Aurora EVT synthetic readiness SOP",
        roles={"hardware", "admin", "reviewer"},
        classification=0,
        valid_until="2026-11-05T00:00:00Z",
    )
    client = TestClient(create_app(store, {"test-reviewer": human}, test_harness=True))
    return store, scope, human, client, load_workflow(store)


@pytest.mark.parametrize(
    "fixture,gate,execution",
    [
        ("pass", "PASS", "SUCCEEDED"),
        ("blocked", "BLOCKED", "SUCCEEDED"),
        ("unknown", "UNKNOWN", "SUCCEEDED"),
        ("timeout", "UNKNOWN", "TIMED_OUT"),
    ],
)
def test_real_chain_with_scripted_reviewer(fixture, gate, execution):
    store, scope, human, client, workflow = setup_chain()
    headers = {"Authorization": "Bearer test-reviewer", "Idempotency-Key": "one-request"}
    body = RunRequest(scope=scope, fixture_id="aurora-" + fixture).model_dump(mode="json")
    submitted = client.post("/runs", json=body, headers=headers)
    assert submitted.status_code == 202
    run_id = submitted.json()["id"]
    assert client.post("/runs", json=body, headers=headers).json()["id"] == run_id
    report = asyncio.run(workflow.check(scope, run_id))
    assert report["assessment_json"]["status"] == gate
    result = report["result_json"]
    assert result["status"] == execution, result.get("reason")
    assert result["cleanup"] == "REMOVED"
    assert result["scan"]["status"] == "PASS"
    assert result["scan"]["engine_executed"] is True
    assert result["mcp"]["independent_server_process"] is True
    methods = [entry.get("message", {}).get("method") for entry in result["mcp"]["messages"]]
    assert methods.count("initialize") == methods.count("notifications/initialized") == 1
    assert "tools/list" in methods and "tools/call" in methods
    assert result["orchestration"] == "DETERMINISTIC_NO_LLM"
    assert report["project"]["current_stage"] == "PROTOTYPE"
    if execution == "SUCCEEDED":
        artifact = report["artifacts"][0]
        fetched = client.get("/artifacts/" + artifact["id"], params=scope.model_dump(), headers=headers)
        assert fetched.status_code == 200
        assert hashlib.sha256(fetched.content).hexdigest() == artifact["content_sha256"]
    else:
        assert report["artifacts"] == []
    # The test harness performs explicit reviewer API calls; this is not an autonomous human approval claim.
    for approval in report["approvals"]:
        current = store.get_run(human, scope, run_id)
        decision = client.post(
            "/approvals/" + approval["id"] + "/decision",
            headers=headers,
            json={
                "scope": scope.model_dump(),
                "proposal_hash": approval["proposal_hash"],
                "expected_run_revision": current["revision"],
                "decision": "APPROVED",
            },
        )
        assert decision.status_code == 200, decision.text
    actions = store.get_run(human, scope, run_id)["actions"]
    for action in sorted(actions, key=lambda value: value["tool"]):
        receipt = workflow.dispatch(scope, run_id, action["id"])
        assert receipt["receipt"]["status"] == "SUCCEEDED"
        assert workflow.dispatch(scope, run_id, action["id"])["replayed"] is True
    final = store.get_run(human, scope, run_id)
    assert final["state"] == "COMPLETED"
    assert final["project"]["current_stage"] == ("EVT" if gate == "PASS" else "PROTOTYPE")
    assert len(final["receipts"]) == (2 if gate == "PASS" else 1)
    assert store.verify_events(human, scope, run_id)
    memories = workflow.memory.search(workflow.role(scope), scope, "Aurora EVT")
    assert memories and memories[0].status == "CANDIDATE"
    restarted = load_workflow(Store(os.environ["FORGEFLOW_TEST_DATABASE_URL"]))
    assert restarted.memory.search(restarted.role(scope), scope, "Aurora EVT")[0].id == memories[0].id
    # A crash after result commit but before Memory write can replay consolidation without execution/duplicates.
    replayed = asyncio.run(restarted.check(scope, run_id))
    assert replayed["state"] == "COMPLETED"
    assert len(restarted.memory.search(restarted.role(scope), scope, "Aurora EVT")) == len(memories)
    evidence_root = os.environ.get("FORGEFLOW_EVIDENCE_DIR")
    if evidence_root:
        from pathlib import Path

        root = Path(evidence_root)
        root.mkdir(parents=True, exist_ok=True)
        (root / ("chain-" + fixture + ".json")).write_text(
            json.dumps(
                {
                    "scenario": fixture,
                    "reviewer": "SCRIPTED_TEST_HARNESS",
                    "scope": scope.model_dump(),
                    "run": final,
                    "events": store.events(human, scope, run_id),
                    "audit_verified": True,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )


def test_real_cancel_before_dispatch_has_no_receipt():
    store, scope, human, _, workflow = setup_chain()
    run = store.submit(human, RunRequest(scope=scope, fixture_id="aurora-pass"), "cancel")
    report = asyncio.run(workflow.check(scope, run["id"]))
    approval = report["approvals"][0]
    store.decide(human, scope, approval["id"], approval["proposal_hash"], report["revision"], "APPROVED")
    store.cancel(human, scope, run["id"])
    with pytest.raises(DomainError, match="CANCEL_REQUESTED"):
        workflow.dispatch(scope, run["id"], report["actions"][0]["id"])
    assert store.get_run(human, scope, run["id"])["receipts"] == []


def test_real_database_cancel_mid_mcp_check_waits_for_resource_ack():
    from concurrent.futures import ThreadPoolExecutor

    store, scope, human, _, workflow = setup_chain()
    run = store.submit(human, RunRequest(scope=scope, fixture_id="aurora-timeout"), "mid-check-cancel")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(workflow.check(scope, run["id"])))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = store.get_run(human, scope, run["id"])
            try:
                if (
                    current["steps"]
                    and workflow.sandbox._inspect(current["steps"][0]["sandbox_handle"])["State"]["Running"]
                ):
                    break
            except Exception:
                pass
            if future.done():
                pytest.fail("Check finished before persistent cancellation test reached the sandbox")
            time.sleep(0.05)
        else:
            pytest.fail("Owned check container did not start")
        accepted = store.cancel(human, scope, run["id"])
        assert accepted["state"] == "RUNNING"  # Intent accepted is not process-death evidence.
        final = future.result(timeout=15)
    assert final["state"] == "CANCELLED"
    assert final["approvals"] == final["receipts"] == []
    assert final["steps"][0]["state"] == "CANCELLED"
    assert workflow.sandbox.cleanup(final["steps"][0]["sandbox_handle"]) is True


def test_persisted_handle_paused_orphan_worker_kill_recovery_and_retry():
    store, scope, human, _, workflow = setup_chain()
    run = store.submit(human, RunRequest(scope=scope, fixture_id="aurora-pass"), "worker-crash")
    service = workflow.service(scope)
    claim = store.claim(service, scope, run["id"], "crash-probe", ttl=4)
    provider = workflow.sandbox
    payload = {"source": "SYNTHETIC", "case": "durable-worker-crash"}
    spec = ExecutionSpec(
        scope=scope,
        run_id=run["id"],
        step_id=claim["step_id"],
        attempt=claim["attempt"],
        template_id="timeout",
        input_manifest=payload,
        input_hash=digest(payload),
        code_hash=template_hash("timeout"),
        image=provider.resolve_image(),
        timeout_seconds=20,
    )
    handle = provider.handle(spec)
    store.set_sandbox_handle(service, scope, run["id"], claim["token"], claim["epoch"], handle)
    identity = {
        "scope": scope.model_dump(),
        "run_id": run["id"],
        "step_id": claim["step_id"],
        "attempt": claim["attempt"],
    }
    scanner_handle = workflow.scanner.handle(scope, run["id"], claim["step_id"], claim["attempt"])
    store.set_scanner_handle(service, scope, run["id"], claim["token"], claim["epoch"], scanner_handle)
    code = (
        "import json,sys; from forgeflow.contracts import ExecutionSpec; "
        "from forgeflow.sandbox import DockerSandbox; "
        "DockerSandbox().execute(ExecutionSpec.model_validate(json.loads(sys.stdin.read())))"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process.stdin.write(spec.model_dump_json().encode())
    process.stdin.close()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                if provider._inspect(handle)["State"]["Running"]:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("Owned child worker did not create its persisted container")
        provider._command(["pause", handle])
        assert provider._inspect(handle)["State"]["Paused"] is True
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    # Real DB lease expiration, not forced timestamp mutation.
    while time.time() < datetime.fromisoformat(claim["lease_expires_at"]).timestamp() + 0.1:
        time.sleep(0.05)
    recovered = workflow.recover(scope, run["id"])
    assert recovered["state"] == "QUEUED" and recovered["cleanup_required"] is False
    assert provider.cleanup(handle) is True
    assert workflow.scanner.cleanup(scanner_handle, identity=identity)["cleanup"] == "REMOVED"
    assert provider.execute(spec).status == "CANCELLED"  # persistent revocation prevents late restart
    with pytest.raises(DomainError, match="STALE_LEASE"):
        store.complete_check(
            service,
            scope,
            run["id"],
            claim["token"],
            claim["epoch"],
            {"status": "SUCCEEDED", "cleanup": "REMOVED"},
            {"status": "PASS"},
            {},
        )
    retried = asyncio.run(workflow.check(scope, run["id"]))
    assert retried["assessment_json"]["status"] == "PASS"
    assert retried["steps"][0]["attempt"] == 2
    assert any(event["kind"] == "RECOVERY_CLEANED" for event in store.events(human, scope, run["id"]))
    evidence_root = os.environ.get("FORGEFLOW_EVIDENCE_DIR")
    if evidence_root:
        from pathlib import Path

        (Path(evidence_root) / "durable-paused-orphan-recovery.json").write_text(
            json.dumps(
                {
                    "source": "REAL_PG_DOCKER_WORKER_SIGKILL",
                    "scope": scope.model_dump(),
                    "old_handle": handle,
                    "scanner_handle": scanner_handle,
                    "after_retry": retried,
                    "events": store.events(human, scope, run["id"]),
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
