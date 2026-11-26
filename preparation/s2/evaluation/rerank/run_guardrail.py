"""Hybrid + rerank on the frozen S2 set, as a guardrail only: nothing found before is lost, nothing unauthorized is read.

Settings and the guardrail come from PREREGISTRATION.md / preregistered.json, committed before any reranker
run. Nothing frozen is modified: the evaluator and the hybrid runner are imported unchanged.

    python preparation/s2/evaluation/rerank/run_guardrail.py              # real: local Ollama bge-m3 + the registered reranker
    python preparation/s2/evaluation/rerank/run_guardrail.py --embedder sealed --reranker fake --out /tmp/x   # plumbing only

Pass 1 runs hybrid retrieval without a reranker and must reproduce the sealed hybrid run question by question;
if it does not, the reranker is never called and the run stops (exit 2). Pass 2 adds the reranker behind the
same Store, authorization and citation steps and checks the registered guardrail against the sealed run.
`--embedder sealed` replays the bge-m3 cosines recorded by the sealed vector run, so the mechanics can be
exercised without Ollama; with it or with `--reranker fake` the numbers mean nothing and the status says so.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "hybrid"))

import evaluator as ev  # noqa: E402 -- frozen evaluator, imported unchanged
import rerank_support as support  # noqa: E402
import run_modes as hybrid_run  # noqa: E402 -- the sealed comparison's runner, imported unchanged

from forgeflow.contracts import DomainError, Scope  # noqa: E402
from forgeflow.retrieval import (  # noqa: E402
    HybridRanker,
    OllamaEmbedder,
    RankedKnowledgeService,
    RerankRanker,
    VectorRanker,
)

REGISTERED = json.loads((HERE / "preregistered.json").read_text())
SEALED_COSINES = HERE.parent / "hybrid/runs/20261103T153249-real-vector-f75c0fc6/similarities.jsonl"


class SealedVector:
    """Plumbing only: the vector half replayed from the cosines the sealed vector run recorded per question."""

    def __init__(self, min_similarity: float):
        self.min_similarity = min_similarity
        self.embedded_texts: list[str] = []
        self.cosines: dict[tuple[str, str, str], float] = {}
        for row in map(json.loads, SEALED_COSINES.read_text().splitlines()):
            for score in row["scores"]:
                key = (row["query"], score["id"], score["version"])
                if self.cosines.setdefault(key, score["cosine"]) != score["cosine"]:
                    raise SystemExit(f"sealed cosines disagree for {key}")

    def __call__(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        pairs = [(self.cosines[(query, str(row["id"]), str(row.get("version", "1")))], row) for row in rows]
        pairs.sort(key=lambda value: (-value[0], str(value[1]["id"]), str(value[1].get("version", "1"))))
        return [(score, row) for score, row in pairs if score >= self.min_similarity]


def load_sealed() -> dict:
    """The sealed hybrid run this guardrail compares against, checked against the registered hash."""
    sealed = REGISTERED["base"]["sealed_run"]
    path = ev.PROJECT / sealed["path"]
    if ev.sha256(path) != sealed["sha256"]:
        raise SystemExit("the sealed hybrid run differs from the registered one")
    return json.loads(path.read_text())


def hybrid_parity(results: list[dict], sealed: dict) -> dict:
    """Hybrid without a reranker against the sealed run: same hits (id, version, hash, status) and error."""
    by_id = {row["id"]: row for row in sealed["questions"]}
    differ = [r["id"] for r in results
              if hybrid_run._hit_keys(r) != hybrid_run._hit_keys(by_id[r["id"]]) or r["error"] != by_id[r["id"]]["error"]]
    return {"against": REGISTERED["base"]["sealed_run"]["path"], "questions": len(results), "differ": differ,
            "reproduced": not differ and len(results) == len(by_id)}


def readable_contents(data: dict, question: dict, identity) -> set[str]:
    """Contents this principal may read in the question's scope, worked out from the frozen documents."""
    scope = Scope.model_validate(data["scopes"][question["scope"]])
    return {doc["content"] for doc in data["documents"]
            if doc["scope"] == question["scope"] and identity.permits(scope, set(doc["roles"]), doc["classification"])}


