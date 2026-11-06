"""Check printed figures from real annual reports against forgeflow.pdf_ingest's tables.

    python preparation/s4/real-reports/table_probe.py --reports ~/code/finreports \
        --facts preparation/s4/real-reports/table_probe.json --out runs/probe.json

A fact passes when, in a table on the stated page, a row whose label contains `row` has a cell under a column
whose key contains `col` (blank: any value column) whose parsed value equals the printed figure, and, where the
fact says so, whose unit and scale match. Spaces and the " / " that joins multi-line headers are ignored when
comparing labels and keys. Everything else is a miss, sorted by what went wrong:
NO_ROW (no row with that label on the page), WRONG_COLUMN (the figure is in the row but under another or an
empty header), WRONG_VALUE (the row exists, the figure is not in it), WRONG_UNIT.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from forgeflow.contracts import Scope
from forgeflow.pdf_ingest import ingest_pdf


def norm(text: str | None) -> str:
    return re.sub(r"[\s/]+", "", text or "").lower()


def num(text: str | None) -> str:
    return (text or "").replace(",", "")


def check(fact: dict, tables: list[dict]) -> dict:
    on_page = [t for t in tables if any(s["page"] == fact["page"] for s in t["segments"])]
    rows = [(t, r) for t in on_page for r in t["rows"] if norm(fact["row"]) in norm(r.get("label"))]
    outcome = {"status": "NO_ROW", "tables_on_page": len(on_page)}
    for table, row in rows:
        def same(cell: dict) -> bool:
            return cell.get("raw") == fact["raw"] or num(cell.get("value")) == num(fact["raw"])

        cells = [c for c in row["cells"] if not fact["col"] or norm(fact["col"]) in norm(c.get("column"))]
        hit = next((c for c in cells if same(c)), None)
        if hit:
            unit_ok = all(str(hit.get(k)) == str(fact[k]) for k in ("unit", "scale") if k in fact)
            return {"status": "OK" if unit_ok else "WRONG_UNIT", "table": table["id"], "column": hit.get("column"),
                    "unit": hit.get("unit"), "scale": hit.get("scale")}
        outcome = {"status": "WRONG_COLUMN" if any(same(c) for c in row["cells"]) else "WRONG_VALUE",
                   "table": table["id"], "label": row.get("label"),
                   "cells": [[c.get("column"), c.get("raw")] for c in row["cells"]][:6]}
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, required=True, help="folder with the PDFs named <key>.pdf")
    parser.add_argument("--facts", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    scope = Scope(tenant_id="local", workspace_id="local", project_id="finance")
    cache: dict[str, list[dict]] = {}
    report = {"measured_at": datetime.now(UTC).isoformat(), "sets": {}}
    for facts_file in args.facts:
        facts = json.loads(facts_file.read_text())["facts"]
        results = []
        for fact in facts:
            if fact["doc"] not in cache:
                result = ingest_pdf(args.reports / f"{fact['doc']}.pdf", scope=scope, roles=["finance"], classification=1)
                cache[fact["doc"]] = result.to_dict()["tables"]
            results.append({**fact, **check(fact, cache[fact["doc"]])})
        counts = Counter(r["status"] for r in results)
        report["sets"][facts_file.stem] = {"passed": f"{counts['OK']}/{len(results)}", "counts": dict(counts),
                                           "results": results}
        print(facts_file.stem, f"{counts['OK']}/{len(results)}", dict(counts))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
