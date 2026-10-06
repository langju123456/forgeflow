"""S2C statusless selections and source checks; all execution is TEST_DRIVER."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_grounded_context import environment as old_environment

from forgeflow.analysis_context import ANALYSIS_POLICY_HASH, PROFILE_ID, SOP_HASH
from forgeflow.context import MemoryService
from forgeflow.contracts import DomainError, Principal, Scope, canonical, digest
from forgeflow.gate import RULE_HASH, evaluate
from forgeflow.grounded_context import FrozenCaseExportGrant, catalog
from forgeflow.policy import POLICY_HASH
from forgeflow.s2c_checks import EVALUATION_CLOCK, S2CInputProvider, S2CRequest, S2CTrustedInput
from forgeflow.s2c_context import STATUS_AUTHORITY, S2CContextBuilder
from forgeflow.s2c_contracts import derive_status, project
from forgeflow.store import Document, Run, aware, filters
from forgeflow.templates import template_hash


@pytest.fixture
def env(tmp_path):
    generator = old_environment.__wrapped__(tmp_path)
    values = next(generator)
    yield values
    generator.close()


@pytest.fixture
def derived(env):
    """Commit a labeled TEST_DRIVER artifact through the real new input store."""
    store, scope, caller, worker, _ = env
    provider = S2CInputProvider(store)
    provider.create_schema()
    run = provider.submit(caller, S2CRequest(scope=scope), "s2c-derived-context-driver")
    claim = store.claim(worker, scope, run["id"], "TEST_DRIVER")
    loaded = provider.load_run(worker, scope, run["id"])
    manifest, witness = loaded["manifest"], loaded["witness"]
    content = canonical({
        "schema_version": "evt-check-v1", "manifest": manifest,
        "runtime": {"uid": 10001, "gid": 10001, "seccomp": "2"},
    })
    result = {
        "handle": "TEST_DRIVER", "status": "SUCCEEDED", "cleanup": "REMOVED", "exit_code": 0,
        "synthetic": True, "orchestration": "TEST_DRIVER",
        "artifacts": [{"path": "result.json", "content": content,
                       "sha256": hashlib.sha256(content.encode()).hexdigest(), "size": len(content.encode())}],
        "scan": {"status": "PASS", "source_hash": template_hash("evt-check"), "provenance": "TEST_DRIVER"},
        "mcp": {"status": "TEST_DRIVER"}, "trusted_input": witness,
        "evaluation_clock": EVALUATION_CLOCK, "skill": {"sha256": witness["skill_hash"]},
    }
    snapshot = {
        "input_hash": digest(manifest), "input_manifest_hash": digest(manifest), "fixture_version": "v1",
        "classification": 0, "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH,
        "code_hash": template_hash("evt-check"), "skill_hash": witness["skill_hash"],
        "documents": [{"id": "aurora-evt-sop", "version": "v1", "source_hash": SOP_HASH}],
        "valid_until": "2026-11-05T00:00:00Z", "trusted_input": witness, "evaluation_clock": EVALUATION_CLOCK,
    }
    provider.finish(
        worker, scope, run["id"], claim["token"], claim["epoch"], result,
        evaluate(manifest, result, now=aware(EVALUATION_CLOCK)), snapshot,
    )
    builder = S2CContextBuilder(store, "M03", input_provider=provider)
    bundle = builder.build(caller, scope, run["id"])
    return builder, bundle, provider


def fail(code, operation):
    with pytest.raises(DomainError) as error:
        operation()
    assert error.value.code == code


def setup(env, scenario="M01", *, export_grant=None):
    store, scope, caller, _, completed = env
    _, run_id, _ = completed(scenario)
    builder = S2CContextBuilder(store, scenario, export_grant=export_grant)
    bundle = builder.build(caller, scope, run_id)
    return builder, bundle


def selection(bundle, *, phase="report"):
    claims = catalog(bundle)
    data = {
        "observations": [item["id"] for item in claims["observations"]],
        "concerns": [item["id"] for item in claims["concerns"]],
    }
    if phase == "report":
        data["recommendation"] = "HUMAN_REVIEW_REQUIRED"
    else:
        data.update(report_refs=["report_pm"], human_approval_required=True)
    return data


@pytest.mark.parametrize("scenario,expected", [
    ("M01", "COMPLETE"), ("M01-blocked", "INCOMPLETE"),
    ("M02", "INCOMPLETE"), ("M04", "COMPLETE"),
])
def test_raw_selection_has_no_status_and_internal_projection_is_server_derived(env, scenario, expected):
    store, scope, caller, _, _ = env
    builder, bundle = setup(env, scenario)
    parent_before = store.get_run(caller, scope, bundle.run_id)
    raw = selection(bundle)
    unchanged = copy.deepcopy(raw)
    parsed = builder.validate_output(bundle, "report", raw)
    assert "status" not in parsed.model_dump(mode="json")
    assert raw == unchanged
    assert builder.expected_status(bundle) == expected
    assert project("report", parsed, catalog(bundle)).status == expected
    material = builder.snapshot(bundle)
    assert material["grounding"]["report_status_authority"] == STATUS_AUTHORITY
    assert material["grounding"]["input_contract"] == "UNCHANGED_CANONICAL_FIXTURE_CHECK"
    pm = builder.build(caller, scope, bundle.run_id, "pm")
    shared = builder.share_report(pm, bundle, builder.validate_output(pm, "report", selection(pm)))
    assert shared.model_context["reports"][0]["status"] == expected
    assert raw == unchanged
    summary = selection(shared, phase="summary")
    validated = builder.validate_output(shared, "summary", summary, ("report_pm",))
    assert "status" not in validated.model_dump(mode="json")
    assert store.get_run(caller, scope, bundle.run_id) == parent_before


@pytest.mark.parametrize("phase", ["report", "summary"])
def test_model_cannot_submit_completion_status_even_when_correct(env, phase):
    builder, bundle = setup(env)
    raw = selection(bundle, phase=phase)
    raw["status"] = "COMPLETE"
    fail("INVALID_MODEL_OUTPUT", lambda: builder.validate_output(bundle, phase, raw, ("report_pm",)))
    assert builder.semantic_check(bundle, phase, raw) == {
        "structural_valid": False, "semantic_valid": None, "codes": ["INVALID_MODEL_OUTPUT"],
    }


def test_raw_validation_rejects_internal_projection_as_provider_selection(env):
    _, scope, caller, _, _ = env
    builder, supervisor = setup(env)
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    projected = project("report", selection(pm), catalog(pm))
    fail("INVALID_MODEL_OUTPUT", lambda: builder.validate_output(pm, "report", projected.model_dump(mode="json")))
    fail("INVALID_MODEL_OUTPUT", lambda: builder.share_report(pm, supervisor, projected.model_dump(mode="json")))


def test_share_report_accepts_explicit_internal_projection_and_rederives_status(env):
    _, scope, caller, _, _ = env
    builder, supervisor = setup(env, "M02")
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    projected = project("report", selection(pm), catalog(pm))
    shared = builder.share_report(pm, supervisor, projected)
    assert shared.model_context["reports"][0]["status"] == "INCOMPLETE"
    changed = projected.model_copy(update={"status": "COMPLETE"})
    fail("SERVER_PROJECTION_CHANGED", lambda: builder.share_report(pm, supervisor, changed))


def test_validated_internal_handoff_is_derived_and_rehash_forgery_rejected(env):
    _, scope, caller, _, _ = env
    builder, supervisor = setup(env, "M02")
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    report = builder.validate_output(pm, "report", selection(pm))
    shared = builder.share_report(pm, supervisor, report)
    builder.revalidate(shared)
    context = copy.deepcopy(shared.model_context)
    context["reports"][0]["status"] = "COMPLETE"
    forged = replace(shared, model_context=context, context_hash=digest(context))
    with pytest.raises(DomainError):
        builder.revalidate(forged)


def test_extra_status_authority_inside_model_report_is_forbidden(env):
    builder, bundle = setup(env)
    raw = selection(bundle)
    raw["status_authority"] = STATUS_AUTHORITY
    fail("INVALID_MODEL_OUTPUT", lambda: builder.validate_output(bundle, "report", raw))


def test_fake_unknown_selection_retains_structural_vs_semantic_distinction(env):
    builder, bundle = setup(env)
    raw = selection(bundle)
    raw["concerns"] = ["c_" + "a" * 16]
    checked = builder.semantic_check(bundle, "report", raw)
    assert checked == {
        "structural_valid": True, "semantic_valid": False, "codes": ["EVIDENCE_COVERAGE_CONCERNS"],
    }
    fail("UNGROUNDED_MODEL_OUTPUT", lambda: builder.validate_output(bundle, "report", raw))


def test_authorized_candidate_memory_does_not_become_authoritative_check(env):
    store, scope, caller, worker, _ = env
    builder, supervisor = setup(env)
    completed = next(event for event in store.events(caller, scope, supervisor.run_id) if event["kind"] == "CHECK_COMPLETED")
    MemoryService(store).propose(
        worker, scope, supervisor.run_id, "Aurora EVT", "Aurora EVT awaits human review", [completed["id"]]
    )
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    proposal = builder.validate_output(pm, "context", {"requests": [
        {"tool": "knowledge.search", "query": "Aurora EVT", "top_k": 1},
        {"tool": "memory.search", "query": "Aurora EVT", "top_k": 1},
    ]})
    expanded = builder.search(pm, proposal)
    assert catalog(expanded) == catalog(pm)
    assert any(source["kind"] == "memory" and source["status"] == "CANDIDATE" for source in expanded.model_context["sources"])
    shared = builder.share_report(expanded, supervisor, builder.validate_output(expanded, "report", selection(expanded)))
    assert shared.model_context["reports"][0]["status"] == "COMPLETE"
    builder.revalidate(shared)


def test_source_acl_revocation_invalidates_statusless_selection(env):
    store, scope, _, _, _ = env
    builder, bundle = setup(env)
    with Session(store.engine) as session, session.begin():
        document = session.scalar(select(Document).where(*filters(Document, scope), Document.id == "aurora-evt-sop"))
        document.roles = ["hardware"]
    fail("NOT_FOUND", lambda: builder.validate_output(bundle, "report", selection(bundle)))


def test_whole_pm_context_must_be_authorized_for_supervisor(env):
    store, scope, caller, _, _ = env
    _, run_id, _ = env[-1]("M04")
    grant = FrozenCaseExportGrant.for_case("M04")
    grant = replace(grant, sources=tuple(replace(source, roles=("pm",)) for source in grant.sources))
    builder = S2CContextBuilder(store, "M04", export_grant=grant)
    pm = builder.build(caller, scope, run_id, "pm")
    fail("NOT_FOUND", lambda: builder.revalidate(pm, recipient_role="supervisor"))


def test_cross_tenant_context_does_not_inherit_derived_status(env):
    _, scope, caller, _, _ = env
    builder, bundle = setup(env)
    other = Scope(tenant_id="other", workspace_id=scope.workspace_id, project_id=scope.project_id)
    fail("NOT_FOUND", lambda: builder.build(caller, other, bundle.run_id))
    stranger = Principal(subject_id="other", kind="user", roles=frozenset({"pm"}), scopes=(other,))
    fail("NOT_FOUND", lambda: builder.build(stranger, scope, bundle.run_id))


def test_server_derived_status_catalog_is_bound_in_publication_transaction(env):
    store, scope, caller, _, _ = env
    builder, bundle = setup(env, "M02")
    material = {
        "policy_revision": ANALYSIS_POLICY_HASH, "profile_id": PROFILE_ID,
        "source_snapshot": builder.snapshot(bundle), "claim_catalog_hash": digest(catalog(bundle)),
    }
    assert derive_status(catalog(bundle)) == "INCOMPLETE"
    with store._tx() as session:
        builder.reauthorize_in_session(session, caller, scope, material)
    mutated = copy.deepcopy(material)
    mutated["source_snapshot"]["grounding"]["report_status_authority"] = "MODEL_SUPPLIED"
    with store._tx() as session:
        fail("GROUNDING_CONTEXT_CHANGED", lambda: builder.reauthorize_in_session(session, caller, scope, mutated))


def test_m03_has_actual_unknown_check_and_all_claims_cite_its_run(derived, env):
    builder, bundle, provider = derived
    store, scope, caller, _, _ = env
    run = store.get_run(caller, scope, bundle.run_id)
    hardware = next(source for source in bundle.model_context["sources"] if source["kind"] == "hardware_check")
    assert hardware["gate"] == run["assessment_json"]["status"] == "UNKNOWN"
    assert hardware["mcp"] == "TEST_DRIVER"
    assert run["request_json"]["fixture_id"] == "aurora-frozen-derived"
    assert run["snapshot_json"]["trusted_input"] == run["request_json"]["trusted_input"]
    assert run["snapshot_json"]["input_hash"] == digest(provider.load_run(caller, scope, bundle.run_id)["manifest"])
    observations = catalog(bundle)["observations"]
    assert len(observations) == 5
    assert {item["authority"] for item in observations} == {"HARDWARE_CHECK"}
    assert {item["source_ref"] for item in observations} == {hardware["ref"]}
    concerns = catalog(bundle)["concerns"]
    assert {item["kind"] for item in concerns} == {"thermal_peak_c:CONFLICT", "ci_smoke:EXPIRED"}
    assert {item["source_ref"] for item in concerns} == {hardware["ref"]}
    assert builder.expected_status(bundle) == "INCOMPLETE"
    assert builder.snapshot(bundle)["grounding"]["input_contract"] == "CONFIRMED_DERIVED_INPUT_CHECK"
    assert run["project"]["current_stage"] == "PROTOTYPE"
    assert run["receipts"] == []
    assert all(approval["decision"] == "PENDING" for approval in run["approvals"])


def test_derived_expired_and_conflicted_inputs_are_visible_without_becoming_fresh(derived):
    builder, bundle, _ = derived
    observations = {item["metric"]: item for item in catalog(bundle)["observations"]}
    assert observations["ci_smoke"]["value"] is True
    assert observations["ci_smoke"]["check_state"] == "UNKNOWN"
    assert observations["ci_smoke"]["evidence_state"] == "EXPIRED"
    assert observations["thermal_peak_c"]["value"] == 68
    assert observations["thermal_peak_c"]["check_state"] == "UNKNOWN"
    assert observations["thermal_peak_c"]["evidence_state"] == "CONFLICT"
    parsed = builder.validate_output(bundle, "report", selection(bundle))
    assert "status" not in parsed.model_dump(mode="json")
    assert project("report", parsed, catalog(bundle)).status == "INCOMPLETE"


def test_derived_m03_handoff_accepts_statusless_and_internal_projection(derived, env):
    builder, supervisor, _ = derived
    _, scope, caller, _, _ = env
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    raw = selection(pm)
    projection = project("report", builder.validate_output(pm, "report", raw), catalog(pm))
    shared = builder.share_report(pm, supervisor, projection)
    assert shared.model_context["reports"][0]["status"] == "INCOMPLETE"
    assert "status" not in raw
    parsed = builder.validate_output(shared, "summary", selection(shared, phase="summary"), ("report_pm",))
    assert "status" not in parsed.model_dump(mode="json")
    builder.revalidate(shared)


def test_derived_snapshot_reauthorizes_inside_supplied_store_transaction(derived, env):
    builder, bundle, _ = derived
    store, scope, caller, _, _ = env
    material = {
        "policy_revision": ANALYSIS_POLICY_HASH, "profile_id": PROFILE_ID,
        "source_snapshot": builder.snapshot(bundle), "claim_catalog_hash": digest(catalog(bundle)),
    }
    with store._tx() as session:
        builder.reauthorize_in_session(session, caller, scope, material)


@pytest.mark.parametrize("requests", [
    [], [{"tool": "knowledge.search", "query": "Aurora EVT", "top_k": 1}],
])
def test_derived_search_uses_same_confirmed_input_verifier(derived, env, requests):
    builder, supervisor, _ = derived
    _, scope, caller, _, _ = env
    pm = builder.build(caller, scope, supervisor.run_id, "pm")
    proposal = builder.validate_output(pm, "context", {"requests": requests})
    expanded = builder.search(pm, proposal)
    assert catalog(expanded) == catalog(pm)
    assert len(expanded.tool_receipts) == len(requests)
    if requests:
        receipt = expanded.tool_receipts[0]
        assert receipt["tool"] == "knowledge.search" and receipt["transport"] == "IN_PROCESS"
        assert receipt["result_count"] == 1
    shared = builder.share_report(expanded, supervisor, builder.validate_output(expanded, "report", selection(expanded)))
    assert shared.model_context["reports"][0]["status"] == "INCOMPLETE"
    builder.revalidate(shared)


def test_canonical_pass_cannot_be_claimed_as_executed_derived_m03(env):
    store, scope, caller, _, completed = env
    _, run_id, _ = completed("M03")
    provider = S2CInputProvider(store)
    provider.create_schema()
    builder = S2CContextBuilder(store, "M03", input_provider=provider)
    fail("DERIVED_CHECK_REQUIRED", lambda: builder.build(caller, scope, run_id))


def test_unexecuted_derived_input_cannot_become_a_hardware_source(env):
    store, scope, caller, _, _ = env
    provider = S2CInputProvider(store)
    provider.create_schema()
    run = provider.submit(caller, S2CRequest(scope=scope), "not-executed")
    builder = S2CContextBuilder(store, "M03", input_provider=provider)
    fail("CHECK_NOT_COMPLETED", lambda: builder.build(caller, scope, run["id"]))


def test_derived_manifest_mutation_cannot_be_reauthorized_after_rehash(derived, env):
    builder, bundle, provider = derived
    store, scope, caller, _, _ = env
    with Session(store.engine) as session, session.begin():
        row = session.scalar(select(S2CTrustedInput).where(*filters(S2CTrustedInput, scope), S2CTrustedInput.run_id == bundle.run_id))
        manifest = copy.deepcopy(row.manifest_json)
        manifest["metrics"]["thermal_peak_c"]["value"] = 1
        row.manifest_json, row.manifest_hash = manifest, digest(manifest)
        row.payload_hash = digest(provider._binding(scope, bundle.run_id, row.request_hash, manifest, row.witness_json))
    fail("DERIVED_INPUT_CHANGED", lambda: builder.revalidate(bundle))
    fail("DERIVED_INPUT_CHANGED", lambda: builder.build(caller, scope, bundle.run_id))


def test_derived_result_artifact_corruption_is_rejected(derived, env):
    builder, bundle, _ = derived
    store, scope, _, _, _ = env
    with Session(store.engine) as session, session.begin():
        row = session.scalar(select(Run).where(*filters(Run, scope), Run.id == bundle.run_id))
        result = copy.deepcopy(row.result_json)
        result["artifacts"][0]["content"] = "{}"
        row.result_json = result
    fail("DERIVED_CHECK_ARTIFACT_INVALID", lambda: builder.revalidate(bundle))


def test_derived_source_acl_revocation_precedes_model_projection(derived, env):
    builder, bundle, _ = derived
    store, scope, _, _, _ = env
    with Session(store.engine) as session, session.begin():
        document = session.scalar(select(Document).where(*filters(Document, scope), Document.id == "aurora-evt-sop"))
        document.roles = ["hardware"]
    fail("NOT_FOUND", lambda: builder.validate_output(bundle, "report", selection(bundle)))
