"""Manual local fixture identities. Every approval command is explicit TEST_DRIVER."""

import argparse
import asyncio
import json

from .contracts import RunRequest
from .fabric_contract import FABRIC_SCOPE
from .fabric_runtime import initialize_fabric, load_fabric_identities, load_fabric_store
from .fabric_store import FabricOperations
from .fixture_family import cli_fixture
from .local import load_workflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    submit = sub.add_parser("submit")
    submit.add_argument("--key", required=True)
    submit.add_argument("--fixture", choices=["pass", "blocked", "unknown", "timeout"], default="pass")
    submit.add_argument("--legacy-fixtures", action="store_true", help="use the original fixtures, not the renewed -v2 copies")
    for name in ("check", "run", "get", "approval", "execute", "explain"):
        sub.add_parser(name).add_argument("id")
    review = sub.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("--key", required=True)
    review.add_argument("--fixture", choices=["aligned", "bom-mismatch", "missing-exported"], default="aligned")
    proposal = sub.add_parser("propose")
    proposal.add_argument("review_id")
    proposal.add_argument("review_hash")
    proposal.add_argument("--key", required=True)
    proposal.add_argument("--kind", required=True, choices=["TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE",
                                                           "STAGE_REVERT", "ISSUE_RETRACT", "MEMORY_REVOKE"])
    proposal.add_argument("--target-receipt")
    decision = sub.add_parser("decide")
    decision.add_argument("approval_id")
    decision.add_argument("proposal_hash")
    decision.add_argument("revision", type=int)
    decision.add_argument("--decision", required=True, choices=["APPROVED", "REJECTED"])
    memory = sub.add_parser("memory")
    memory.add_argument("--query", default="")
    args = parser.parse_args()
    if args.command == "init":
        output = initialize_fabric()
    else:
        source = load_fabric_store()
        service = FabricOperations(source)
        service.create_schema()
        principals = list(load_fabric_identities().values())
        owner = next(p for p in principals if "admin" in p.roles)
        reviewer = next(p for p in principals if "reviewer" in p.roles)
        executor = next(p for p in principals if p.kind == "service")
        scope = FABRIC_SCOPE
        if args.command == "submit":
            output = source.submit(owner, RunRequest(scope=scope, fixture_id=cli_fixture(args.fixture, args.legacy_fixtures)), args.key)
        elif args.command == "check":
            output = asyncio.run(load_workflow(source).check(scope, args.id))
        elif args.command == "run":
            output = source.get_run(owner, scope, args.id)
        elif args.command == "review":
            output = service.create(owner, scope, args.run_id, args.key, args.fixture)
        elif args.command == "get":
            output = service.get(reviewer, scope, args.id)
        elif args.command == "propose":
            output = service.propose(owner, scope, args.review_id, args.review_hash, args.key,
                                     args.kind, args.target_receipt)
        elif args.command == "approval":
            output = service.get_approval(reviewer, scope, args.id)
        elif args.command == "explain":
            output = service.explain(reviewer, scope, args.id)
        elif args.command == "decide":
            output = service.decide(reviewer, scope, args.approval_id, args.proposal_hash, args.revision, args.decision)
        elif args.command == "execute":
            output = service.execute(executor, scope, args.id)
        else:
            output = service.memory(reviewer, scope, args.query)
    print(json.dumps({"approval_provenance": "TEST_DRIVER", "output": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
