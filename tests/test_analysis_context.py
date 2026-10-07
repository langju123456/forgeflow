"""S2 source/authority negatives on real SQLite Store; no model/Docker calls."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeflow import context, contracts
from forgeflow.analysis_context import (
    ANALYSIS_POLICY_HASH,
    PROFILE_ID,
    ROOT,
    SOP_CONTENT,
    SOP_HASH,
    SYNTHETIC_SCOPE,
    AnalysisContextBuilder,
    ExportGrant,
    SourceGrant,
)
from forgeflow.analysis_contracts import compact_schema, parse_output, strict_json_object
from forgeflow.context import MemoryService
from forgeflow.contracts import DomainError, Principal, RunRequest, Scope, digest
from forgeflow.gate import RULE_HASH, evaluate
from forgeflow.policy import POLICY_HASH
from forgeflow.store import Document, DocumentVersion, Run, Store, filters
from forgeflow.templates import template_hash


@pytest.fixture
def env(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 's2-context.sqlite'}")
    store.create_schema()
    scope = SYNTHETIC_SCOPE
    caller = Principal(
        subject_id="owner",
        kind="user",
        roles=frozenset({"submitter", "admin", "pm"}),
        scopes=(scope,),
        clearance=1,
    )
    worker = Principal(
        subject_id="harness",
        kind="service",
        roles=frozenset({"worker", "hardware", "memory_writer"}),
        scopes=(scope,),
        clearance=1,
    )
    store.put_document(
        caller,
        scope,
        "aurora-evt-sop",
        "v1",
        SOP_CONTENT,
        {"hardware", "pm", "supervisor", "admin", "submitter"},
        0,
    )
    run = store.submit(caller, RunRequest(scope=scope, fixture_id="aurora-pass"), "context-test")
    claim = store.claim(worker, scope, run["id"], "test-harness")
    manifest = json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
    result = {
        "status": "SUCCEEDED",
        "cleanup": "REMOVED",
        "synthetic": True,
        "artifacts": [],
        "stdout": "HOST_SECRET_EXCLUDE",
        "stderr": "/private/key/HOST_PATH_EXCLUDE",
        "reason": "ERROR_EXCLUDE",
        "scan": {"status": "PASS", "detail": "SCAN_SECRET_EXCLUDE"},
        "mcp": {"status": "CONNECTED", "transcript": "TRANSCRIPT_EXCLUDE"},
    }
    snapshot = {
        "input_hash": digest(manifest),
        "input_manifest_hash": digest(manifest),
        "rule_hash": RULE_HASH,
        "policy_hash": POLICY_HASH,
        "code_hash": template_hash("evt-check"),
        "fixture_version": "v1",
        "classification": 0,
        "documents": [{"id": "aurora-evt-sop", "version": "v1", "source_hash": SOP_HASH}],
        "valid_until": "2026-11-05T00:00:00Z",
    }
    store.complete_check(
        worker, scope, run["id"], claim["token"], claim["epoch"], result, evaluate(manifest, result), snapshot
    )
    builder = AnalysisContextBuilder(store)
    yield store, scope, caller, worker, run["id"], builder
    store.engine.dispose()


def report_for(ref):
    return {
        "status": "COMPLETE",
        "facts": [{"text": "Synthetic gate passed; human decision still required.", "source_refs": [ref]}],
        "risks": [],
        "unknowns": [],
        "recommendations": [],
    }


def error(code, operation):
    with pytest.raises(DomainError) as raised:
        operation()
    assert raised.value.code == code


def test_projection_uses_real_completed_event_and_excludes_transcript(env):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    text = json.dumps(bundle.model_context)
    for forbidden in (
        "HOST_SECRET",
        "HOST_PATH",
        "ERROR_EXCLUDE",
        "SCAN_SECRET",
        "TRANSCRIPT_EXCLUDE",
        "lease_token",
        "approvals",
        "actions",
        "artifacts",
        "tenant_id",
        "run_id",
    ):
        assert forbidden not in text
    assert bundle.model_context["sources"][0]["gate"] == "PASS"
    assert bundle.principal.kind == "agent" and bundle.principal.roles == frozenset({"supervisor"})
    assert bundle.principal.clearance == 0
    assert bundle.principal.scopes == (scope,)
    assert store.get_run(caller, scope, run_id)["project"]["current_stage"] == "PROTOTYPE"
    assert len(bundle.citations) == 2


def test_source_refs_stable_across_roles_and_builder_restart(env):
    store, scope, caller, _, run_id, builder = env
    supervisor = builder.build(caller, scope, run_id)
    pm = builder.build(caller, scope, run_id, "pm")
    restarted = AnalysisContextBuilder(store).build(caller, scope, run_id)
    assert supervisor.citations == pm.citations == restarted.citations
    assert supervisor.material_hash == pm.material_hash == restarted.material_hash
    assert builder.snapshot(supervisor) == AnalysisContextBuilder(store).snapshot(restarted)
    other = Scope(tenant_id="another", workspace_id=scope.workspace_id, project_id=scope.project_id)
    ref = next(iter(supervisor.citations))
    binding = supervisor.citations[ref]
    assert builder._reference(other, run_id, binding.kind, binding.id, binding.source_hash) != ref


def test_caller_cannot_gain_scope_or_hardware_role(env):
    _, scope, caller, worker, run_id, builder = env
    other = Scope(tenant_id="other", workspace_id="engineering", project_id="aurora")
    error("NOT_FOUND", lambda: builder.build(caller, other, run_id))
    error("ANALYSIS_ROLE_DENIED", lambda: builder.build(caller, scope, run_id, "hardware"))
    error("ANALYSIS_ROLE_DENIED", lambda: builder.build(worker, scope, run_id))
    inaccessible = caller.model_copy(update={"roles": frozenset({"nothing"})})
    error("NOT_FOUND", lambda: builder.build(inaccessible, scope, run_id))


def test_whole_run_acl_refuses_reprojection_of_hardware_only_sources(env):
    store, scope, caller, _, run_id, builder = env
    with Session(store.engine) as session, session.begin():
        document = session.scalar(
            select(Document).where(*filters(Document, scope), Document.id == "aurora-evt-sop")
        )
        document.roles = ["hardware", "admin"]
    error("NOT_FOUND", lambda: builder.build(caller, scope, run_id))


def test_document_supersession_invalidates_existing_context(env):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    store.put_document(
        caller,
        scope,
        "aurora-evt-sop",
        "v2",
        "Changed synthetic source",
        {"hardware", "pm", "supervisor", "admin"},
        0,
    )
    error("SOURCE_CHANGED", lambda: builder.revalidate(bundle))


def test_document_expiry_invalidates_existing_context(env):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    with Session(store.engine) as session, session.begin():
        version = session.scalar(
            select(DocumentVersion).where(
                *filters(DocumentVersion, scope), DocumentVersion.document_id == "aurora-evt-sop"
            )
        )
        version.valid_until = contracts.utcnow() - timedelta(days=1)
    error("EVIDENCE_EXPIRED", lambda: builder.revalidate(bundle))


def test_result_mutation_and_prompt_mutation_fail_closed(env):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    forged = replace(bundle, model_context={"sources": [{"text": "Forged fact"}]})
    error("SOURCE_CHANGED", lambda: builder.revalidate(forged))
    with Session(store.engine) as session, session.begin():
        run = session.scalar(select(Run).where(*filters(Run, scope), Run.id == run_id))
        run.result_json = {**run.result_json, "status": "FAILED"}
    error("SOURCE_CHANGED", lambda: builder.revalidate(bundle))


def test_uncompleted_run_never_becomes_fact_context(env):
    store, scope, caller, _, _, builder = env
    run = store.submit(caller, RunRequest(scope=scope, fixture_id="aurora-pass"), "uncompleted")
    error("CHECK_NOT_COMPLETED", lambda: builder.build(caller, scope, run["id"]))


def test_knowledge_and_candidate_memory_keep_separate_origins(env):
    store, scope, caller, worker, run_id, builder = env
    completed = next(
        event for event in store.events(caller, scope, run_id) if event["kind"] == "CHECK_COMPLETED"
    )
    MemoryService(store).propose(
        worker, scope, run_id, "Aurora EVT readiness", "Aurora EVT awaits human review", [completed["id"]]
    )
    bundle = builder.build(caller, scope, run_id, "pm")
    proposal = parse_output(
        "context",
        {
            "requests": [
                {"tool": "knowledge.search", "query": "Aurora EVT", "top_k": 2},
                {"tool": "memory.search", "query": "Aurora EVT", "top_k": 2},
            ]
        },
    )
    expanded = builder.search(bundle, proposal)
    memory = [item for item in expanded.model_context["sources"] if item["kind"] == "memory"]
    assert len(memory) == 1 and memory[0]["status"] == "CANDIDATE"
    assert len(expanded.citations) == 3
    assert [item["tool"] for item in expanded.tool_receipts] == ["knowledge.search", "memory.search"]
    assert all(
        item["transport"] == "IN_PROCESS" and item["result_count"] == 1 for item in expanded.tool_receipts
    )
    assert "tool_receipts" not in expanded.model_context


def test_caller_and_role_intersection_before_content_and_ranking(env, monkeypatch):
    store, scope, caller, _, run_id, _ = env
    source = store.put_document(caller, scope, "only-pm", "v1", "CALLER_ACL_PROBE", {"pm"}, 0)
    grant = ExportGrant(
        documents=(
            SourceGrant("aurora-evt-sop", "v1", SOP_HASH),
            SourceGrant("only-pm", "v1", source["source_hash"]),
        )
    )
    builder = AnalysisContextBuilder(store, grant)
    restricted_caller = caller.model_copy(update={"roles": frozenset({"submitter"})})
    bundle = builder.build(restricted_caller, scope, run_id, "pm")
    ranked = []
    original = context._score
    monkeypatch.setattr(
        context, "_score", lambda query, row: ranked.append(row["id"]) or original(query, row)
    )
    citations = []
    original_citation = builder._citation
    monkeypatch.setattr(
        builder,
        "_citation",
        lambda p, s, k, identifier, session=None: (
            citations.append(identifier) or original_citation(p, s, k, identifier, session)
        ),
    )
    proposal = parse_output(
        "context", {"requests": [{"tool": "knowledge.search", "query": "CALLER_ACL_PROBE", "top_k": 1}]}
    )
    expanded = builder.search(bundle, proposal)
    assert "only-pm" not in ranked
    assert source["citation_id"] not in citations
    assert expanded.tool_receipts[0]["result_count"] == 0
    # The exact same query has a positive privileged twin, proving a real ACL
    # denial rather than an accidental lexical nonmatch.
    privileged = builder.build(caller, scope, run_id, "pm")
    expanded = builder.search(privileged, proposal)
    assert expanded.tool_receipts[0]["result_count"] == 1
    assert source["citation_id"] in citations


def test_visible_document_still_requires_export_grant(env):
    store, scope, caller, _, run_id, builder = env
    store.put_document(caller, scope, "new-synthetic", "v1", "NEW_EXPORT_PROBE", {"pm", "admin"}, 0)
    bundle = builder.build(caller, scope, run_id, "pm")
    proposal = parse_output(
        "context", {"requests": [{"tool": "knowledge.search", "query": "NEW_EXPORT_PROBE", "top_k": 1}]}
    )
    expanded = builder.search(bundle, proposal)
    assert expanded.citations == bundle.citations
    assert "NEW_EXPORT_PROBE" not in json.dumps(expanded.model_context)


def test_pm_private_report_cannot_be_shared_with_supervisor(env):
    store, scope, caller, _, run_id, _ = env
    content = "PRIVATE_PM_ONLY synthetic fixture note"
    source = store.put_document(caller, scope, "pm-private", "v1", content, {"pm", "admin"}, 0)
    grant = ExportGrant(
        documents=(
            SourceGrant("aurora-evt-sop", "v1", SOP_HASH),
            SourceGrant("pm-private", "v1", source["source_hash"]),
        )
    )
    builder = AnalysisContextBuilder(store, grant)
    supervisor = builder.build(caller, scope, run_id)
    pm = builder.build(caller, scope, run_id, "pm")
    pm = builder.search(
        pm,
        parse_output(
            "context", {"requests": [{"tool": "knowledge.search", "query": "PRIVATE_PM_ONLY", "top_k": 1}]}
        ),
    )
    private_ref = pm.model_context["sources"][-1]["ref"]
    report = builder.validate_output(pm, "report", report_for(private_ref))
    error("NOT_FOUND", lambda: builder.share_report(pm, supervisor, report))
    # Even an uncited private excerpt in the context prevents whole-report sharing.
    report = builder.validate_output(pm, "report", report_for(pm.model_context["sources"][0]["ref"]))
    error("NOT_FOUND", lambda: builder.share_report(pm, supervisor, report))


def test_share_report_and_summary_references_are_reauthorized(env):
    _, scope, caller, _, run_id, builder = env
    supervisor = builder.build(caller, scope, run_id)
    pm = builder.build(caller, scope, run_id, "pm")
    ref = pm.model_context["sources"][0]["ref"]
    report = builder.validate_output(pm, "report", report_for(ref))
    shared = builder.share_report(pm, supervisor, report)
    assert len(shared.model_context["sources"]) == 2
    data = {
        "status": "COMPLETE",
        "summary": "Synthetic assessment awaits review",
        "source_refs": [ref],
        "report_refs": ["report_pm"],
    }
    assert builder.validate_output(shared, "summary", data, report_refs=("report_pm",)).status == "COMPLETE"
    error("INVALID_REPORT_REFERENCE", lambda: builder.validate_output(shared, "summary", data))


def test_same_transaction_reauthorization_does_not_open_nested_tx(env, monkeypatch):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    material = {
        "profile_id": PROFILE_ID,
        "policy_revision": ANALYSIS_POLICY_HASH,
        "source_snapshot": builder.combined_snapshot(bundle),
    }
    with store._tx() as session:
        monkeypatch.setattr(store, "_tx", lambda: pytest.fail("Nested transaction opened"))
        builder.reauthorize_in_session(session, caller, scope, material)


def test_same_transaction_reauthorization_rejects_wrong_caller_and_policy(env):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    material = {
        "profile_id": PROFILE_ID,
        "policy_revision": ANALYSIS_POLICY_HASH,
        "source_snapshot": builder.combined_snapshot(bundle),
    }
    other = caller.model_copy(update={"subject_id": "other-user"})
    with store._tx() as session:
        error("SOURCE_CHANGED", lambda: builder.reauthorize_in_session(session, other, scope, material))
        error(
            "ANALYSIS_POLICY_CHANGED",
            lambda: builder.reauthorize_in_session(
                session, caller, scope, {**material, "policy_revision": "forged"}
            ),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "forged"),
        ("source_hash", "0" * 64),
        ("status", "EXPIRED"),
        ("classification", 3),
        ("ref", "src_" + "0" * 32),
    ],
)
def test_source_snapshot_cannot_forge_declared_citation_metadata(env, field, value):
    store, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    source_snapshot = builder.combined_snapshot(bundle)
    source_snapshot["bundles"][0]["sources"][1][field] = value
    material = {
        "profile_id": PROFILE_ID,
        "policy_revision": ANALYSIS_POLICY_HASH,
        "source_snapshot": source_snapshot,
    }
    with store._tx() as session:
        error("SOURCE_CHANGED", lambda: builder.reauthorize_in_session(session, caller, scope, material))


@pytest.mark.parametrize(
    "raw",
    [
        '{"requests":[],"requests":[]}',
        '{"requests":NaN}',
        '{"requests":Infinity}',
        '{"requests":1e9999}',
        "[]",
        "```json\n{}\n```",
        {"requests": float("nan")},
        {"requests": {1: "not-a-string-key"}},
        {"requests": (1, 2)},
        '{"requests":"\\ud800"}',
    ],
)
def test_strict_json_rejects_ambiguous_or_nonfinite_output(raw):
    error("INVALID_MODEL_OUTPUT", lambda: strict_json_object(raw))


@pytest.mark.parametrize("extra", ["scope", "role", "gate", "approval", "code", "actions"])
def test_model_output_cannot_create_authority(extra):
    error(
        "INVALID_MODEL_OUTPUT",
        lambda: parse_output(
            "plan",
            {
                "assignment": "pm",
                "task_question": "Summarize synthetic evidence",
                "source_refs": ["src_" + "a" * 32],
                extra: "forged-authority",
            },
        ),
    )


@pytest.mark.parametrize(
    "tool_request",
    [
        {"tool": "sandbox.execute", "query": "EVT", "top_k": 1},
        {"tool": "knowledge.search", "query": "EVT", "top_k": True},
        {"tool": "memory.search", "query": "EVT", "top_k": "1"},
        {"tool": "knowledge.search", "query": "EVT", "top_k": 6},
    ],
)
def test_search_proposal_structural_limits(tool_request):
    error("INVALID_MODEL_OUTPUT", lambda: parse_output("context", {"requests": [tool_request]}))


def test_nonblank_fields_and_source_snapshot_caller_sets_are_sorted(env):
    _, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    assert builder.snapshot(bundle)["caller"]["roles"] == sorted(caller.roles)
    error(
        "INVALID_MODEL_OUTPUT",
        lambda: parse_output(
            "plan",
            {"assignment": "pm", "task_question": "   ", "source_refs": [next(iter(bundle.citations))]},
        ),
    )


def test_outputs_bounded_refs_and_compact_schema_preserves_security(env):
    _, scope, caller, _, run_id, builder = env
    bundle = builder.build(caller, scope, run_id)
    ref = bundle.model_context["sources"][0]["ref"]
    error(
        "INVALID_CITATION",
        lambda: builder.validate_output(
            bundle,
            "plan",
            {"assignment": "pm", "task_question": "Readiness", "source_refs": ["src_" + "f" * 32]},
        ),
    )
    too_many = report_for(ref)
    too_many["facts"] *= 4
    error("INVALID_MODEL_OUTPUT", lambda: parse_output("report", too_many))
    data = report_for(ref)
    data["facts"][0]["text"] = "x" * 81
    error("INVALID_MODEL_OUTPUT", lambda: parse_output("report", data))
    schema = compact_schema("report")
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["CitedStatement"]["properties"]["text"]["maxLength"] == 80
    assert schema["$defs"]["CitedStatement"]["properties"]["source_refs"]["maxItems"] == 1
    assert schema["properties"]["facts"]["maxItems"] == 3
    assert all(
        schema["properties"][field]["maxItems"] == 1 for field in ("risks", "unknowns", "recommendations")
    )
    assert "title" not in json.dumps(schema)


def test_fixture_file_hash_and_origin_are_rechecked(env, tmp_path):
    store, scope, caller, _, run_id, _ = env
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    data = json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
    changed = copy.deepcopy(data)
    changed["metrics"]["boot_cycles"]["value"] = 101
    (fixtures / "aurora-pass.json").write_text(json.dumps(changed))
    builder = AnalysisContextBuilder(store, fixture_root=fixtures)
    error("SOURCE_CHANGED", lambda: builder.build(caller, scope, run_id))
    changed = {**data, "synthetic": False}
    (fixtures / "aurora-pass.json").write_text(json.dumps(changed))
    error("SOURCE_CHANGED", lambda: builder.build(caller, scope, run_id))
    assert hashlib.sha256(SOP_CONTENT.encode()).hexdigest() == SOP_HASH
