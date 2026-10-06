"""Offline derived scope bindings verify Fabric contracts, never a new execution.

The M01 archive stays read-only. ``DERIVED_OFFLINE_SCOPE_REBINDING`` describes
the test fixture transformation outside the run and its recorded receipts.
"""

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from forgeflow.contracts import DomainError, Scope, digest
from forgeflow.fabric_contract import (
    FABRIC_SCOPE,
    POLICY,
    POLICY_HASH,
    REVISION,
    FabricSources,
    evaluate_fabric,
    load_sources,
)
from forgeflow.local import SCOPE as LEGACY_SCOPE

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01/raw-chains/M01.json"
FIXTURE_ORIGIN = {
    "kind": "DERIVED_OFFLINE_SCOPE_REBINDING",
    "archive": str(ARCHIVE.relative_to(ROOT)),
    "new_execution_performed": False,
}


def _rechain(run, events):
    """Explicitly rebuild the derived event chain using the actual Fabric scope."""
    previous = "0" * 64
    for sequence, event in enumerate(events, 1):
        event["sequence"], event["prev_hash"] = sequence, previous
        event["payload_hash"] = digest(event["payload"])
        event["event_hash"] = digest({
            "scope": FABRIC_SCOPE.model_dump(mode="json"),
            "run_id": run["id"],
            **{key: event[key] for key in (
                "step_id", "sequence", "kind", "actor", "payload_hash",
                "prev_hash", "classification", "created_at",
            )},
        })
        previous = event["event_hash"]


def refresh_bindings(run, events):
    """Bind deliberate test mutations without pretending to rerun the checker."""
    run["request_hash"] = digest(run["request_json"])
    run["snapshot_hash"] = digest(run["snapshot_json"])
    result_hash = digest(run["result_json"])
    for step in run["steps"]:
        if step["step_key"] == "readiness":
            step["output_hash"] = result_hash
    approvals = {}
    for approval in run["approvals"]:
        approval["proposal_hash"] = digest(approval["proposal"])
        approval["snapshot_hash"] = digest(approval["proposal"]["snapshot"])
        approvals[approval["id"]] = approval
    for event in events:
        if event["kind"] == "RUN_SUBMITTED":
            event["payload"]["request_hash"] = run["request_hash"]
        elif event["kind"] == "CHECK_COMPLETED":
            event["payload"].update(
                result_hash=result_hash,
                assessment_hash=digest(run["assessment_json"]),
                status=run["assessment_json"].get("status", "UNKNOWN"),
            )
        elif event["kind"] == "APPROVAL_PREPARED":
            approval = approvals[event["payload"]["approval_id"]]
            event["payload"]["proposal_hash"] = approval["proposal_hash"]
    _rechain(run, events)


def offline_parent():
    """Create a derived in-memory fixture and leave both old PENDING rows intact."""
    archive = json.loads(ARCHIVE.read_text())
    run, events = copy.deepcopy((archive["parent_after"], archive["parent_events"]))
    original_scope = LEGACY_SCOPE.model_dump(mode="json")
    fabric_scope = FABRIC_SCOPE.model_dump(mode="json")

    def rebind(value):
        if isinstance(value, dict):
            if all(value.get(key) == item for key, item in original_scope.items()):
                value.update(fabric_scope)
            for child in value.values():
                rebind(child)
        elif isinstance(value, list):
            for child in value:
                rebind(child)

    rebind(run)
    rebind(events)
    # Derived synthetic fixtures use the archived completion clock, so the
    # bounded BOM observation remains deterministic after its real expiry.
    completion = next(event for event in events if event["kind"] == "CHECK_COMPLETED")
    run["snapshot_json"]["evaluation_clock"] = completion["created_at"]
    for approval in run["approvals"]:
        approval["proposal"]["snapshot"] = copy.deepcopy(run["snapshot_json"])
    refresh_bindings(run, events)
    return run, events


def evaluate(fixture_id="aligned", *, run=None, events=None, source_config=None):
    if run is None:
        run, events = offline_parent()
    return evaluate_fabric(FABRIC_SCOPE, run, events, fixture_id, source_config=source_config)


def checks(report):
    return {row["id"]: row for row in report["checks"]}


def test_bound_recorded_flow_passes_without_evt_or_business_authority():
    report = evaluate()
    assert report["revision"] == REVISION and report["scope"] == FABRIC_SCOPE.model_dump(mode="json")
    assert report["flow_gate"] == "PASS" and report["evt_gate"] == "UNKNOWN"
    assert report["architect"]["static_status"] == report["android"]["static_status"] == "PASS"
    assert report["sre"]["operational_readiness"] == "READY"
    assert report["policy_id"] == POLICY["id"] and report["policy_hash"] == POLICY_HASH
    assert report["human_approval_required"] is True
    assert report["local_test_stage_allowed"] is True
    assert report["full_evt_eligibility"] is False and report["automatic_business_actions"] is False
    assert report["android"]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    assert report["android"]["device_status"] == "NOT_RUN_NO_ANDROID_DEVICE"
    assert FIXTURE_ORIGIN["kind"] == "DERIVED_OFFLINE_SCOPE_REBINDING"
    assert FIXTURE_ORIGIN["new_execution_performed"] is False


