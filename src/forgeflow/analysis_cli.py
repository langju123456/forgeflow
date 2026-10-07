"""Run local bounded model analysis without approving or dispatching business actions."""

import argparse
import asyncio
import json

from .analysis_provider import OllamaProvider
from .analysis_runtime import build_runtime
from .contracts import DomainError, RunRequest
from .fixture_family import cli_fixture
from .local import SCOPE, load_identities, load_workflow


async def execute(args):
    if args.command == "preflight":
        return await OllamaProvider().preflight()
    source, workflow = build_runtime()
    caller = next(p for p in load_identities().values() if "admin" in p.roles)
    if args.command == "demo":
        request = RunRequest(scope=SCOPE, fixture_id=cli_fixture(args.fixture, args.legacy_fixtures))
        run = source.submit(caller, request, "s2-parent:" + args.key)
        if run.get("assessment_json") is None:
            await load_workflow(source).check(SCOPE, run["id"])
        run = source.get_run(caller, SCOPE, run["id"])
        analysis = await workflow.analyze(caller, SCOPE, run["id"], "s2-analysis:" + args.key)
        return {
            "run_id": run["id"],
            "technical_gate": run.get("assessment_json"),
            "parent_state": run["state"],
            "project": run["project"],
            "analysis": analysis,
            "business_actions": "NOT_APPROVED_OR_DISPATCHED_BY_ANALYSIS",
        }
    if args.command == "run":
        return await workflow.analyze(caller, SCOPE, args.run_id, args.key)
    if args.command == "show":
        return workflow.store.get(caller, SCOPE, args.analysis_id, reauthorize=workflow._reauthorize)
    if args.command == "events":
        return workflow.store.events(caller, SCOPE, args.analysis_id, reauthorize=workflow._reauthorize)
    if args.command == "cancel":
        return workflow.store.cancel(caller, SCOPE, args.analysis_id)
    return workflow.store.recover(caller, SCOPE, args.analysis_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--run-id", required=True)
    run.add_argument("--key", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("--fixture", choices=["pass", "blocked", "unknown", "timeout"], default="pass")
    demo.add_argument("--legacy-fixtures", action="store_true", help="use the original fixtures, not the renewed -v2 copies")
    demo.add_argument("--key", required=True)
    for name in ("show", "events", "cancel", "recover"):
        command = sub.add_parser(name)
        command.add_argument("analysis_id")
    try:
        result = asyncio.run(execute(parser.parse_args()))
    except DomainError as error:
        print(json.dumps({"status": "DENIED_OR_INCOMPLETE", "code": error.code}))
        raise SystemExit(1) from None
    except Exception as error:
        # Connection strings and provider error bodies never enter the CLI output.
        print(json.dumps({"status": "UNAVAILABLE", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
