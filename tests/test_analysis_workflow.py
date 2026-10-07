"""TEST_DRIVER orchestration faults; these are not real LLM quality evidence.

The parent S1 check is explicitly seeded by a test driver. Real Docker/MCP and
real model runs have separate live acceptance evidence owned by the root runner.
"""

import asyncio
import copy
import json
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from forgeflow import contracts
from forgeflow.analysis_context import SOP_CONTENT, SYNTHETIC_SCOPE, AnalysisContextBuilder
from forgeflow.analysis_provider import MODEL, MODEL_DIGEST, OllamaProvider
from forgeflow.analysis_store import AnalysisCall, AnalysisRun, AnalysisStore
from forgeflow.analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from forgeflow.context import MemoryService
from forgeflow.contracts import DomainError, Principal, RunRequest, Scope, digest
from forgeflow.gate import RULE_HASH, evaluate
from forgeflow.policy import POLICY_HASH
from forgeflow.skills import SkillRegistry
from forgeflow.store import Document, Store, filters
from forgeflow.templates import template_hash

ROOT = Path(__file__).resolve().parents[1]


class TestDriverProvider:
    __test__ = False
    provenance = "TEST_DRIVER"

    def __init__(self, *, mutate=None, wait=False, after=None):
        self.calls = []
        self.mutate = mutate
        self.wait = wait
        self.after = after
        self.started = asyncio.Event()

    async def generate(self, call):
        self.calls.append(copy.deepcopy(call))
        self.started.set()
        if self.wait:
            await asyncio.Event().wait()
        material = json.loads(call["messages"][-1]["content"])
        source = material["evidence"]["sources"][0]
        ref = source["ref"]
        phase = len(self.calls)
        output = {
            1: {"assignment": "pm", "task_question": "Review readiness gaps", "source_refs": [ref]},
            2: {"requests": [{"tool": "knowledge.search", "query": "Aurora EVT", "top_k": 1}]},
            3: {
                "status": "COMPLETE",
                "facts": [{"text": f"The recorded technical gate is {source['gate']}", "source_refs": [ref]}],
                "risks": [],
                "unknowns": [],
                "recommendations": [
                    {"text": "Review evidence before approving actions", "source_refs": [ref]}
                ],
            },
            4: {
                "status": "COMPLETE",
                "summary": "Analysis complete; human approval remains separate",
                "source_refs": [ref],
                "report_refs": ["report_pm"],
            },
        }[phase]
        reply = {
            "status": "SUCCEEDED",
            "content": output,
            "content_utf8": json.dumps(output),
            "actual_model_revision": MODEL_DIGEST,
            "finish_reason": "stop",
            "dispatched": True,
            "usage": {"state": "CONFIRMED", "input_tokens": 200, "output_tokens": 100, "cost_microusd": None},
            "provenance": "TEST_DRIVER",
            "transport": "TEST_DRIVER",
            "completion_confirmed": True,
        }
        if self.mutate:
            self.mutate(phase, reply)
        if self.after:
            self.after(phase, reply)
        return reply


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'workflow.sqlite'}")
    store.create_schema()
    scope = SYNTHETIC_SCOPE
    caller = Principal(
        subject_id="test-driver-user",
        kind="user",
        scopes=(scope,),
        clearance=0,
        roles=frozenset({"submitter", "pm", "supervisor", "hardware", "admin", "reviewer"}),
    )
    worker = Principal(
        subject_id="TEST_DRIVER_HARDWARE",
        kind="service",
        scopes=(scope,),
        clearance=0,
        roles=frozenset({"hardware", "worker"}),
    )
    store.put_document(
        caller,
        scope,
        "aurora-evt-sop",
        "v1",
        SOP_CONTENT,
        {"hardware", "pm", "supervisor", "admin", "reviewer", "submitter"},
        0,
        valid_until="2026-11-05T00:00:00Z",
    )
    skillroot = tmp_path / "skills"
    skillroot.mkdir()
    for name in ("pm-analysis", "supervisor-analysis"):
        shutil.copytree(ROOT / "skills" / name, skillroot / name)
    registry = SkillRegistry(skillroot, role_tools=ROLE_TOOLS)
    ledger = AnalysisStore(store)
    ledger.create_schema()
    return store, ledger, scope, caller, worker, registry