def test_later_human_audit_and_project_changes_preserve_technical_binding():
    run, events = offline_parent()
    first = evaluate(run=run, events=events)
    run["revision"] += 1
    run["state"] = "COMPLETED"
    run["project"].update(current_stage="TEST_STAGE", stage_revision=1)
    run["approvals"][0].update(decision="APPROVED", reviewer="offline-human-reviewer")
    run["receipts"] = [{"id": "derived-test-stage-receipt", "kind": "TEST_STAGE", "simulation": True}]
    run["actions"] = [{"id": "derived-local-action", "kind": "TEST_STAGE",
                       "status": "COMPLETED", "simulation": True}]
    dispatch_step = copy.deepcopy(run["steps"][0])
    dispatch_step.update(id="derived-local-dispatch-step", step_key="fabric-dispatch")
    run["steps"].append(dispatch_step)
    event = copy.deepcopy(events[-1])
    event.update(id="derived-human-audit", step_id=None, kind="APPROVAL_DECIDED",
                 actor="offline-human-reviewer", created_at="2026-10-06T07:08:00+00:00",
                 payload={"approval_id": run["approvals"][0]["id"], "decision": "APPROVED"})
    events.append(event)
    _rechain(run, events)
    second = evaluate(run=run, events=events)
    assert second["audit_chain_valid"] is True
    assert second["technical_hash"] == first["technical_hash"]
    assert second["flow_gate"] == first["flow_gate"] == "PASS"
    assert second["evt_gate"] == "UNKNOWN" and second["automatic_business_actions"] is False
    assert second == first


@pytest.mark.parametrize("foreign_scope", [
    LEGACY_SCOPE,
    Scope(tenant_id="foreign", workspace_id=FABRIC_SCOPE.workspace_id,
          project_id=FABRIC_SCOPE.project_id),
])
def test_fixed_fabric_scope_cannot_be_replaced_by_legacy_or_foreign_scope(foreign_scope):
    run, events = offline_parent()
    with pytest.raises(DomainError, match="NOT_FOUND"):
        evaluate_fabric(foreign_scope, run, events)
    run["project_id"] = "another-project"
    with pytest.raises(DomainError, match="NOT_FOUND"):
        evaluate(run=run, events=events)


@pytest.mark.parametrize("fixture_id", ["bom-mismatch", "missing-exported"])
def test_controlled_xml_contradictions_block_flow_and_evt(fixture_id):
    report = evaluate(fixture_id)
    assert report["flow_gate"] == report["evt_gate"] == "BLOCKED"
    assert report["local_test_stage_allowed"] is False
    assert report["architect"]["static_status"] == report["android"]["static_status"] == "BLOCKED"
    assert report["android"]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    assert report["automatic_business_actions"] is False


@pytest.mark.parametrize("status", [None, "UNKNOWN", "unrecognized"])
def test_unresolved_hardware_assessment_cannot_be_promoted_to_pass(status):
    run, events = offline_parent()
    run["assessment_json"]["status"] = status
    refresh_bindings(run, events)
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == report["evt_gate"] == "UNKNOWN"
    assert report["local_test_stage_allowed"] is False
    assert report["full_evt_eligibility"] is False


def test_test_driver_mcp_receipt_remains_unknown_after_valid_hash_rebinding():
    run, events = offline_parent()
    run["result_json"]["mcp"] = {"status": "TEST_DRIVER"}
    refresh_bindings(run, events)
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == report["evt_gate"] == "UNKNOWN"
    assert report["local_test_stage_allowed"] is False
    assert report["sre"]["operational_readiness"] == "UNKNOWN"


def test_snapshot_content_requires_its_current_exact_hash():
    run, events = offline_parent()
    run["snapshot_hash"] = "0" * 64
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == report["evt_gate"] == "BLOCKED"
    assert report["local_test_stage_allowed"] is False


@pytest.mark.parametrize("target", ["audit_payload", "audit_row_scope", "artifact_content", "artifact_size"])
def test_observed_audit_or_artifact_tampering_blocks_flow(target):
    run, events = offline_parent()
    if target == "audit_payload":
        events[1]["payload"]["attempt"] += 1
    elif target == "audit_row_scope":
        events[1]["project_id"] = "another-project"
    elif target == "artifact_content":
        run["artifacts"][0]["content"] += " "
    else:
        run["artifacts"][0]["size"] += 1
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == report["evt_gate"] == "BLOCKED"
    assert report["local_test_stage_allowed"] is False


