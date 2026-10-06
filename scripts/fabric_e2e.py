"""Explicit TEST_DRIVER approvals in an isolated project; no real EVT transition.

This harness performs actual local PostgreSQL/MCP/Docker checks and SIGKILLs
only its own transaction worker. It never invokes the model or a remote tracker.
"""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from forgeflow.contracts import DomainError, RunRequest, digest
from forgeflow.evt_review import EVTApproval
from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_runtime import initialize_fabric, load_fabric_identities, load_fabric_store
from forgeflow.fabric_store import FabricApproval, FabricItem, FabricOperations, FabricReceipt
from forgeflow.local import ROOT, SCOPE, load_identities, load_workflow
from forgeflow.store import Run, filters, view

DEST = ROOT / "deliveries/2026-10-06/s3-fabric/evidence"


def setup():
    source = load_fabric_store()
    service = FabricOperations(source)
    service.create_schema()
    principals = list(load_fabric_identities().values())
    owner = next(p for p in principals if "admin" in p.roles)
    reviewer = next(p for p in principals if "reviewer" in p.roles)
    executor = next(p for p in principals if p.kind == "service")
    return source, service, owner, reviewer, executor


def original(source):
    owner = next(p for p in load_identities().values() if "admin" in p.roles)
    with source._tx() as session:
        ids = session.scalars(select(Run.id).where(*filters(Run, SCOPE))).all()
        approvals = [view(row) for row in session.scalars(select(EVTApproval).where(*filters(EVTApproval, SCOPE)).order_by(EVTApproval.id))]
        project = view(source._project(session, SCOPE))
    return {"project": project, "parents": {identifier: digest({
        "run": source.get_run(owner, SCOPE, identifier), "events": source.events(owner, SCOPE, identifier)})
        for identifier in sorted(ids)}, "evt_approvals_hash": digest(approvals),
        "evt_approvals": [{"id": row["id"], "decision": row["decision"]} for row in approvals]}


def reject(code, action):
    try:
        action()
    except DomainError as error:
        if error.code != code:
            raise AssertionError((code, error.code)) from error
        return {"expected": code, "observed": error.code}
    raise AssertionError("Expected rejection: " + code)


def save(name, data):
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def checks():
    initialize_fabric()
    source, service, owner, _, executor = setup()
    if (DEST / "checks.json").exists():
        previous = json.loads((DEST / "checks.json").read_text())
        for case in previous["cases"].values():
            assert service.get(owner, FABRIC_SCOPE, case["review_id"])["report_hash"] == case["report_hash"]
        assert original(source) == previous["original_before"]
        print(json.dumps({"status": "EXISTING_CHECKS_VALIDATED", "new_sandbox_checks": 0, "new_model_calls": 0}))
        return
    before = original(source)
    result = {"provenance": "ACTUAL_LOCAL_EXECUTION", "approval_provenance": "TEST_DRIVER",
              "original_before": before, "cases": {}, "new_model_calls": 0, "external_writes": 0}
    for name in ("pass", "unknown"):
        request = RunRequest(scope=FABRIC_SCOPE, fixture_id="aurora-" + name)
        parent = source.submit(owner, request, "s3d-live-01-" + name)
        assert source.submit(owner, request, "s3d-live-01-" + name)["id"] == parent["id"]
        checked = asyncio.run(load_workflow(source).check(FABRIC_SCOPE, parent["id"]))
        review = service.create(owner, FABRIC_SCOPE, parent["id"], "s3d-review-01-" + name)
        assert not checked["approvals"] and not checked["actions"]
        assert service.get(owner, FABRIC_SCOPE, review["id"]) == review
        save("parent-" + name + ".json", {"run": checked, "events": source.events(executor, FABRIC_SCOPE, parent["id"]), "review": review})
        result["cases"][name] = {"run_id": parent["id"], "review_id": review["id"], "report_hash": review["report_hash"],
            "flow_gate": review["report_json"]["flow_gate"], "evt_gate": review["report_json"]["evt_gate"],
            "execution_status": checked["result_json"]["status"], "cleanup": checked["result_json"]["cleanup"],
            "mcp_protocol": checked["result_json"]["mcp"]["protocol_version"],
            "mcp_transport": checked["result_json"]["mcp"]["transport"],
            "scanner_engine_executed": checked["result_json"]["scan"]["engine_executed"]}
        assert review["report_json"]["flow_gate"] == name.upper()
        assert review["report_json"]["evt_gate"] == "UNKNOWN"
    result["original_after"] = original(source)
    assert result["original_after"] == before
    save("checks.json", result)
    print(json.dumps({"cases": result["cases"], "original_unchanged": True, "new_model_calls": 0}))