def guardrail(sealed: dict, metrics: dict, slices: dict, outside_readable: int) -> dict:
    """The four registered conditions, measured against the sealed hybrid run."""
    base_metrics, base_slices = sealed["metrics"], sealed["slices"]
    measured = {
        "all_query_leak": metrics["all_query_leak"]["numerator"],
        "forbidden_candidate_rows": metrics["forbidden_candidate_rows"],
        "forbidden_citation_rows": metrics["forbidden_citation_rows"],
        "reranker_passages_outside_readable": outside_readable,
        "core_content_recall_at_5": metrics["core_content_recall_at_5"],
        "macro_recall_at_5": metrics["macro_recall_at_5"],
        "exact_token_fully_recalled": slices["exact-token"]["positives_fully_recalled"]["numerator"],
        "unanswerable_nonempty_context": metrics["unanswerable_nonempty_context"]["numerator"],
    }
    sealed_values = {
        "core_content_recall_at_5": base_metrics["core_content_recall_at_5"],
        "macro_recall_at_5": base_metrics["macro_recall_at_5"],
        "exact_token_fully_recalled": base_slices["exact-token"]["positives_fully_recalled"]["numerator"],
        "unanswerable_nonempty_context": base_metrics["unanswerable_nonempty_context"]["numerator"],
    }
    passed = {
        "unauthorized_zero": all(measured[key] == 0 for key in (
            "all_query_leak", "forbidden_candidate_rows", "forbidden_citation_rows",
            "reranker_passages_outside_readable")),
        "recall_not_lower": all(measured[key] >= sealed_values[key] - 1e-12 for key in (
            "core_content_recall_at_5", "macro_recall_at_5")),
        "exact_token_not_lower": measured["exact_token_fully_recalled"] >= sealed_values["exact_token_fully_recalled"],
        "unanswerable_not_more": (measured["unanswerable_nonempty_context"]
                                  <= sealed_values["unanswerable_nonempty_context"]),
    }
    return {"rule": REGISTERED["guardrail"], "sealed_hybrid": sealed_values, "measured": measured, "passed": passed,
            "passed_all": all(passed.values())}


def order_changes(base: list[dict], reranked: list[dict]) -> dict:
    """Which questions the reranker reordered, and where the first gold source moved down."""
    by_id = {row["id"]: row for row in base}
    changed = [r["id"] for r in reranked if hybrid_run._hit_keys(r) != hybrid_run._hit_keys(by_id[r["id"]])]
    lower = [{"id": r["id"], "hybrid": by_id[r["id"]]["metrics"]["reciprocal_rank_at_5"],
              "hybrid_rerank": r["metrics"]["reciprocal_rank_at_5"]}
             for r in reranked if r["metrics"]["relevant_total"]
             and r["metrics"]["reciprocal_rank_at_5"] < by_id[r["id"]]["metrics"]["reciprocal_rank_at_5"]]
    lost = [r["id"] for r in reranked if r["metrics"]["relevant_found"] < by_id[r["id"]]["metrics"]["relevant_found"]]
    return {"questions_with_a_different_top5": changed, "mrr_lower_than_hybrid": lower, "gold_lost": lost}