def test_caller_success_prose_does_not_grant_apk_or_device_evidence():
    run, events = offline_parent()
    run["android"] = {"build_status": "SUCCEEDED", "apk_status": "PASS", "device_status": "PASS"}
    run["result_json"].update(apk_status="PASS", device_status="PASS", build_status="SUCCEEDED")
    refresh_bindings(run, events)
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == "PASS" and report["evt_gate"] == "UNKNOWN"
    assert report["local_test_stage_allowed"] is True
    assert report["android"]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    assert report["android"]["device_status"] == "NOT_RUN_NO_ANDROID_DEVICE"
    assert report["full_evt_eligibility"] is False


@pytest.mark.parametrize("material", ["rule_hash", "skill_hash", "policy_hash", "code_hash"])
def test_rebound_stale_execution_material_still_fails_current_sources(material):
    run, events = offline_parent()
    run["snapshot_json"][material] = "0" * 64
    refresh_bindings(run, events)
    report = evaluate(run=run, events=events)
    assert report["flow_gate"] == report["evt_gate"] == "BLOCKED"
    assert report["local_test_stage_allowed"] is False
    assert checks(report)["current_execution_sources"]["status"] == "FAIL"


def test_exact_document_source_version_changes_technical_binding():
    run, events = offline_parent()
    first = evaluate(run=run, events=events)
    run["snapshot_json"]["documents"][0]["version"] = "v999"
    refresh_bindings(run, events)
    second = evaluate(run=run, events=events)
    # The pure contract has no current document database. Its exact version
    # binding changes, so an approval for the previous technical hash is stale.
    assert second["technical_hash"] != first["technical_hash"]
    assert second["technical_binding"] != first["technical_binding"]
    assert second["flow_gate"] == "PASS" and second["evt_gate"] == "UNKNOWN"


def test_controlled_source_byte_changes_rebind_technical_hash(tmp_path):
    xml_root = tmp_path / "android-xml"
    shutil.copytree(ROOT / "fixtures/aurora-domain", xml_root)
    configured = FabricSources(xml_root=xml_root)
    first = evaluate(source_config=configured)
    path = xml_root / "aligned.xml"
    path.write_bytes(path.read_bytes() + b"\n")
    second = evaluate(source_config=configured)
    assert second["technical_hash"] != first["technical_hash"]
    assert first["flow_gate"] == second["flow_gate"] == "PASS"
    policy_bytes = (ROOT / "fixtures/fabric/policy.json").read_bytes()
    assert hashlib.sha256(policy_bytes).hexdigest() == POLICY_HASH
    assert load_sources()["policy_hash"] == POLICY_HASH
    policy_root = tmp_path / "fabric-policy"
    shutil.copytree(ROOT / "fixtures/fabric", policy_root)
    policy_config = FabricSources(policy_root=policy_root)
    first_policy_report = evaluate(source_config=policy_config)
    policy_path = policy_root / "policy.json"
    changed_policy = json.loads(policy_path.read_text())
    changed_policy["limitations"] += " Test-only changed policy wording."
    policy_path.write_text(json.dumps(changed_policy))
    second_policy_report = evaluate(source_config=policy_config)
    assert second_policy_report["technical_hash"] != first_policy_report["technical_hash"]
    assert second_policy_report["policy_hash"] != first_policy_report["policy_hash"]
    assert second_policy_report["flow_gate"] == "PASS" and second_policy_report["evt_gate"] == "UNKNOWN"
    assert second_policy_report["evt_stage_transition_allowed"] is False
    assert second_policy_report["test_stage"] == {
        "from_stage": "PROTOTYPE", "to_stage": "TEST_STAGE", "simulation": True,
    }
    for override in ("enable_evt", "target_evt", "integer_simulation"):
        forged_policy = copy.deepcopy(changed_policy)
        if override == "enable_evt":
            forged_policy["evt_stage_transition_allowed"] = True
        elif override == "target_evt":
            forged_policy["test_stage"]["to_stage"] = "EVT"
        else:
            forged_policy["test_stage"]["simulation"] = 1
        policy_path.write_text(json.dumps(forged_policy))
        with pytest.raises(DomainError, match="INVALID_FABRIC_POLICY"):
            evaluate(source_config=policy_config)


def test_evaluation_is_pure_bounded_and_has_no_caller_source_dictionary_grant():
    run, events = offline_parent()
    before = copy.deepcopy((run, events))
    archive_before = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    first = evaluate(run=run, events=events)
    assert first == evaluate(run=run, events=events)
    assert (run, events) == before
    assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == archive_before
    archived_approvals = json.loads(ARCHIVE.read_text())["parent_after"]["approvals"]
    assert len(run["approvals"]) == len(archived_approvals) == 2
    assert {row["id"] for row in run["approvals"]} == {row["id"] for row in archived_approvals}
    assert all(row["decision"] == "PENDING" and row["consumed_by_action"] is None
               for row in run["approvals"])
    assert "evidence_origin" not in run
    with pytest.raises(DomainError, match="INVALID_FABRIC_FIXTURE"):
        evaluate("../aligned", run=run, events=events)
    with pytest.raises((DomainError, TypeError)):
        evaluate(run=run, events=events, source_config=load_sources())