def checked(env, fixture="aurora-pass"):
    store, _, scope, caller, worker, _ = env
    run = store.submit(caller, RunRequest(scope=scope, fixture_id=fixture), fixture)
    lease = store.claim(worker, scope, run["id"], "TEST_DRIVER", ttl=90)
    manifest = json.loads((ROOT / "fixtures" / f"{fixture}.json").read_text())
    result = {
        "status": "SUCCEEDED",
        "cleanup": "REMOVED",
        "synthetic": True,
        "orchestration": "TEST_DRIVER",
        "scan": {"status": "TEST_DRIVER"},
        "mcp": {"status": "TEST_DRIVER"},
    }
    snapshot = {
        "fixture_version": "v1",
        "input_hash": digest(manifest),
        "input_manifest_hash": digest(manifest),
        "rule_hash": RULE_HASH,
        "policy_hash": POLICY_HASH,
        "code_hash": template_hash("evt-check"),
        "documents": [{"id": "aurora-evt-sop", "version": "v1", "source_hash": digest_text(SOP_CONTENT)}],
        "classification": 0,
        "valid_until": "2026-11-05T00:00:00Z",
    }
    return store.complete_check(
        worker,
        scope,
        run["id"],
        lease["token"],
        lease["epoch"],
        result,
        evaluate(manifest, result, now=contracts.utcnow()),
        snapshot,
    )


def digest_text(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def workflow(env, provider, ledger=None):
    store, current, _, _, _, registry = env
    return AnalysisWorkflow(ledger or current, provider, AnalysisContextBuilder(store), registry)


def parent_unchanged(env, original):
    store, _, scope, caller, _, _ = env
    assert store.get_run(caller, scope, original["id"]) == original
    assert not original["receipts"]
    assert original["project"]["current_stage"] == "PROTOTYPE"
    assert all(approval["decision"] == "PENDING" for approval in original["approvals"])


@pytest.mark.parametrize(
    "fixture,gate", [("aurora-pass", "PASS"), ("aurora-blocked", "BLOCKED"), ("aurora-unknown", "UNKNOWN")]
)
async def test_four_independent_role_calls_preserve_gate_and_actions(env, fixture, gate):
    original = checked(env, fixture)
    provider = TestDriverProvider()
    _, _, scope, caller, _, _ = env
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "normal")
    assert result["state"] == "COMPLETE"
    assert len(provider.calls) == 4
    assert [call["phase"] for call in result["calls"]] == [
        "PLAN",
        "CONTEXT_PROPOSAL",
        "FINAL_REPORT",
        "SUMMARY",
    ]
    assert all(len(call["messages"]) == 2 for call in provider.calls)
    assert all(call["state"] == "VALIDATED" for call in result["calls"])
    assert all(call["provider_metadata_json"]["transport"] == "TEST_DRIVER" for call in result["calls"])
    assert gate in result["calls"][2]["output_json"]["facts"][0]["text"]
    assert "reports" not in json.loads(provider.calls[1]["messages"][1]["content"])["evidence"]
    engine = workflow(env, provider)
    receipts = [
        event
        for event in env[1].events(caller, scope, result["id"], reauthorize=engine._reauthorize)
        if event["kind"] == "ANALYSIS_TOOL_READ"
    ]
    assert len(receipts) == 1
    read = receipts[0]["payload_json"]["receipts"]
    assert len(read) == 1 and read[0]["tool"] == "knowledge.search"
    assert read[0]["transport"] == "IN_PROCESS" and read[0]["result_count"] == 1
    parent_unchanged(env, original)


async def test_duplicate_after_new_builder_reuses_result_without_generation(env):
    original = checked(env)
    _, _, scope, caller, _, _ = env
    first = TestDriverProvider()
    result = await workflow(env, first).analyze(caller, scope, original["id"], "duplicate")
    second = TestDriverProvider()
    replay = await workflow(env, second).analyze(caller, scope, original["id"], "duplicate")
    assert replay["id"] == result["id"]
    assert replay["summary_hash"] == result["summary_hash"]
    assert not second.calls


