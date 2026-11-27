"""Run the public CATL development example with a real PDF, SQLite and the ordinary HTTP handler."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.annual_cash import DOCUMENT, METRIC, VERSION, read_cell
from forgeflow.report_desk import SOURCES, ReportDesk, mount_report_desk


def compare(rows, year):
    first = None
    proven = []
    for row in rows:
        for line in row["content"].splitlines():
            fields = [v.strip() for v in line.split("|")]
            if not first and fields[0] == METRIC and len(fields) > 1 and fields[1]:
                first = {"raw": fields[1], "id": row["id"]}
        cell = read_cell(row["id"], row["content"], year)
        if cell:
            proven.append({"raw": cell.raw, "id": row["id"]})
    agrees = len({c["raw"] for c in proven}) == 1
    return {"row_first": first, "year_and_unit": proven if agrees else [],
            "two_disclosures": proven if agrees and len(proven) >= 2 else []}


def run(reports_dir):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        report = next(r for r in json.loads(SOURCES.read_text())["reports"] if r["key"] == DOCUMENT)
        source = root / "catalogue.json"
        source.write_text(json.dumps({"reports": [report]}))
        # No ranking/answer model is involved. The period lookup uses the real ACL and Store.
        desk = ReportDesk(reports_dir, root, sources=source, ollama_url="http://127.0.0.1:1")
        desk.load()
        if not desk.reports or desk.reports[0]["state"] != "ok":
            raise RuntimeError("CATL report is missing or does not match the manifest SHA-256")
        app = FastAPI()
        mount_report_desk(app, desk)
        outcomes = []
        with TestClient(app) as client:
            for year in ("2025", "2024", "2023", "2022"):
                question = f"宁德时代 {year} 年经营活动产生的现金流量净额是多少？"
                start = time.perf_counter()
                response = client.post("/reports/ask", json={"question": question})
                response.raise_for_status()
                answer = response.json()
                citations = [{k: c[k] for k in ("id", "page", "bbox")} for c in answer["citations"]]
                outcomes.append({"request": question, "status": answer["status"], "answer": answer["answer"],
                                 "citations": citations, "wall_ms": round((time.perf_counter()-start)*1000, 1)})
        # Fixed candidate order: the original incorrect citation, then the two annual tables,
        # then the parent-company table. This isolates the user's choice of period safeguard.
        ids = [f"{DOCUMENT}#p{part}" for part in ("12-t1", "11-t1", "30-t3", "121-t2")]
        rows = [desk._row_index[i] for i in ids]
        trials = {year: compare(rows, year) for year in ("2025", "2024", "2023", "2022")}
        return {"schema_version": 1, "case_id": "catl-operating-cash-period-dev", "split": "public-dev",
                "source": {"url": report["url"], "sha256": report["sha256"]}, "tool_version": VERSION,
                "model": None, "model_calls": 0, "external_cost": 0, "transport": "FastAPI TestClient",
                "index_version": desk.index_version(), "candidate_ids": ids, "comparison": trials,
                "product_runs": outcomes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run(args.reports)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(encoded)
    print(args.output, hashlib.sha256(encoded.encode()).hexdigest())
