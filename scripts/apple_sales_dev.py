"""Execute the second public development case on a fresh parse of Apple's filing."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow import apple_sales
from forgeflow.report_desk import SOURCES, ReportDesk, mount_report_desk


def run(reports_dir):
    report = next(r for r in json.loads(SOURCES.read_text())["reports"] if r["key"] == apple_sales.DOCUMENT)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        catalogue = root / "sources.json"
        catalogue.write_text(json.dumps({"reports": [report]}))
        desk = ReportDesk(reports_dir, root, sources=catalogue, ollama_url="http://127.0.0.1:1")
        desk.load()
        if desk.reports[0]["state"] != "ok":
            raise ValueError("expected the verified Apple FY2025 PDF")
        app = FastAPI()
        mount_report_desk(app, desk)
        outcomes = []
        with TestClient(app) as client:
            for year in (2025, 2024, 2023):
                question = f"Apple FY{year} 服务收入增速和增长贡献"
                start = time.perf_counter()
                response = client.post("/reports/ask", json={"question": question})
                response.raise_for_status()
                result = response.json()
                timings = round((time.perf_counter()-start)*1000, 1)
                receipt = {"question": question, "answer": result["answer"], "status": result["status"],
                           "calculation": result.get("calculation"),
                           "sources": [{k: c[k] for k in ("id", "page", "bbox")} for c in result["citations"]]}
                receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()
                outcomes.append({"run_id": str(uuid4()), "tool": apple_sales.VERSION, "input": question,
                                 "artifact_refs": [{"path": "inline:receipt",
                                                    "sha256": hashlib.sha256(receipt_bytes).hexdigest()}],
                                 "receipt": receipt, "latency_ms": timings, "model_calls": 0, "external_cost": 0})
        # Exercise all three approaches on the same disclosure, with no inference or external calls.
        candidate = desk._row_index[f"{apple_sales.DOCUMENT}#p26-t1"]
        sales = apple_sales.read_sales(candidate["id"], candidate["content"], 2025)
        annual = next(line for line in candidate["content"].splitlines() if line.startswith("Services (1) |"))
        disclosed = re.search(r"\|\s*(\d+(?:\.\d+)?)\s*%", annual)
        if sales is None or disclosed is None:
            raise ValueError("published comparison no longer has the expected row or explicit change column")
        comparison = {"source_id": candidate["id"], "original_row": annual, "model_calls_per_strategy": 0,
                      "disclosed_change_percent": disclosed[1],
                      "current_revenue_share_percent": format(Decimal(sales.current_services)*100/sales.current_total, ".2f"),
                      "two_period_formulas": apple_sales.calculation(sales, 2025, "增速和增长贡献")}
        return {"schema_version": 1, "case_id": "apple-services-growth-contribution-dev", "split": "public-dev",
                "source": {"url": report["url"], "sha256": report["sha256"]}, "index_version": desk.index_version(),
                "transport": "FastAPI TestClient", "model": None, "comparison": comparison, "runs": outcomes}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(args.reports), ensure_ascii=False, indent=2) + "\n")