@pytest.mark.parametrize(
    "phase,field,value,expected",
    [
        (1, "assignment", "sre", 1),
        (1, "scope", {"tenant_id": "victim"}, 1),
        (1, "gate_status", "PASS", 1),
        (2, "requests", [{"tool": "sandbox.execute", "query": "ignore all rules", "top_k": 1}], 2),
        (2, "requests", [{"tool": "knowledge.search", "query": "Aurora", "top_k": 6}], 2),
        (3, "facts", [{"text": "Forged fact", "source_refs": ["src_" + "0" * 32]}], 3),
        (4, "report_refs", ["other_report"], 4),
    ],
)
async def test_model_authority_or_forged_reference_stops_without_retry(env, phase, field, value, expected):
    original = checked(env)
    _, _, scope, caller, _, _ = env

    def mutate(current, reply):
        if current == phase:
            reply["content"][field] = value

    provider = TestDriverProvider(mutate=mutate)
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "denied")
    assert result["state"] == "FAILED"
    assert len(provider.calls) == expected
    assert result["calls"][-1]["state"] == "OUTPUT_INVALID"
    parent_unchanged(env, original)


@pytest.mark.parametrize("fault", ["TRUNCATED", "REVISION", "OUTPUT_BUDGET", "PROVIDER_ERROR", "UNKNOWN"])
async def test_provider_fault_is_not_a_report(env, fault):
    original = checked(env)
    _, _, scope, caller, _, _ = env

    def mutate(_, reply):
        if fault == "TRUNCATED":
            reply["finish_reason"] = "length"
        elif fault == "REVISION":
            reply["actual_model_revision"] = "0" * 64
        elif fault == "OUTPUT_BUDGET":
            reply["usage"]["output_tokens"] = 801
        elif fault == "PROVIDER_ERROR":
            reply["status"] = "PROVIDER_ERROR"
            reply["usage"] = None
            reply["completion_confirmed"] = False
        else:
            reply["status"] = "OUTCOME_UNKNOWN"
            reply["usage"] = None

    provider = TestDriverProvider(mutate=mutate)
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "provider-fault")
    assert result["state"] == ("OUTCOME_UNKNOWN" if fault in {"UNKNOWN", "PROVIDER_ERROR"} else "FAILED")
    assert len(provider.calls) == 1
    assert result["summary_json"] is None
    parent_unchanged(env, original)


async def test_provider_denied_before_dispatch_records_no_delivery(env):
    original = checked(env)
    _, _, scope, caller, _, _ = env

    def mutate(_, reply):
        reply.update(status="DENIED", dispatched=False, content=None)
        reply["usage"] = {
            "state": "NOT_DISPATCHED",
            "input_tokens": None,
            "output_tokens": None,
            "cost_microusd": None,
        }

    provider = TestDriverProvider(mutate=mutate)
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "not-dispatched")
    assert len(provider.calls) == 1  # Adapter admission, no model delivery.
    assert result["state"] == "FAILED"
    assert result["calls"][0]["delivery_status"] == "NOT_DISPATCHED"


async def test_cancel_inflight_retains_unknown_and_prevents_same_key_resend(env):
    original = checked(env)
    _, ledger, scope, caller, _, _ = env
    provider = TestDriverProvider(wait=True)
    engine = workflow(env, provider)
    task = asyncio.create_task(engine.analyze(caller, scope, original["id"], "cancel"))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    with ledger.store._tx() as session:
        row = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope)))
        analysis_id = row.id
    ledger.cancel(caller, scope, analysis_id)
    result = await asyncio.wait_for(task, timeout=2)
    assert result["state"] == "CANCELLED"
    assert result["calls"][0]["state"] == "OUTCOME_UNKNOWN"
    assert result["tokens_reserved"] == 4300
    assert len(provider.calls) == 1
    replay = await workflow(env, TestDriverProvider()).analyze(caller, scope, original["id"], "cancel")
    assert replay["state"] == "CANCELLED"


