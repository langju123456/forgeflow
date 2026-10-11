#!/usr/bin/env python3
"""Representative chain on a database COPY (name starts with forgeflow_chain_); never on the live database.

    FORGEFLOW_DATABASE_URL=<copy> .venv/bin/python scripts/representative_chain.py start --key K --out DIR [--analysis]
    ... fabric_cli.py explain APPROVAL   (read only)         <- a person looks
    ... fabric_cli.py decide APPROVAL HASH REVISION --decision APPROVED|REJECTED   <- a person decides
    ... fabric_cli.py execute APPROVAL
    ... representative_chain.py crash-execute APPROVAL --out DIR   (killed before commit, recovered, replayed)
    ... representative_chain.py verify --state DIR/state-K.json --out DIR

start submits a run, runs the real Docker check over MCP, optionally the local model analysis, the Gate review
and the proposals, then stops. It never decides an approval and never executes an ordinary one.
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

from forgeflow import chain
from forgeflow.contracts import DomainError
from forgeflow.fabric_contract import FABRIC_SCOPE
from forgeflow.fabric_store import FabricReceipt
from forgeflow.local import SCOPE, database_url, load_workflow
from forgeflow.store import filters


def runtime():
    chain.require_copy_database(database_url())  # before anything touches the database
    from forgeflow.fabric_runtime import load_fabric_identities, load_fabric_store
    from forgeflow.fabric_store import FabricOperations

    source = load_fabric_store()
    service = FabricOperations(source)
    service.create_schema()
    principals = list(load_fabric_identities().values())
    owner = next(p for p in principals if "admin" in p.roles)
    executor = next(p for p in principals if p.kind == "service")
    return source, service, owner, executor


def save(out, name, data):
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def start(args):
    source, service, owner, _ = runtime()
    before = chain.original_digest(source, SCOPE)
    analyze, model = None, None
    if args.analysis:
        from forgeflow.analysis_provider import OllamaProvider
        from forgeflow.analysis_runtime import build_fabric_runtime

        model = asyncio.run(OllamaProvider().preflight())
        _, workflow = build_fabric_runtime(source)
        analyze = workflow.analyze

    async def check(scope, run_id):
        return await load_workflow(source).check(scope, run_id)

    state = asyncio.run(chain.start_chain(source, service, owner, key=args.key, fixture=args.fixture,
                                          check=check, analyze=analyze))
    state["model"] = {k: model[k] for k in ("model", "digest", "status") if k in model} if model else None
    state["original_aurora_digest_before"], state["original_aurora_digest_after"] = before, chain.original_digest(source, SCOPE)
    state["original_aurora_unchanged"] = before == state["original_aurora_digest_after"]
    save(args.out, f"state-{args.key}.json", state)
    print(json.dumps({"state_file": f"state-{args.key}.json", "run_id": state["run_id"], "flow_gate": state["flow_gate"],
                      "evt_gate": state["evt_gate"], "proposals": state["proposals"], "refused": state["refused"],
                      "original_aurora_unchanged": state["original_aurora_unchanged"]}, indent=2))
    return 0 if state["original_aurora_unchanged"] else 1


def verify(args):
    source, service, owner, _ = runtime()
    state = json.loads(Path(args.state).read_text())
    report = chain.verify_chain(source, service, owner, state)
    report["original_aurora_unchanged"] = chain.original_digest(source, SCOPE) == state["original_aurora_digest_before"]
    save(args.out, Path(args.state).name.replace("state-", "verify-"), report)
    print(json.dumps(report, indent=2))
    return 0 if report["audit_chain_valid"] and report["review_matches"] and report["original_aurora_unchanged"] else 1


def crash_child(approval_id):
    _, service, _, executor = runtime()
    service.execute(executor, FABRIC_SCOPE, approval_id, _before_commit=lambda: os.kill(os.getpid(), signal.SIGKILL))


def crash_execute(args):
    """Execute an APPROVED action in a child that is killed after its effects but before commit, then recover."""
    source, service, _, executor = runtime()
    child = subprocess.run([sys.executable, str(Path(__file__)), "_crash-child", args.approval_id],
                           capture_output=True, text=True, timeout=60, check=False)
    with source._tx() as session:
        left = session.scalar(select(FabricReceipt).where(
            *filters(FabricReceipt, FABRIC_SCOPE), FabricReceipt.approval_id == args.approval_id))
    receipt = service.execute(executor, FABRIC_SCOPE, args.approval_id)
    replay = service.execute(executor, FABRIC_SCOPE, args.approval_id)
    evidence = {"child_exit_code": child.returncode, "killed_by_sigkill": child.returncode == -signal.SIGKILL,
                "receipt_after_kill": left is not None, "receipt_after_recovery": receipt["receipt"]["id"],
                "recovery_replayed": receipt["replayed"], "second_execute_replayed": replay["replayed"],
                "same_receipt": replay["receipt"] == receipt["receipt"]}
    save(args.out, f"crash-{args.approval_id}.json", evidence)
    print(json.dumps(evidence, indent=2))
    ok = evidence["killed_by_sigkill"] and not evidence["receipt_after_kill"] and evidence["same_receipt"]
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("start")
    one.add_argument("--key", required=True)
    one.add_argument("--fixture", choices=["pass", "blocked", "unknown", "timeout"], default="pass")
    one.add_argument("--analysis", action="store_true", help="run the local model analysis (real Ollama calls)")
    one.add_argument("--out", required=True)
    two = commands.add_parser("verify")
    two.add_argument("--state", required=True)
    two.add_argument("--out", default=".")
    three = commands.add_parser("crash-execute")
    three.add_argument("approval_id")
    three.add_argument("--out", required=True)
    commands.add_parser("_crash-child").add_argument("approval_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "_crash-child":
            return crash_child(args.approval_id)
        return {"start": start, "verify": verify, "crash-execute": crash_execute}[args.command](args)
    except DomainError as error:
        print(json.dumps({"status": "REFUSED", "code": error.code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
