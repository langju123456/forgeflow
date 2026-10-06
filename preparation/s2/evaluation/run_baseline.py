"""Freeze once, or execute the already-frozen synthetic baseline without egress."""

from __future__ import annotations

import argparse
import json

from evaluator import ROOT, freeze_inputs, run_baseline, validate_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "freeze", "run"])
    parser.add_argument("--label", default="lexical")
    args = parser.parse_args()
    if args.command == "validate":
        data = validate_dataset(json.loads((ROOT / "dataset.json").read_text()))
        print(
            json.dumps(
                {
                    "dataset_id": data["id"],
                    "questions": len(data["questions"]),
                    "document_versions": len(data["documents"]),
                    "retrieval_executed": False,
                },
                indent=2,
            )
        )
    elif args.command == "freeze":
        print(json.dumps(freeze_inputs(), ensure_ascii=False, indent=2))
    else:
        output, report = run_baseline(args.label)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "metrics": report["metrics"],
                    "sealed_s1_unchanged_after": report["sealed_s1_unchanged_after"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