async def test_process_interrupt_preserves_unknown_without_automatic_recovery_retry(env):
    original = checked(env)
    _, ledger, scope, caller, _, _ = env
    provider = TestDriverProvider(wait=True)
    task = asyncio.create_task(workflow(env, provider).analyze(caller, scope, original["id"], "interrupt"))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replay_provider = TestDriverProvider()
    replay = await workflow(env, replay_provider).analyze(caller, scope, original["id"], "interrupt")
    assert replay["state"] == "OUTCOME_UNKNOWN"
    assert replay["calls"][0]["usage_state"] == "UNKNOWN"
    assert not replay_provider.calls
    with pytest.raises(DomainError, match="ANALYSIS_LEASE_BUSY"):
        ledger.recover(caller, scope, replay["id"])
    with ledger.store._tx() as session:
        row = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope)))
        row.lease_expires_at = contracts.utcnow() - timedelta(seconds=1)
    assert ledger.recover(caller, scope, replay["id"])["state"] == "OUTCOME_UNKNOWN"


async def test_timeout_bound_cancels_even_a_test_driver_that_ignores_call_deadline(env):
    original = checked(env)
    _, ledger, scope, caller, _, _ = env

    class ShortDeadlineStore:
        def __getattr__(self, key):
            return getattr(ledger, key)

        def reserve_call(self, *args, **kwargs):
            kwargs["timeout_seconds"] = 0.03
            return ledger.reserve_call(*args, **kwargs)

    provider = TestDriverProvider(wait=True)
    result = await asyncio.wait_for(
        workflow(env, provider, ShortDeadlineStore()).analyze(caller, scope, original["id"], "timeout"),
        timeout=2,
    )
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert len(provider.calls) == 1
    assert result["tokens_reserved"] == 4300


async def test_cross_scope_is_denied_before_provider_call(env):
    original = checked(env)
    _, _, _, caller, _, _ = env
    other = Scope(tenant_id="other-tenant", workspace_id="engineering", project_id="aurora")
    caller = caller.model_copy(update={"scopes": (*caller.scopes, other)})
    provider = TestDriverProvider()
    with pytest.raises(DomainError):
        await workflow(env, provider).analyze(caller, other, original["id"], "cross-tenant")
    assert not provider.calls


async def test_submitter_without_model_role_cannot_create_an_analysis(env):
    original = checked(env)
    store, _, scope, caller, _, _ = env
    observer = caller.model_copy(update={"roles": frozenset({"submitter"})})
    provider = TestDriverProvider()
    with pytest.raises(DomainError):
        await workflow(env, provider).analyze(observer, scope, original["id"], "observer")
    assert not provider.calls
    with store._tx() as session:
        assert session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope))) is None


async def test_source_revocation_after_delivery_preserves_usage_and_denies_publication(env):
    original = checked(env)
    store, ledger, scope, caller, _, _ = env

    def after(phase, _):
        if phase == 1:
            with store._tx() as session:
                document = session.scalar(select(Document).where(*filters(Document, scope)))
                document.roles = ["hardware", "admin", "reviewer"]

    provider = TestDriverProvider(after=after)
    with pytest.raises(DomainError):
        await workflow(env, provider).analyze(caller, scope, original["id"], "revoked")
    with store._tx() as session:
        analysis = session.scalar(select(AnalysisRun).where(*filters(AnalysisRun, scope)))
        call = session.scalar(select(AnalysisCall).where(*filters(AnalysisCall, scope)))
        assert analysis.state == "INCOMPLETE"
        assert call.state == "PUBLICATION_REJECTED"
        assert call.usage_state == "CONFIRMED"
        assert call.output_json is None
    assert len(provider.calls) == 1


async def test_oversized_application_report_is_invalid_without_a_fourth_call(env):
    original = checked(env)
    _, _, scope, caller, _, _ = env

    def mutate(phase, reply):
        if phase == 3:
            ref = reply["content"]["facts"][0]["source_refs"]
            reply["content"]["facts"] = [{"text": "x" * 500, "source_refs": ref} for _ in range(10)]

    provider = TestDriverProvider(mutate=mutate)
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "input-budget")
    assert result["state"] == "FAILED"
    assert len(provider.calls) == 3
    assert result["calls"][-1]["state"] == "OUTPUT_INVALID"
    assert result["summary_json"] is None
    parent_unchanged(env, original)