def run(embedder_kind: str, reranker_kind: str, ollama_url: str, out: Path, local_files_only: bool) -> tuple[Path, dict]:
    settings = support.registered_reranker()
    base = REGISTERED["base"]["settings"]
    if base != hybrid_run.PREREGISTERED:
        raise SystemExit("hybrid settings differ from the hybrid registration")
    if ev.sha256(ev.PROJECT / REGISTERED["frozen"]["path"]) != REGISTERED["frozen"]["sha256"]:
        raise SystemExit("freeze.json differs from the registered one")
    sealed = load_sealed()
    inputs = hybrid_run.verify_inputs()
    data = ev.validate_dataset(json.loads((ev.ROOT / "dataset.json").read_text()))
    real = embedder_kind == "ollama" and reranker_kind == "cross-encoder"
    output = out / f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{'real' if real else 'fake'}-hybrid-rerank-{uuid4().hex[:8]}"
    output.mkdir(parents=True)

    if embedder_kind == "ollama":
        embedder = OllamaEmbedder(base["model"], ollama_url)
        vector = VectorRanker(embedder, base["min_similarity"])
    elif embedder_kind == "sealed":
        embedder, vector = None, SealedVector(base["min_similarity"])
    else:
        embedder = hybrid_run.FakeEmbedder()
        vector = VectorRanker(embedder, base["min_similarity"])
    hybrid = HybridRanker(vector, base["rrf_k"])
    report: dict = {
        "recorded_at": None, "status": None, "git_head": support.git_head(),
        "preregistered": REGISTERED, "preregistered_sha256": ev.sha256(HERE / "preregistered.json"),
        "reranker_settings": settings,
        "embedder": getattr(embedder, "model_id", "sealed bge-m3 cosines (replay)"),
        "embedder_model_detail": hybrid_run.model_detail(embedder),
        "dataset_id": data["id"], "freeze_sha256": ev.sha256(ev.ROOT / "freeze.json"), "inputs_verified": inputs,
        "environment": {
            "python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "packages": {name: importlib.metadata.version(name) for name in ["pydantic", "sqlalchemy", "httpx"]},
            "store": "ephemeral fresh SQLite; synthetic fixture principals",
            "retriever": "RankedKnowledgeService(RerankRanker(HybridRanker)); S1 ACL and citation checks; "
                         "embeddings from local Ollama, reranker in-process",
        },
    }
    with tempfile.TemporaryDirectory(prefix="sqlite-", dir=output) as scratch:
        store, seeded = ev.seed_store(data, "sqlite:///" + str(Path(scratch) / "evaluation.db"))
        trace = ev.RecordingStore(store)
        identities = ev.principals(data)
        try:
            reader = RankedKnowledgeService(trace, hybrid)
            for q in data["questions"]:  # warmup, as in the frozen runner; it also fills the vector cache
                try:
                    reader.search(identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]]),
                                  q["query"], base["top_k"])
                except DomainError:
                    pass
            base_results = [ev.evaluate_question(data, q, store, seeded, reader, trace, identities[q["principal"]])
                            for q in data["questions"]]
            parity = hybrid_parity(base_results, sealed)
            report["hybrid_parity_with_sealed_run"] = parity
            report["hybrid"] = {"metrics": ev.summarize(base_results), "slices": hybrid_run.slice_summary(base_results)}
            if real and not parity["reproduced"]:
                report["status"] = "BASELINE_NOT_REPRODUCED"  # the reranker is never loaded or called
            else:
                reranker = support.build_reranker(reranker_kind, settings, local_files_only)
                report["reranker_timing"] = support.load_and_warm(reranker)
                report["reranker"] = support.reranker_detail(reranker)
                mismatch = support.settings_mismatch(report["reranker"], settings) if reranker_kind != "fake" else []
                if mismatch:
                    raise SystemExit("loaded reranker differs from the registered settings: " + "; ".join(mismatch))
                timed = support.TimedReranker(reranker)
                reranking = RerankRanker(hybrid, timed, settings["candidates"])
                reader = RankedKnowledgeService(trace, reranking)
                results, audits = [], []
                for q in data["questions"]:
                    identity, start = identities[q["principal"]], len(timed.calls)
                    results.append(ev.evaluate_question(data, q, store, seeded, reader, trace, identity))
                    calls = timed.calls[start:]
                    readable = readable_contents(data, q, identity)
                    audits.append({
                        "id": q["id"], "reranker_calls": len(calls),
                        "passages": sum(len(call["passages"]) for call in calls),
                        "outside_readable": sum(passage not in readable for call in calls
                                                for passage in call["passages"]),
                        "query_altered": sum(call["query"] != q["query"] for call in calls),
                    })
                metrics, slices = ev.summarize(results), hybrid_run.slice_summary(results)
                outside = sum(audit["outside_readable"] for audit in audits)
                report["hybrid_rerank"] = {"metrics": metrics, "slices": slices, "questions": results}
                report["rerank_audit"] = {
                    "calls": len(timed.calls), "passages": sum(audit["passages"] for audit in audits),
                    "outside_readable": outside, "query_altered": sum(audit["query_altered"] for audit in audits),
                    "most_passages_in_one_call": max((len(call["passages"]) for call in timed.calls), default=0),
                    "reranker_ms_per_call": ev.latency_summary([call["ms"] for call in timed.calls]),
                    "truncation": support.truncation(reranker, timed.calls), "per_question": audits,
                }
                report["order_changes"] = order_changes(base_results, results)
                report["guardrail"] = guardrail(sealed, metrics, slices, outside)
                report["status"] = "REAL_HYBRID_RERANK_SYNTHETIC" if real else "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS"
        finally:
            store.engine.dispose()
    report["recorded_at"] = datetime.now(UTC).isoformat()
    report["texts_sent_to_embedder"] = len(vector.embedded_texts)
    report["benchmark_and_present_s1_unchanged_after"] = hybrid_run.verify_inputs() == inputs
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return output, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--embedder", choices=["ollama", "sealed", "fake"], default="ollama")
    parser.add_argument("--reranker", choices=["cross-encoder", "fake"], default="cross-encoder")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--local-files-only", action="store_true", help="load the reranker from the local cache only")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    args = parser.parse_args()
    output, report = run(args.embedder, args.reranker, args.ollama_url, args.out, args.local_files_only)
    brief: dict = {"output": str(output), "status": report["status"],
                   "hybrid_parity_differ": report["hybrid_parity_with_sealed_run"]["differ"],
                   "inputs_unchanged": report["benchmark_and_present_s1_unchanged_after"]}
    if "hybrid_rerank" in report:
        m, base = report["hybrid_rerank"]["metrics"], report["hybrid"]["metrics"]
        brief |= {
            "core_content_recall_at_5": [round(base["core_content_recall_at_5"], 4), round(m["core_content_recall_at_5"], 4)],
            "macro_recall_at_5": [round(base["macro_recall_at_5"], 4), round(m["macro_recall_at_5"], 4)],
            "macro_mrr_at_5": [round(base["macro_mrr_at_5"], 4), round(m["macro_mrr_at_5"], 4)],
            "all_query_leak": m["all_query_leak"], "forbidden_candidates": m["forbidden_candidate_rows"],
            "reranker_passages_outside_readable": report["rerank_audit"]["outside_readable"],
            "stable": m["stable_query_results"], "latency_ms": m["latency_search_including_service_citation"],
            "order_changes": report["order_changes"], "guardrail": report["guardrail"]["passed"],
            "guardrail_passed": report["guardrail"]["passed_all"],
        }
    print(json.dumps(brief, ensure_ascii=False))
    return 2 if report["status"] == "BASELINE_NOT_REPRODUCED" else 0


if __name__ == "__main__":
    sys.exit(main())
