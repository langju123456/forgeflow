"""Actual SQLite control-plane boundaries; sandbox/scanner are TEST_DRIVER only."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeflow.analysis_context import ROOT, SOP_CONTENT, SYNTHETIC_SCOPE
from forgeflow.context import KnowledgeService, MemoryService
from forgeflow.contracts import (
    DomainError,
    ExecutionResult,
    Principal,
    RunRequest,
    SandboxArtifact,
    Scope,
    canonical,
    digest,
)
from forgeflow.gate import evaluate
from forgeflow.s2c_checks import (
    DERIVED_FIXTURE_ID,
    DERIVED_MANIFEST_HASH,
    DERIVED_SOURCE_HASH,
    EVALUATION_CLOCK,
    S2CCheckWorkflow,
    S2CInputProvider,
    S2CRequest,
    S2CTrustedInput,
)
from forgeflow.scanner import Scanner
from forgeflow.skills import SkillRegistry
from forgeflow.store import Artifact, Document, Event, Evidence, Run, Step, Store, filters
from forgeflow.templates import template_hash
from forgeflow.workflow import Workflow


def fail(code, operation):
    with pytest.raises(DomainError) as caught:
        operation()
    assert caught.value.code == code


class SandboxDriver:
    def __init__(self):
        self.cleanups = []

    def template_hash(self, template):
        return template_hash(template)

    def resolve_image(self):
        return "sha256:" + "a" * 64

    def handle(self, spec):
        return "ff-" + digest({"scope": spec.scope.model_dump(), "run": spec.run_id,
                               "step": spec.step_id, "attempt": spec.attempt})[:32]

    def cleanup(self, handle):
        self.cleanups.append(handle)
        return True


class ScannerDriver:
    def __init__(self, store):
        self.store, self.calls, self.cleanups = store, [], []
        self.status = "PASS"
        self.cleanup_state = "REMOVED"

    def handle(self, scope, run_id, step_id, attempt):
        return Scanner.handle(scope, run_id, step_id, attempt)

    def scan(self, template, *, handle, identity, lease_deadline):
        with self.store._tx() as session:
            step = self.store._get(session, Step, Scope.model_validate(identity["scope"]), identity["step_id"])
            assert step.scanner_handle == handle and step.lease_token and step.attempt == identity["attempt"]
        self.calls.append((template, handle, identity, lease_deadline))
        return {"status": self.status, "source_hash": template_hash(template), "cleanup": self.cleanup_state,
                "provenance": "TEST_DRIVER"}

    def cleanup(self, handle, *, identity):
        self.cleanups.append((handle, identity))
        return {"cleanup": "REMOVED"}


class BridgeDriver:
    def __init__(self):
        self.calls = []
        self.status = "SUCCEEDED"
        self.cleanup_state = "REMOVED"
        self.bad_artifact = False
        self.interrupt = False
        self.after_execute = None

    async def execute(self, principal, spec, *, scan_receipt, lease_deadline, cancel_requested):
        self.calls.append(spec)
        assert principal.roles == {"hardware"} and spec.template_id == "evt-check"
        assert spec.input_hash == DERIVED_MANIFEST_HASH and scan_receipt["status"] == "PASS"
        if self.interrupt:
            raise asyncio.CancelledError("TEST_DRIVER_CRASH")
        manifest = copy.deepcopy(spec.input_manifest)
        if self.bad_artifact:
            manifest["metrics"]["thermal_peak_c"].pop("conflict")
        text = canonical({"schema_version": "evt-check-v1", "manifest": manifest,
                          "runtime": {"uid": 10001, "gid": 10001, "seccomp": "2"}})
        artifacts = () if self.status != "SUCCEEDED" else (SandboxArtifact(
            path="result.json", content=text, size=len(text.encode()), sha256=hashlib.sha256(text.encode()).hexdigest(),
        ),)
        result = ExecutionResult(handle=SandboxDriver().handle(spec), status=self.status,
                                 cleanup=self.cleanup_state, artifacts=artifacts)
        if self.after_execute:
            self.after_execute(spec)
        return result, {"status": "TEST_DRIVER"}


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 's2c.sqlite'}")
    store.create_schema()
    provider = S2CInputProvider(store)
    provider.create_schema()
    scope = SYNTHETIC_SCOPE
    owner = Principal(subject_id="owner", kind="user", roles=frozenset({"admin", "submitter", "pm"}),
                      scopes=(scope,))
    store.put_document(owner, scope, "aurora-evt-sop", "v1", SOP_CONTENT,
                       {"hardware", "pm", "supervisor", "admin", "submitter"}, 0,
                       valid_until="2026-11-05T00:00:00Z")
    sandbox, scanner, bridge = SandboxDriver(), ScannerDriver(store), BridgeDriver()
    workflow = S2CCheckWorkflow(store, sandbox, scanner, bridge, KnowledgeService(store), MemoryService(store),
                                SkillRegistry(ROOT / "skills"), input_provider=provider)
    yield store, provider, scope, owner, workflow, sandbox, scanner, bridge
    store.engine.dispose()


def submitted(env, key="derived"):
    _, provider, scope, owner, *_ = env
    return provider.submit(owner, S2CRequest(scope=scope), key)


def test_submit_commits_exact_binding_and_samekey_across_provider_restart(env):
    store, provider, scope, owner, *_ = env
    run = submitted(env)
    loaded = provider.load_run(owner, scope, run["id"])
    assert run["request_json"]["fixture_id"] == DERIVED_FIXTURE_ID
    assert loaded["witness"]["source_content_hash"] == DERIVED_SOURCE_HASH
    assert digest(loaded["manifest"]) == DERIVED_MANIFEST_HASH
    assert loaded["manifest"]["metrics"]["thermal_peak_c"]["conflict"] is True
    assert loaded["manifest"]["metrics"]["ci_smoke"]["valid_until"] == "2026-10-04T12:00:00Z"
    assert loaded["witness"]["evaluation_clock"] == EVALUATION_CLOCK
    restarted = S2CInputProvider(store)
    assert restarted.submit(owner, S2CRequest(scope=scope), "derived") == run
    with store._tx() as session:
        assert session.scalar(select(func.count()).select_from(S2CTrustedInput)) == 1
        assert session.scalar(select(func.count()).select_from(Run)) == 1
        assert session.scalar(select(func.count()).select_from(Step)) == 1
    assert [e["kind"] for e in store.events(owner, scope, run["id"])] == ["RUN_SUBMITTED", "TRUSTED_INPUT_BOUND"]


def test_atomic_submit_rolls_back_run_step_and_input_when_binding_event_fails(env, monkeypatch):
    store, provider, scope, owner, *_ = env
    original = store._event

    def broken(*args, **kwargs):
        if args[4] == "TRUSTED_INPUT_BOUND":
            raise RuntimeError("TEST_DRIVER_COMMIT_FAILURE")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError, match="COMMIT_FAILURE"):
        provider.submit(owner, S2CRequest(scope=scope), "fail")
    with store._tx() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == 0
        assert session.scalar(select(func.count()).select_from(Step)) == 0
        assert session.scalar(select(func.count()).select_from(S2CTrustedInput)) == 0


@pytest.mark.parametrize("extra", ["manifest", "path", "clock", "role", "network", "template_id"])
def test_public_request_forbids_input_and_authority_fields(env, extra):
    with pytest.raises(ValidationError):
        S2CRequest.model_validate({"scope": SYNTHETIC_SCOPE.model_dump(), extra: "attacker"})


def test_submit_denies_agent_scope_and_existing_canonical_key(env):
    store, provider, scope, owner, workflow, *_ = env
    fail("DERIVED_SUBMISSION_DENIED", lambda: provider.submit(workflow.role(scope), S2CRequest(scope=scope), "agent"))
    other = Scope(tenant_id="other", workspace_id="engineering", project_id="aurora")
    fail("NOT_FOUND", lambda: provider.submit(owner, S2CRequest(scope=other), "scope"))
    store.submit(owner, RunRequest(scope=scope, fixture_id="aurora-pass"), "collision")
    fail("IDEMPOTENCY_CONFLICT", lambda: provider.submit(owner, S2CRequest(scope=scope), "collision"))


@pytest.mark.parametrize("target", ["request", "manifest", "witness", "payload"])
def test_persisted_binding_tampering_cannot_publish_even_after_partial_rehash(env, target):
    store, provider, scope, owner, *_ = env
    run = submitted(env)
    with Session(store.engine) as session, session.begin():
        row = session.scalar(select(S2CTrustedInput).where(*filters(S2CTrustedInput, scope)))
        if target == "request":
            r = session.scalar(select(Run).where(*filters(Run, scope)))
            request = copy.deepcopy(r.request_json)
            request["trusted_input"]["evaluation_clock"] = "2026-10-01T00:00:00Z"
            r.request_json, r.request_hash = request, digest(request)
        elif target == "manifest":
            value = copy.deepcopy(row.manifest_json)
            value["metrics"]["thermal_peak_c"]["conflict"] = False
            row.manifest_json, row.manifest_hash = value, digest(value)
        elif target == "witness":
            value = copy.deepcopy(row.witness_json)
            value["source_content_hash"] = "a" * 64
            row.witness_json, row.witness_hash = value, digest(value)
        else:
            row.payload_hash = "a" * 64
    fail("DERIVED_INPUT_CHANGED", lambda: provider.load_run(owner, scope, run["id"]))


def test_input_row_cannot_reference_run_in_another_tenant(env):
    store, _, scope, _, *_ = env
    submitted(env)
    with pytest.raises(IntegrityError), Session(store.engine) as session, session.begin():
        row = session.scalar(select(S2CTrustedInput).where(*filters(S2CTrustedInput, scope)))
        row.tenant_id = "other"
        session.flush()
    with store._tx() as session:
        assert session.scalar(select(func.count()).select_from(S2CTrustedInput)) == 1


def test_pinned_file_drift_refuses_before_lease_or_tools(env, tmp_path):
    store, _, scope, owner, workflow, _, scanner, bridge = env
    run = submitted(env)
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    raw = (ROOT / "fixtures/aurora-pass.json").read_text()
    (fixture_root / "aurora-pass.json").write_text(raw + " ")
    workflow.input_provider = S2CInputProvider(store, fixture_root=fixture_root)
    fail("DERIVED_SOURCE_CHANGED", lambda: workflow.input_provider.load_run(owner, scope, run["id"]))
    assert scanner.calls == [] and bridge.calls == []
    assert store.get_run(owner, scope, run["id"])["state"] == "QUEUED"


async def test_derived_checker_executes_exact_input_then_persists_real_unknown_facts(env):
    store, provider, scope, owner, workflow, _, scanner, bridge = env
    canonical_parent = store.submit(owner, RunRequest(scope=scope, fixture_id="aurora-pass"), "old-parent")
    before = store.get_run(owner, scope, canonical_parent["id"])
    run = submitted(env)
    result = await workflow.check(scope, run["id"])
    assert len(scanner.calls) == len(bridge.calls) == 1
    assert bridge.calls[0].input_manifest != json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
    assert result["assessment_json"]["status"] == "UNKNOWN"
    checks = {r["rule"]: r for r in result["assessment_json"]["checks"]}
    assert checks["thermal_peak_c"]["status"] == checks["ci_smoke"]["status"] == "UNKNOWN"
    assert "conflicted" in checks["thermal_peak_c"]["reason"] and "expired" in checks["ci_smoke"]["reason"]
    assert sum(r["status"] == "SATISFIED" for r in checks.values()) == 3
    artifact = json.loads(result["artifacts"][0]["content"])
    assert digest(artifact["manifest"]) == DERIVED_MANIFEST_HASH
    assert result["snapshot_json"]["trusted_input"] == result["result_json"]["trusted_input"]
    assert result["receipts"] == [] and result["project"]["current_stage"] == "PROTOTYPE"
    assert {a["kind"] for a in result["approvals"]} == {"ISSUE"}
    checked = provider.checked(owner, scope, run["id"])
    assert checked[1] == artifact["manifest"] and checked[2]["kind"] == "CHECK_COMPLETED"
    assert checked[4].isoformat() != EVALUATION_CLOCK
    await workflow.check(scope, run["id"])
    assert len(scanner.calls) == len(bridge.calls) == 1
    assert store.get_run(owner, scope, canonical_parent["id"]) == before
    fail("S2C_BUSINESS_DISPATCH_DENIED", lambda: workflow.dispatch(scope, run["id"], result["actions"][0]["id"]))


@pytest.mark.parametrize("mode,expected", [
    ("timeout", "WAITING_APPROVAL"), ("cleanup_failure", "NEEDS_REVIEW"),
    ("wrong_artifact", "WAITING_APPROVAL"), ("scan_denied", "WAITING_APPROVAL"),
])
async def test_failures_never_become_confirmed_checks_or_publishable_inputs(env, mode, expected):
    _, provider, scope, owner, workflow, _, scanner, bridge = env
    if mode == "timeout":
        bridge.status = "TIMED_OUT"
    elif mode == "cleanup_failure":
        bridge.cleanup_state = "FAILED"
    elif mode == "wrong_artifact":
        bridge.bad_artifact = True
    else:
        scanner.status = "BLOCKED"
    run = submitted(env)
    result = await workflow.check(scope, run["id"])
    assert result["assessment_json"]["status"] == "UNKNOWN" and result["state"] == expected
    fail("DERIVED_CHECK_NOT_CONFIRMED", lambda: provider.checked(owner, scope, run["id"]))
    if mode == "scan_denied":
        assert bridge.calls == []


async def test_interruption_requires_recovery_fences_old_reply_and_reuses_bound_manifest(env):
    store, provider, scope, owner, workflow, sandbox, scanner, bridge = env
    run = submitted(env)
    bridge.interrupt = True
    with pytest.raises(asyncio.CancelledError):
        await workflow.check(scope, run["id"])
    view_before = store.get_run(workflow.service(scope), scope, run["id"])
    step = view_before["steps"][0]
    with pytest.raises(DomainError) as busy:
        await workflow.check(scope, run["id"])
    assert busy.value.code == "LEASE_BUSY"
    with store._tx() as session:
        row = store._get(session, Step, scope, step["id"])
        row.lease_expires_at = store._now(session) - timedelta(seconds=1)
    recovered = workflow.recover(scope, run["id"])
    assert recovered["state"] == "QUEUED" and recovered["cleanup_required"] is False
    assert sandbox.cleanups == [step["sandbox_handle"]]
    assert scanner.cleanups[0][0] == step["scanner_handle"]
    fail("STALE_LEASE", lambda: provider.finish(workflow.service(scope), scope, run["id"],
                                               step["lease_token"], step["fencing_epoch"], {}, {}, {}))
    bridge.interrupt = False
    result = await workflow.check(scope, run["id"])
    assert result["steps"][0]["attempt"] == 2
    assert bridge.calls[0].input_hash == bridge.calls[1].input_hash == DERIVED_MANIFEST_HASH
    assert bridge.calls[0].attempt == 1 and bridge.calls[1].attempt == 2
    assert provider.checked(owner, scope, run["id"])[0]["assessment_json"]["status"] == "UNKNOWN"


async def test_completion_rechecks_binding_in_sealed_commit_transaction(env):
    store, provider, scope, _, workflow, _, _, bridge = env
    run = submitted(env)

    def tamper(_):
        with Session(store.engine) as session, session.begin():
            row = session.scalar(select(S2CTrustedInput).where(*filters(S2CTrustedInput, scope)))
            row.payload_hash = "a" * 64

    bridge.after_execute = tamper
    with pytest.raises(DomainError) as rejected:
        await workflow.check(scope, run["id"])
    assert rejected.value.code == "DERIVED_INPUT_CHANGED"
    current = store.get_run(workflow.service(scope), scope, run["id"])
    assert current["assessment_json"] is None and current["state"] == "RUNNING"
    assert not [e for e in store.events(workflow.service(scope), scope, run["id"]) if e["kind"] == "CHECK_COMPLETED"]
    assert current["artifacts"] == [] and current["approvals"] == []


async def test_checked_reauthorizes_whole_run_documents_and_audit(env):
    store, provider, scope, owner, workflow, *_ = env
    run = submitted(env)
    await workflow.check(scope, run["id"])
    with Session(store.engine) as session, session.begin():
        document = session.scalar(select(Document).where(*filters(Document, scope)))
        document.roles = ["hardware"]
    fail("NOT_FOUND", lambda: provider.checked(owner, scope, run["id"]))
    with Session(store.engine) as session, session.begin():
        document = session.scalar(select(Document).where(*filters(Document, scope)))
        document.roles = ["hardware", "pm", "admin"]
        event = session.scalar(select(Event).where(*filters(Event, scope), Event.kind == "CHECK_COMPLETED"))
        event.event_hash = "a" * 64
    fail("INVALID_SOURCE_AUDIT", lambda: provider.checked(owner, scope, run["id"]))


@pytest.mark.parametrize("table", [Artifact, Evidence])
async def test_checked_binds_persisted_artifact_and_metric_rows_to_completed_source(env, table):
    store, provider, scope, owner, workflow, *_ = env
    run = submitted(env)
    await workflow.check(scope, run["id"])
    with Session(store.engine) as session, session.begin():
        row = session.scalar(select(table).where(*filters(table, scope)))
        if table is Artifact:
            row.content = canonical({"manifest": "substituted"})
            row.content_sha256, row.size = hashlib.sha256(row.content.encode()).hexdigest(), len(row.content.encode())
        else:
            data = copy.deepcopy(row.data)
            data["value"] = "substituted"
            row.data = data
    fail("DERIVED_CHECK_ARTIFACT_INVALID", lambda: provider.checked(owner, scope, run["id"]))


async def test_old_workflow_fails_closed_for_the_new_fixture_without_scanning(env):
    store, _, scope, _, workflow, sandbox, scanner, bridge = env
    run = submitted(env)
    old = Workflow(store, sandbox, scanner, bridge, workflow.knowledge, workflow.memory, workflow.skills)
    result = await old.check(scope, run["id"])
    assert result["state"] == "FAILED" and result["result_json"]["reason"] == "FileNotFoundError"
    assert scanner.calls == [] and bridge.calls == []
    assert result["assessment_json"]["status"] == "UNKNOWN"


def test_provider_does_not_accept_canonical_unbound_or_cross_scope_runs(env):
    store, provider, scope, owner, *_ = env
    canonical_run = store.submit(owner, RunRequest(scope=scope, fixture_id="aurora-pass"), "plain")
    fail("DERIVED_INPUT_NOT_BOUND", lambda: provider.load_run(owner, scope, canonical_run["id"]))
    run = submitted(env)
    other = Scope(tenant_id="other", workspace_id="engineering", project_id="aurora")
    fail("NOT_FOUND", lambda: provider.load_run(owner, other, run["id"]))


def test_frozen_clock_only_evaluates_synthetic_facts_not_current_authorization(env):
    _, provider, scope, owner, *_ = env
    run = submitted(env)
    loaded = provider.load_run(owner, scope, run["id"])
    result = evaluate(loaded["manifest"], {"status": "SUCCEEDED", "cleanup": "REMOVED"})
    assert result["status"] == "UNKNOWN"
    assert loaded["witness"]["evaluation_clock_kind"] == "FROZEN_SYNTHETIC"