async def test_authorized_but_oversized_retrieval_exhausts_input_budget_before_report(env):
    original = checked(env)
    store, _, scope, caller, worker, _ = env
    writer = worker.model_copy(update={"roles": worker.roles | {"memory_writer"}})
    event = next(
        item for item in store.events(writer, scope, original["id"]) if item["kind"] == "CHECK_COMPLETED"
    )
    for index in range(5):
        MemoryService(store).propose(
            writer,
            scope,
            original["id"],
            f"Aurora candidate {index}",
            "Aurora EVT candidate, not confirmed: " + "bounded synthetic note " * 25,
            [event["id"]],
        )

    def mutate(phase, reply):
        if phase == 2:
            reply["content"]["requests"] = [{"tool": "memory.search", "query": "Aurora EVT", "top_k": 5}]

    provider = TestDriverProvider(mutate=mutate)
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "context-budget")
    assert result["state"] == "FAILED"
    assert len(provider.calls) == 2
    assert result["summary_json"] is None
    assert all(call["state"] == "VALIDATED" for call in result["calls"])


async def test_compact_projection_preserves_every_unknown_rule_and_stage_fact(env):
    original = checked(env, "aurora-unknown")
    _, _, scope, caller, _, _ = env
    engine = workflow(env, TestDriverProvider())
    bundle = engine.context.build(caller, scope, original["id"])
    before = bundle.model_context["sources"][0]
    projected = engine._projection(bundle)["sources"][0]
    assert len(projected["measurements"]) == 5
    for rule, state in before["checks"].items():
        assert projected["measurements"][rule] == [
            before["metrics"].get(rule),
            state,
            before["evidence_status"].get(rule, "MISSING"),
        ]
    for key in ("ref", "synthetic", "gate", "checker", "cleanup", "stage", "transition", "scan", "mcp"):
        assert projected[key] == before[key]
    assert projected["measurements"]["ci_smoke"] == [None, "UNKNOWN", "MISSING"]
    assert "checks" in before  # The source bundle itself was never changed.


async def test_denied_dynamic_skill_does_not_call_provider(env):
    original = checked(env)
    store, ledger, scope, caller, _, registry = env
    denied = SkillRegistry(registry.root, role_tools={**ROLE_TOOLS, "pm": frozenset({"knowledge.search"})})
    provider = TestDriverProvider()
    engine = AnalysisWorkflow(ledger, provider, AnalysisContextBuilder(store), denied)
    with pytest.raises(DomainError, match="ANALYSIS_SKILL_DENIED"):
        await engine.analyze(caller, scope, original["id"], "denied-skill")
    assert not provider.calls


async def test_http500_unconfirmed_completion_holds_global_slot_across_provider_instances(env):
    """Real adapter + TEST_DRIVER HTTP transport, never a real model request."""
    original = checked(env)
    _, _, scope, caller, _, _ = env
    requests = []

    class DriverBytes(httpx.AsyncByteStream):
        def __init__(self, value):
            self.value = json.dumps(value).encode()

        async def __aiter__(self):
            yield self.value

    async def handle(request):
        requests.append(request.url.path)
        bodies = {
            "/api/version": {"version": "0.32.1"},
            "/api/tags": {"models": [{"name": MODEL, "digest": MODEL_DIGEST}]},
            "/api/show": {
                "details": {"format": "gguf"},
                "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
                "capabilities": ["completion", "thinking"],
            },
            "/api/ps": {"models": []},
            "/api/chat": {"error": "TEST_DRIVER_ACCEPTANCE_UNKNOWN"},
        }
        return httpx.Response(
            500 if request.url.path == "/api/chat" else 200,
            stream=DriverBytes(bodies[request.url.path]),
        )

    provider = OllamaProvider(transport=httpx.MockTransport(handle))
    result = await workflow(env, provider).analyze(caller, scope, original["id"], "http500")
    assert result["state"] == "OUTCOME_UNKNOWN"
    assert result["calls"][0]["state"] == "OUTCOME_UNKNOWN"
    assert result["tokens_reserved"] == 4300
    assert requests.count("/api/chat") == 1
    requests.clear()
    fresh_instance = OllamaProvider(transport=httpx.MockTransport(handle))
    next_result = await workflow(env, fresh_instance).analyze(caller, scope, original["id"], "after-http500")
    assert next_result["state"] == "FAILED"
    assert not next_result["calls"]
    assert not requests  # Durable concurrency admission rejects before the adapter.
