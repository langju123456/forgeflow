"""Measure annual-report desk cold/warm loading and lexical request timings locally.

This is a DEV_ONLY in-process benchmark: it uses real, hash-checked public PDFs but
forces lexical retrieval and no answer model.  It measures neither the desk-service
network stack nor model latency, and must not be used as a production throughput claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import tempfile
import time
from pathlib import Path

from forgeflow.report_desk import SOURCES, ReportDesk
from forgeflow.report_desk_perf import run_query_plan

QUESTIONS = [
    "宁德时代 2025 年经营活动产生的现金流量净额是多少？",
    "贵州茅台 2025 年直销渠道的营业收入是多少？",
    "What was Apple's products gross margin percentage in fiscal 2025?",
    "腾讯 2025 年的毛利是多少？",
]


def _load(reports: Path, work: Path) -> tuple[ReportDesk, float]:
    desk = ReportDesk(reports, work, ollama_url="http://127.0.0.1:1", answerer=None)
    started = time.perf_counter()
    desk.load()
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    missing = [report["key"] for report in desk.reports if report["state"] != "ok"]
    if missing:
        raise RuntimeError("registered report missing or changed: " + ", ".join(missing))
    if desk.mode != "lexical" or desk.gate is not None or desk.answerer is not None:
        raise RuntimeError("benchmark must remain lexical and model-free")
    return desk, elapsed


def run(reports: Path) -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cold, cold_ms = _load(reports, root / "cold")
        warm_root = root / "warm"
        shutil.copytree(root / "cold" / "ingest-cache", warm_root / "ingest-cache")
        warm, warm_ms = _load(reports, warm_root)
        plan = run_query_plan(warm.ask, QUESTIONS)
        sources = json.loads(SOURCES.read_text())["reports"]
        return {
            "schema_version": 1,
            "case_id": "annual-report-desk-local-perf-dev",
            "split": "public-dev",
            "driver": "real ReportDesk + SQLite + hash-checked public PDFs; in-process lexical calls",
            "boundary": {
                "included": ["PDF hash check", "cold/warm parsed-row cache", "SQLite", "lexical retrieval", "ask"],
                "excluded": ["Ollama embeddings", "chat generation", "Redis", "RabbitMQ", "gRPC", "HTTP network", "production load"],
            },
            "machine": {"system": platform.platform(), "machine": platform.machine()},
            "model": None,
            "model_calls": 0,
            "external_cost": 0,
            "sources": [{key: report[key] for key in ("key", "sha256", "bytes", "url")} for report in sources],
            "cold_load_ms": cold_ms,
            "warm_load_ms": warm_ms,
            "cold_rows": cold.status["rows"],
            "warm_rows": warm.status["rows"],
            "index_version": warm.index_version(),
            "queries": plan,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("reports", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run(args.reports)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(encoded)
    print(args.output, hashlib.sha256(encoded.encode()).hexdigest())
