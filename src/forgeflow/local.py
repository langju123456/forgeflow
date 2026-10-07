"""Task-owned local configuration, synthetic fixture seeding and manual CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
from pathlib import Path

from .contracts import Principal, RunRequest, Scope
from .fixture_family import RENEWED_VALID_UNTIL, cli_fixture

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime"
SCOPE = Scope(tenant_id="synthetic-lab", workspace_id="engineering", project_id="aurora")


def database_url() -> str:
    value = os.environ.get("FORGEFLOW_DATABASE_URL")
    if value:
        return value
    for line in (RUNTIME / "dev.env").read_text().splitlines():
        if line.startswith("FORGEFLOW_DATABASE_URL="):
            return line.partition("=")[2]
    raise RuntimeError("Initialize the local ForgeFlow database first")


def load_store():
    from .evt_store import EVTBoundStore

    store = EVTBoundStore(database_url())
    store.create_schema()
    return store


def load_identities() -> dict[str, Principal]:
    rows = json.loads((RUNTIME / "identities.json").read_text())
    return {row["token"]: Principal.model_validate(row["principal"]) for row in rows}


def initialize():
    RUNTIME.mkdir(exist_ok=True)
    path = RUNTIME / "identities.json"
    if not path.exists():
        users = [
            ("local-owner", {"submitter", "admin", "reviewer", "hardware", "pm"}),
            ("local-observer", {"submitter"}),
        ]
        rows = [
            {
                "token": secrets.token_urlsafe(32),
                "principal": Principal(
                    subject_id=name, kind="user", roles=frozenset(roles), scopes=(SCOPE,), clearance=0
                ).model_dump(mode="json"),
            }
            for name, roles in users
        ]
        path.write_text(json.dumps(rows, indent=2) + "\n")
        path.chmod(0o600)
    owner = next(p for p in load_identities().values() if "admin" in p.roles)
    image = subprocess.run(
        ["docker", "image", "inspect", "forgeflow-scanner:semgrep-1.179.0", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    config = {"scanner_image": image.stdout.strip() if image.returncode == 0 else None}
    (RUNTIME / "runtime.json").write_text(json.dumps(config) + "\n")
    store = load_store()
    store.ensure_project(SCOPE)
    store.put_document(
        owner,
        SCOPE,
        "aurora-evt-sop",
        "v1",
        "Aurora EVT readiness synthetic SOP v1: boot_cycles >=100; boot_failures=0; "
        "BOM revision A3; CI smoke true; thermal_peak_c <=70. Unknown evidence never passes. "
        "This example is not hardware certification. Technical assessment and human stage "
        "approval are separate.",
        roles={"hardware", "pm", "supervisor", "admin", "reviewer", "submitter"},
        classification=0,
        valid_until=RENEWED_VALID_UNTIL,
    )
    return {
        "scope": SCOPE.model_dump(),
        "identity": "DEV_ONLY",
        "database": "PostgreSQL",
        "tokens_file": str(path),
        "credentials": "generated task-local, mode0600",
    }


def load_workflow(store=None):
    from .context import KnowledgeService, MemoryService
    from .mcp_bridge import MCPBridge
    from .sandbox import DockerSandbox
    from .scanner import Scanner
    from .skills import SkillRegistry
    from .workflow import Workflow

    store = store or load_store()
    provider = DockerSandbox()
    config = json.loads((RUNTIME / "runtime.json").read_text()) if (RUNTIME / "runtime.json").exists() else {}
    scanner_image = os.environ.get("FORGEFLOW_SCANNER_IMAGE") or config.get("scanner_image")
    return Workflow(
        store,
        provider,
        Scanner(scanner_image),
        MCPBridge(),
        KnowledgeService(store),
        MemoryService(store),
        SkillRegistry(ROOT / "skills"),
    )


def main():
    parser = argparse.ArgumentParser(description="ForgeFlow synthetic local manual demonstration")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    submit = sub.add_parser("submit")
    submit.add_argument("--fixture", choices=["pass", "blocked", "unknown", "timeout"], default="pass")
    submit.add_argument("--legacy-fixtures", action="store_true", help="use the original fixtures, not the renewed -v2 copies")
    submit.add_argument("--key", required=True)
    for name in ("check", "show", "cancel", "recover"):
        cmd = sub.add_parser(name)
        cmd.add_argument("run_id")
    approve = sub.add_parser("decide")
    approve.add_argument("approval_id")
    approve.add_argument("proposal_hash")
    approve.add_argument("revision", type=int)
    approve.add_argument("--decision", choices=["APPROVED", "REJECTED"], default="APPROVED")
    dispatch = sub.add_parser("dispatch")
    dispatch.add_argument("run_id")
    dispatch.add_argument("action_id")
    args = parser.parse_args()
    if args.command == "init":
        output = initialize()
    else:
        store = load_store()
        owner = next(p for p in load_identities().values() if "admin" in p.roles)
        if args.command == "submit":
            output = store.submit(
                owner, RunRequest(scope=SCOPE, fixture_id=cli_fixture(args.fixture, args.legacy_fixtures)), args.key
            )
        elif args.command == "show":
            output = store.get_run(owner, SCOPE, args.run_id)
        elif args.command == "cancel":
            output = store.cancel(owner, SCOPE, args.run_id)
        elif args.command == "decide":
            output = store.decide(
                owner, SCOPE, args.approval_id, args.proposal_hash, args.revision, args.decision
            )
        elif args.command == "check":
            output = asyncio.run(load_workflow(store).check(SCOPE, args.run_id))
        elif args.command == "dispatch":
            output = load_workflow(store).dispatch(SCOPE, args.run_id, args.action_id)
        else:
            output = load_workflow(store).recover(SCOPE, args.run_id)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