def effects():
    source, service, owner, reviewer, executor = setup()
    check = json.loads((DEST / "checks.json").read_text())
    normal, unknown = check["cases"]["pass"], check["cases"]["unknown"]
    review = service.get(owner, FABRIC_SCOPE, normal["review_id"])
    if (DEST / "effects.json").exists():
        previous = json.loads((DEST / "effects.json").read_text())
        for receipt in previous["receipts"].values():
            replay = service.execute(executor, FABRIC_SCOPE, receipt["approval_id"])
            assert replay["replayed"] and replay["receipt"] == receipt
        assert original(source) == check["original_before"]
        print(json.dumps({"status": "EXISTING_EFFECTS_REPLAYED", "receipts": len(previous["receipts"]), "new_effects": 0}))
        return
    unknown_review = service.get(owner, FABRIC_SCOPE, unknown["review_id"])
    failures = [reject("FABRIC_GATE_NOT_PASS", lambda kind=kind: service.propose(owner, FABRIC_SCOPE,
        unknown_review["id"], unknown_review["report_hash"], "s3d-deny-" + kind, kind))
        for kind in ("TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE")]
    old_owner = next(p for p in load_identities().values() if "admin" in p.roles)
    failures.append(reject("NOT_FOUND", lambda: service.get(old_owner, FABRIC_SCOPE, review["id"])))
    receipts, decisions, approvals = {}, {}, {}

    def approve(kind, target=None):
        approval = service.propose(owner, FABRIC_SCOPE, review["id"], review["report_hash"], "s3d-action-01-" + kind,
                                   kind, target_receipt_id=target)
        assert service.propose(owner, FABRIC_SCOPE, review["id"], review["report_hash"], "s3d-action-01-" + kind,
                               kind, target_receipt_id=target)["id"] == approval["id"]
        decision = service.decide(reviewer, FABRIC_SCOPE, approval["id"], approval["proposal_hash"], 0, "APPROVED")
        assert not decision["executed"]
        assert service.decide(reviewer, FABRIC_SCOPE, approval["id"], approval["proposal_hash"], 0, "APPROVED")["replayed"]
        approvals[kind], decisions[kind] = approval, decision
        return approval

    stage = approve("TEST_STAGE")
    # Own subprocess dies after SQL effects+receipt flush, before transaction commit.
    child = subprocess.run([sys.executable, str(Path(__file__)), "crash-worker", stage["id"]],
                           cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert child.returncode == -signal.SIGKILL, child.stderr
    fresh, recovered, _, _, recovered_executor = setup()
    with fresh._tx() as session:
        assert fresh._project(session, FABRIC_SCOPE).current_stage == "PROTOTYPE"
        assert not session.scalar(select(FabricReceipt).where(*filters(FabricReceipt, FABRIC_SCOPE), FabricReceipt.approval_id == stage["id"]))
    receipts["TEST_STAGE"] = recovered.execute(recovered_executor, FABRIC_SCOPE, stage["id"])["receipt"]
    assert service.execute(executor, FABRIC_SCOPE, stage["id"])["replayed"]
    for kind in ("ISSUE", "MEMORY_ACTIVATE"):
        approval = approve(kind)
        receipts[kind] = service.execute(executor, FABRIC_SCOPE, approval["id"])["receipt"]
        assert service.execute(executor, FABRIC_SCOPE, approval["id"])["receipt"] == receipts[kind]
        assert service.get(owner, FABRIC_SCOPE, review["id"]) == review
    active_memory = service.memory(reviewer, FABRIC_SCOPE, "Aurora")
    assert len(active_memory) == 1 and active_memory[0]["underlying_status"] == "CANDIDATE"
    for kind, original_kind in (("MEMORY_REVOKE", "MEMORY_ACTIVATE"), ("ISSUE_RETRACT", "ISSUE"), ("STAGE_REVERT", "TEST_STAGE")):
        approval = approve(kind, receipts[original_kind]["id"])
        receipts[kind] = service.execute(executor, FABRIC_SCOPE, approval["id"])["receipt"]
        assert service.execute(executor, FABRIC_SCOPE, approval["id"])["replayed"]
    assert not service.memory(reviewer, FABRIC_SCOPE, "Aurora")
    assert service.get(owner, FABRIC_SCOPE, review["id"]) == review
    with source._tx() as session:
        project = view(source._project(session, FABRIC_SCOPE))
        items = [view(row) for row in session.scalars(select(FabricItem).where(*filters(FabricItem, FABRIC_SCOPE)))]
        stored_approvals = [view(row) for row in session.scalars(select(FabricApproval).where(*filters(FabricApproval, FABRIC_SCOPE)))]
    assert project["current_stage"] == "PROTOTYPE" and project["stage_revision"] == 2
    assert original(source) == check["original_before"]
    output = {"provenance": "ACTUAL_LOCAL_POSTGRESQL", "approval_provenance": "TEST_DRIVER",
        "actual_user_business_approvals": 0, "new_model_calls": 0, "external_writes": 0,
        "decisions": decisions, "approvals": stored_approvals, "receipts": receipts, "failures": failures,
        "memory_active_before_revoke": active_memory, "items": items, "project_after": project,
        "recovery": {"signal": "SIGKILL", "child_exit_code": child.returncode, "effects_before_commit": "ROLLED_BACK",
                     "restarted_store_execution": "ONE_COMMITTED_RECEIPT", "after_commit_replay": "SAME_RECEIPT"},
        "original_unchanged": True, "technical_review_after_actions": review}
    save("effects.json", output)
    save("parent-after-effects.json", {"run": source.get_run(owner, FABRIC_SCOPE, normal["run_id"]),
                                      "events": source.events(owner, FABRIC_SCOPE, normal["run_id"])})
    print(json.dumps({"receipts": len(receipts), "fixture_decisions": len(decisions), "project": project,
                      "recovery": output["recovery"], "original_unchanged": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["checks", "effects", "crash-worker"])
    parser.add_argument("approval_id", nargs="?")
    args = parser.parse_args()
    if args.command == "checks":
        checks()
    elif args.command == "effects":
        effects()
    else:
        _, service, _, _, executor = setup()
        service.execute(executor, FABRIC_SCOPE, args.approval_id, _before_commit=lambda: os.kill(os.getpid(), signal.SIGKILL))


if __name__ == "__main__":
    main()
