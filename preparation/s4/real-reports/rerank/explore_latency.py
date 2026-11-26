"""Exploratory: time hybrid and hybrid + rerank on the 40 annual-report questions with no other model task running.

Not part of the registration in this folder and not a replacement for its one run (runs/rerank-20261126T091738Z):
that run's latency was measured while another model task shared the GPU (STATUS.md), so this measures it again,
on an otherwise idle machine, and is labelled exploratory. Its recall, permission and decision numbers stay the
registered run's; this report keeps only timings, plus a check that every top 5 came out the same as there.

It calls the registered runner's own evaluate() (run_rerank.py, imported unchanged), so ingestion, the hybrid pass,
the rerank pass and the timing points are the same code. The reranker is the one the 年报问答 desk builds when
reranking is switched on, forgeflow.report_desk.registered_reranker(): the evaluated snapshot, read from the local
cache only. A thread asks the local Ollama every 5 seconds which models are loaded; the run refuses to start when
any model other than the embedder is loaded, and the report says whether one appeared while it ran.

    HF_HUB_OFFLINE=1 python preparation/s4/real-reports/rerank/explore_latency.py --reports ~/code/finreports
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the reranker comes from the local cache; nothing is downloaded
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run_rerank as rr  # noqa: E402 -- the registered runner, imported unchanged

from forgeflow import report_desk  # noqa: E402
from forgeflow.evt_store import EVTBoundStore  # noqa: E402
from forgeflow.retrieval import OllamaEmbedder, VectorRanker  # noqa: E402

REGISTERED_RUN = HERE / "runs/rerank-20261126T091738Z/report.json"


def loaded_models(base_url: str) -> list[str]:
    with httpx.Client(timeout=5, trust_env=False) as client:
        return sorted(str(m.get("name", "")) for m in client.get(f"{base_url}/api/ps").json().get("models", []))


class ModelWatch(threading.Thread):
    """Which models the local Ollama has loaded, every few seconds, while the measurement runs."""

    def __init__(self, base_url: str, every_s: float = 5.0):
        super().__init__(name="model-watch", daemon=True)
        self.base_url, self.every_s = base_url, every_s
        self.samples: list[dict] = []
        self._halt = threading.Event()

    def run(self) -> None:
        began = time.perf_counter()
        while not self._halt.is_set():
            try:
                models = loaded_models(self.base_url)
            except (httpx.HTTPError, ValueError) as error:
                models = [f"unavailable: {type(error).__name__}"]
            self.samples.append({"s": round(time.perf_counter() - began, 1), "models": models})
            self._halt.wait(self.every_s)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=30)


def top5(outcomes: list[dict]) -> dict[str, list[tuple]]:
    return {o["id"]: [(h["id"], h.get("version"), h.get("status")) for h in o["hits"]] for o in outcomes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--cache", type=Path, help="embedding cache file (default: <reports>/.cache/embeddings.sqlite)")
    parser.add_argument("--out", type=Path, default=HERE / "exploratory")
    args = parser.parse_args()
    reports = args.reports.expanduser().resolve()

    retrieval_cfg = rr.qa.PREREG["retrieval"]
    embedder_name = retrieval_cfg["embedder"]
    others = [m for m in loaded_models(args.ollama_url) if m.split(":")[0] != embedder_name]
    if others:
        raise SystemExit(f"other models are loaded ({', '.join(others)}): the latency would not be clean")

    settings = rr.support.registered_reranker()
    sealed = rr.load_sealed()
    registered = json.loads(REGISTERED_RUN.read_text())
    inputs = rr.qa.verify_inputs(reports)
    questions = rr.qa.load_questions()
    ollama = rr.qa.ollama_detail(args.ollama_url, [embedder_name])
    digest = next((m["digest"] for m in ollama["models"] if m["name"] in {embedder_name, embedder_name + ":latest"}),
                  None)
    if not digest:
        raise SystemExit("embedder model not found in ollama")
    inner = OllamaEmbedder(embedder_name, args.ollama_url, timeout=300.0)
    model_key = f"{inner.model_id}@{digest}"
    cache_file = args.cache or reports / ".cache" / "embeddings.sqlite"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    embedder = rr.qa.CachedBatchEmbedder(inner, cache_file, retrieval_cfg["embed_batch"], model_key)

    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    output = args.out / f"latency-{stamp}"
    output.mkdir(parents=True)
    watch = ModelWatch(args.ollama_url)
    watch.start()
    began_run = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="s4-rerank-latency-") as scratch:
        store = EVTBoundStore(f"sqlite:///{Path(scratch) / 'store.sqlite'}")
        store.create_schema()
        try:
            ingest_log, languages = rr.qa.ingest(store, reports, Path(scratch))
            vector = VectorRanker(embedder, retrieval_cfg["min_similarity"])
            result = rr.evaluate(
                store=store, questions=questions, extra_questions=rr.qa.PREREG["permission_checks"]["extra_questions"],
                people=rr.qa.principals(), named=rr.qa.scopes(), asker=rr.qa.PREREG["asker"],
                corpus_scope=rr.qa.PREREG["corpus"]["scope"], languages=languages, vector=vector,
                rrf_k=retrieval_cfg["rrf_k"], top_k=retrieval_cfg["top_k"],
                warmup_query=retrieval_cfg["warmup_query"], settings=settings,
                make_reranker=report_desk.registered_reranker,
                sealed=sealed, expected_page_recall=rr.REGISTERED["base"]["sealed_run"]["page_recall_at_5"]["all"],
                enforce_parity=True)
        finally:
            store.engine.dispose()
            watch.stop()
    run_s = round(time.perf_counter() - began_run, 1)

    others_seen = sorted({m for s in watch.samples for m in s["models"] if m.split(":")[0] != embedder_name})
    completed = result["status"] == "COMPLETED"
    same: dict = {}
    if completed:
        for name in ("hybrid", "hybrid_rerank"):
            now, then = top5(result["questions"][name]), top5(registered["questions"][name])
            differ = sorted(q for q in then if now.get(q) != then[q])
            same[name] = {"same": len(then) - len(differ), "of": len(then), "differ": differ}
    if not completed:
        status = result["status"]
    elif others_seen:
        status = "EXPLORATORY_LATENCY_NOT_CLEAN"
    else:
        status = "EXPLORATORY_LATENCY"
    per_question = []
    if completed:
        reranked = {o["id"]: o for o in result["questions"]["hybrid_rerank"]}
        for o in result["questions"]["hybrid"]:
            r = reranked[o["id"]]
            per_question.append({"id": o["id"], "hybrid_ms": o["ms"], "hybrid_rerank_ms": r["ms"],
                                 "reranker_ms": r["reranker"]["ms"], "passages": r["reranker"]["passages"]})
    report = {
        "recorded_at": datetime.now(UTC).isoformat(), "status": status, "exploratory": True,
        "note": "Timings only, measured again because the registered run's latency was taken while another model "
                "task shared the GPU. The registered run's recall, permission and decision results stand unchanged; "
                "this report is not a second registered run.",
        "git_head": rr.support.git_head(),
        "registered_run": {"path": str(REGISTERED_RUN.relative_to(rr.PROJECT)), "sha256": rr.support.sha256(REGISTERED_RUN),
                           "latency": registered["latency"], "reranker_timing": registered["reranker_timing"]},
        "reranker_source": "forgeflow.report_desk.registered_reranker",
        "reranker_settings": settings, "reranker": result.get("reranker"),
        "reranker_timing": result.get("reranker_timing"),
        "baseline_parity": result.get("baseline_parity"),
        "same_top5_as_registered_run": same,
        "latency": result.get("latency"),
        "per_question": per_question,
        "index_ms": result.get("index_ms"), "ingest_total_ms": sum(d.get("parse_ms", 0) + d.get("store_ms", 0)
                                                                   for d in ingest_log.get("documents", [])),
        "run_s": run_s,
        "other_models_seen": others_seen, "model_samples": watch.samples,
        "embedder_cache_key": model_key, "ollama_version": ollama.get("ollama_version"),
        "inputs_verified": inputs, "inputs_unchanged_after": rr.qa.verify_inputs(reports) == inputs,
        "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine()},
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    print(json.dumps({"output": str(output), "status": status, "run_s": run_s, "other_models_seen": others_seen,
                      "same_top5_as_registered_run": {k: f"{v['same']}/{v['of']}" for k, v in same.items()},
                      "latency": report["latency"], "reranker_timing": report["reranker_timing"],
                      "registered_latency": registered["latency"]}, ensure_ascii=False, indent=1))
    return 0 if status == "EXPLORATORY_LATENCY" else 2


if __name__ == "__main__":
    sys.exit(main())
