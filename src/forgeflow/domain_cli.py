"""Manual supplemental Architect/Android review; no build, inference or action."""

import argparse
import json

from .contracts import DomainError
from .domain_store import DomainOperations
from .local import SCOPE, load_identities, load_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review")
    review.add_argument("run_id")
    review.add_argument("--key", required=True)
    review.add_argument("--fixture", choices=["aligned", "bom-mismatch", "missing-exported"], default="aligned")
    commands.add_parser("show").add_argument("review_id")
    args = parser.parse_args()
    try:
        principal = next(p for p in load_identities().values() if "admin" in p.roles)
        service = DomainOperations(load_store())
        service.create_schema()
        result = (service.review(principal, SCOPE, args.run_id, args.key, args.fixture) if args.command == "review"
                  else service.get(principal, SCOPE, args.review_id))
    except DomainError as error:
        print(json.dumps({"status": "REJECTED", "code": error.code}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
