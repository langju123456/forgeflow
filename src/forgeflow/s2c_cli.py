"""Manually run the local S2C chain; raw selection and server state are separate."""

import argparse
import asyncio
import json

from .analysis_provider import OllamaProvider
from .contracts import DomainError
from .grounded_runtime import PUBLIC_SCENARIOS
from .local import SCOPE, load_identities
from .s2c_runtime import S2CRuntimeFactory


async def execute(args, *, factory_type=S2CRuntimeFactory):
    if args.command == "preflight":
        return await OllamaProvider().preflight()
    factory = factory_type()
    caller = next(p for p in load_identities().values() if "admin" in p.roles)
    if args.command in {"demo", "run"}:
        workflow = factory(args.scenario)
        if args.command == "demo":
            parent = factory.submit_parent(caller, args.scenario, "s2c-parent:" + args.key)
            await factory.check_parent(SCOPE, parent)
            run_id = parent["id"]
        else:
            run_id = args.run_id
        parent = factory.source.get_run(caller, SCOPE, run_id)
        analysis = await workflow.analyze(caller, SCOPE, run_id, "s2c-analysis:" + args.key)
        records = factory.grounding.list(caller, SCOPE, analysis["id"], reauthorize=factory.reauthorize)
        return {"scenario": args.scenario, "run_id": run_id, "technical_gate": parent["assessment_json"],
                "project": parent["project"], "analysis": analysis, "grounding_records": records,
                "model_task": "EVIDENCE_SELECTION_ONLY", "status_authority": "SERVER_DERIVED_AUTHORIZED_CATALOG",
                "business_actions": "NOT_APPROVED_OR_DISPATCHED"}
    aid = args.analysis_id
    if args.command == "records":
        return factory.grounding.list(caller, SCOPE, aid, reauthorize=factory.reauthorize)
    if args.command == "events":
        return factory.persistence.events(caller, SCOPE, aid, reauthorize=factory.reauthorize)
    result = factory.persistence.get(caller, SCOPE, aid, reauthorize=factory.reauthorize)
    if args.command == "show":
        return result
    if args.command == "cancel":
        return factory.persistence.cancel(caller, SCOPE, aid)
    return factory.persistence.recover(caller, SCOPE, aid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    for name in ("demo", "run"):
        command = commands.add_parser(name)
        command.add_argument("--scenario", choices=sorted(PUBLIC_SCENARIOS), default="M01")
        command.add_argument("--key", required=True)
        if name == "run":
            command.add_argument("--run-id", required=True)
    for name in ("show", "records", "events", "cancel", "recover"):
        commands.add_parser(name).add_argument("analysis_id")
    try:
        result = asyncio.run(execute(parser.parse_args()))
    except DomainError as error:
        print(json.dumps({"status": "DENIED_OR_INCOMPLETE", "code": error.code}))
        raise SystemExit(1) from None
    except Exception as error:
        print(json.dumps({"status": "UNAVAILABLE", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
