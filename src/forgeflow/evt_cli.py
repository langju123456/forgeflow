"""Create and show bound reviews; this CLI never makes a human approval decision."""

import argparse
import json

from .contracts import DomainError
from .evt_contract import DEFAULT_POLICY
from .evt_review import EVTReviews
from .local import SCOPE, load_identities, load_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("run_id")
    create.add_argument("--domain-review", required=True)
    create.add_argument("--sre-review", required=True)
    create.add_argument("--policy", default=DEFAULT_POLICY)
    create.add_argument("--key", required=True)
    commands.add_parser("show").add_argument("review_id")
    args = parser.parse_args()
    try:
        owner = next(p for p in load_identities().values() if "admin" in p.roles)
        service = EVTReviews(load_store())
        service.create_schema()
        result = (service.create(owner, SCOPE, args.run_id, args.key, args.domain_review, args.sre_review, args.policy)
                  if args.command == "create" else service.get(owner, SCOPE, args.review_id))
    except DomainError as error:
        print(json.dumps({"status": "REJECTED", "code": error.code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
