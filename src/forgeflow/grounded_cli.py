"""Manually reproduce grounded selection without approving business actions."""

import argparse
import asyncio
import json

from .analysis_provider import OllamaProvider
from .contracts import DomainError, RunRequest
from .grounded_runtime import PUBLIC_SCENARIOS, GroundedRuntimeFactory
from .local import SCOPE, load_identities, load_workflow

FIXTURES = {
    "M01": "aurora-pass", "M01-blocked": "aurora-blocked", "M02": "aurora-unknown",
    "M03": "aurora-pass", "M04": "aurora-pass",
}


async def execute(args, *, factory_type=GroundedRuntimeFactory):
    if args.command == "preflight":
        return await OllamaProvider().preflight()
    factory = factory_type()
    source = factory.source
    caller = next(p for p in load_identities().values() if "admin" in p.roles)
    if args.command in {"demo", "run"}:
        workflow = factory(args.scenario)
        if args.command == "demo":
            run = source.submit(
                caller, RunRequest(scope=SCOPE, fixture_id=FIXTURES[args.scenario]),
                "s2b-parent:" + args.key,
            )
            if run.get("assessment_json") is None:
                await load_workflow(source).check(SCOPE, run["id"])
            run = source.get_run(caller, SCOPE, run["id"])
            run_id = run["id"]
        else:
            run_id = args.run_id
            run = source.get_run(caller, SCOPE, run_id)
        analysis = await workflow.analyze(caller, SCOPE, run_id, "s2b-analysis:" + args.key)
        records = factory.grounding.list(
            caller, SCOPE, analysis["id"], reauthorize=factory.reauthorize
        )
        return {
            "scenario": args.scenario, "run_id": run_id,
            "technical_gate": run.get("assessment_json"), "parent_state": run["state"],
            "project": run["project"], "analysis": analysis, "grounding_records": records,
            "business_actions": "NOT_APPROVED_OR_DISPATCHED_BY_ANALYSIS",
            "accepted_prose": "DETERMINISTIC_SERVER_RENDERER",
        }
    analysis_id = args.analysis_id
    if args.command == "records":
        return factory.grounding.list(caller, SCOPE, analysis_id, reauthorize=factory.reauthorize)
    if args.command == "show":
        return factory.persistence.get(caller, SCOPE, analysis_id, reauthorize=factory.reauthorize)
    if args.command == "events":
        return factory.persistence.events(caller, SCOPE, analysis_id, reauthorize=factory.reauthorize)
    # Refuse using the grounded interface to mutate a different application
    # revision in the shared analysis table.
    factory.persistence.get(caller, SCOPE, analysis_id, reauthorize=factory.reauthorize)
    if args.command == "cancel":
        return factory.persistence.cancel(caller, SCOPE, analysis_id)
    return factory.persistence.recover(caller, SCOPE, analysis_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-v2", action="store_true",
                        help="Use the source-enumerated application revision; new runs require a new key")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    for name in ("demo", "run"):
        command = sub.add_parser(name)
        command.add_argument("--scenario", choices=sorted(PUBLIC_SCENARIOS), default="M01")
        command.add_argument("--key", required=True)
        if name == "run":
            command.add_argument("--run-id", required=True)
    for name in ("show", "records", "events", "cancel", "recover"):
        sub.add_parser(name).add_argument("analysis_id")
    try:
        args = parser.parse_args()
        options = {}
        if args.selection_v2:
            from .grounded_selection_runtime import SelectionRuntimeFactory

            options["factory_type"] = SelectionRuntimeFactory
        result = asyncio.run(execute(args, **options))
    except DomainError as error:
        print(json.dumps({"status": "DENIED_OR_INCOMPLETE", "code": error.code}))
        raise SystemExit(1) from None
    except Exception as error:
        # Neither connection strings nor provider response bodies are printed.
        print(json.dumps({"status": "UNAVAILABLE", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
