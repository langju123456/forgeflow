"""Retrieval only over the four real annual reports: hybrid, then hybrid + rerank. No answer or gate model is called.

Settings, metrics and the decision rule come from PREREGISTRATION.md / preregistered.json in this folder,
committed before any reranker run. Corpus, ingestion, restricted copies, principals and the two extra
permission questions are the earlier registration's (../preregistered.json), through the same code
(../run_qa.py, imported unchanged).

    python preparation/s4/real-reports/rerank/run_rerank.py --reports ~/code/finreports \\
        --guardrail-report preparation/s2/evaluation/rerank/runs/<run>/report.json            # real
    python preparation/s4/real-reports/rerank/run_rerank.py --reports DIR --embedder fake --reranker fake \\
        --out /tmp/x                                                                          # plumbing only

Pass 1 asks the 40 frozen questions (and the two extra permission questions) with hybrid retrieval alone and
must reproduce the sealed run's top 5 for every question and its page-level Recall@5; if it does not, the
reranker is never loaded and the run stops (exit 2). Pass 2 asks them again with the registered reranker
behind the same Store, authorization and citation steps. Every row the Store returns, every citation, every
passage sent to the reranker and every text sent to the embedder is audited for content the asking principal
may not read.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(PROJECT / "preparation/s2/evaluation/rerank"))

import rerank_support as support  # noqa: E402
import run_qa as qa  # noqa: E402 -- the sealed QA run's runner, imported unchanged

from forgeflow.contracts import DomainError, Principal, Scope  # noqa: E402
from forgeflow.evt_store import EVTBoundStore  # noqa: E402
from forgeflow.pdf_ingest import parse_source_line  # noqa: E402
from forgeflow.retrieval import (  # noqa: E402
    HybridRanker,
    OllamaEmbedder,
    RankedKnowledgeService,
    RerankRanker,
    VectorRanker,
)

REGISTERED = json.loads((HERE / "preregistered.json").read_text())
LEAK_COUNTERS = ("store_rows_forbidden", "store_rows_restricted", "citations_forbidden", "hits_restricted",
                 "reranker_passages_with_restricted_marker", "reranker_passages_not_readable")


class RecordingRanker:
    """Passes a ranker through and keeps its last full ranking: the candidates the reranker is given."""

    def __init__(self, base):
        self.base = base
        self.last: list[tuple[float, dict]] | None = None

    def __call__(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        self.last = self.base(query, rows)
        return self.last


def row_place(row: dict) -> dict:
    cited = parse_source_line(str(row["content"])) or {"file": None, "pages": []}
    return {"file": cited["file"], "pages": sorted({p["page"] for p in cited["pages"]})}


def place_on_gold(place: dict, doc: str, pages: list[int]) -> bool:
    return place["file"] == f"{doc}.pdf" and bool(set(place["pages"]) & set(pages))


def load_sealed() -> dict:
    """The sealed QA run whose retrieval pass 1 must reproduce, checked against the registered hash."""
    sealed = REGISTERED["base"]["sealed_run"]
    path = PROJECT / sealed["path"]
    if support.sha256(path) != sealed["sha256"]:
        raise SystemExit("the sealed QA run differs from the registered one")
    return json.loads(path.read_text())


def load_guardrail(path: Path | None, real: bool) -> dict | None:
    """The 47-question guardrail's verdict, from the report run_guardrail.py wrote."""
    if path is None:
        return None
    report = json.loads(path.read_text())
    if real and report.get("status") != "REAL_HYBRID_RERANK_SYNTHETIC":
        raise SystemExit("the guardrail report is not a completed real guardrail run")
    if report.get("reranker_settings") != support.registered_reranker():
        raise SystemExit("the guardrail ran with other reranker settings")
    return {"path": str(path), "sha256": support.sha256(path), "status": report.get("status"),
            "git_head": report.get("git_head"), "passed": report["guardrail"]["passed"],
            "passed_all": report["guardrail"]["passed_all"]}


# --------------------------------------------------------------------------- one question


def retrieve(q: dict, principal: Principal, scope: Scope, service: RankedKnowledgeService, audit: qa.AuditStore,
             ranking: RecordingRanker, timed: support.TimedReranker | None, readable: set[str], top_k: int,
             candidates: int) -> dict:
    """One search, with what the Store returned, what was ranked, what the reranker was sent, and where gold is."""
    audit.reset()
    ranking.last = None
    first_call = len(timed.calls) if timed else 0
    began = time.perf_counter()
    error = None
    try:
        hits = tuple(service.search(principal, scope, q["query"], top_k))
    except DomainError as exc:
        hits, error = (), exc.code
    ms = (time.perf_counter() - began) * 1000
    rows_seen, citations_seen = list(audit.rows), list(audit.citations)
    ranked = ranking.last or []
    calls = timed.calls[first_call:] if timed else []
    rerank_scores = calls[0]["scores"] if calls else []  # same order as the first `candidates` ranked rows
    listed = [{"id": row["id"], **row_place(row), "rrf_rank": rank, "rrf_score": round(score, 6),
               "rerank_score": rerank_scores[rank - 1] if rank <= len(rerank_scores) else None}
              for rank, (score, row) in enumerate(ranked[:candidates], start=1)]
    out: dict[str, Any] = {
        "id": q["id"], "kind": q["kind"], "lang": q["lang"], "doc": q.get("doc"), "query": q["query"],
        "principal": principal.subject_id, "scope": scope.project_id,
        "ms": round(ms, 1), "error": error, "hits": [qa._hit_view(h) for h in hits],
        "store_rows_seen": len(rows_seen),
        "store_rows_forbidden": sum(not qa.row_authorized(r, principal, scope) for r in rows_seen),
        "store_rows_restricted": sum(bool(qa._markers(str(r.get("id")) + str(r.get("content", ""))))
                                     or r.get("project_id") != scope.project_id for r in rows_seen),
        "citations_seen": len(citations_seen),
        "citations_forbidden": sum(not qa.row_authorized(r, principal, scope) for r in citations_seen),
        "hits_restricted": sum(bool(qa._markers(h.id + h.content)) for h in hits),
        "candidates_returned": len(ranked), "candidates": listed,
    }
    if timed is not None:
        passages = [passage for call in calls for passage in call["passages"]]
        out["reranker"] = {
            "calls": len(calls), "passages": len(passages), "ms": round(sum(call["ms"] for call in calls), 1),
            "passages_with_restricted_marker": sum(bool(qa._markers(passage)) for passage in passages),
            "passages_not_readable": sum(passage not in readable for passage in passages),
            "query_altered": sum(call["query"] != q["query"] for call in calls)}
    if q["kind"] == "answerable":
        out["gold"] = {"doc": q["doc"], "pages": q["pages"]}
        gold_ranks = [rank for rank, h in enumerate(hits, start=1) if qa.on_gold_page(h, q["doc"], q["pages"])]
        out["gold_page_in_top5"] = bool(gold_ranks)
        out["reciprocal_rank"] = 1 / gold_ranks[0] if gold_ranks else 0.0
        for candidate in listed:
            candidate["gold_page"] = place_on_gold(candidate, q["doc"], q["pages"])
        out["gold_in_first_n"] = any(candidate["gold_page"] for candidate in listed)
        out["first_gold_rrf_rank"] = next((rank for rank, (_, row) in enumerate(ranked, start=1)
                                           if place_on_gold(row_place(row), q["doc"], q["pages"])), None)
    return out


# --------------------------------------------------------------------------- summaries (pure, tested)


def summarize(outcomes: list[dict], languages: dict[str, str]) -> dict:
    def cross(o: dict) -> bool:
        return languages[o["doc"]] != o["lang"]

    def mean(values: list[float]) -> float | None:
        return round(statistics.mean(values), 4) if values else None

    answerable = [o for o in outcomes if o["kind"] == "answerable"]
    split = {"all": lambda o: True, "same_language": lambda o: not cross(o), "cross_language": cross,
             "zh_on_zh": lambda o: not cross(o) and o["lang"] == "zh",
             "en_on_en": lambda o: not cross(o) and o["lang"] == "en"}
    return {
        "page_recall_at_5": {k: qa.rate([o["gold_page_in_top5"] for o in answerable if f(o)])
                             for k, f in split.items()},
        "mrr_at_5": {k: mean([o["reciprocal_rank"] for o in answerable if f(o)]) for k, f in split.items()},
        "candidate_ceiling": {k: qa.rate([o["gold_in_first_n"] for o in answerable if f(o)])
                              for k, f in split.items()},
        "first_gold_rrf_rank": {o["id"]: o["first_gold_rrf_rank"] for o in answerable},
        "retrieval_errors": sum(bool(o["error"]) for o in outcomes),
        "latency": qa.pct([o["ms"] for o in outcomes]),
    }


def baseline_parity(base: list[dict], base_extra: list[dict], sealed: dict, expected: str) -> dict:
    """Hybrid without a reranker against the sealed run: the same top 5 in the same order, and 18/28."""
    by_id = {q["id"]: q["retrieval"] for q in sealed["questions"] + sealed["extra_questions"]}

    def key(hits: list[dict]) -> list[tuple]:
        return [(h["id"], h["version"], h["status"]) for h in hits]

    top5_differ = [o["id"] for o in base + base_extra
                   if o["id"] not in by_id or key(o["hits"]) != key(by_id[o["id"]]["hits"])
                   or o["error"] != by_id[o["id"]]["error"]]
    gold_differ = [o["id"] for o in base if o["kind"] == "answerable" and o["id"] in by_id
                   and o["gold_page_in_top5"] != by_id[o["id"]]["gold_page_in_top5"]]
    recall = qa.rate([o["gold_page_in_top5"] for o in base if o["kind"] == "answerable"])
    measured = f"{recall['n']}/{recall['of']}"
    return {"against": REGISTERED["base"]["sealed_run"]["path"], "questions": len(base) + len(base_extra),
            "top5_differ": top5_differ, "gold_page_differ": gold_differ,
            "page_recall_at_5": measured, "expected_page_recall_at_5": expected,
            "reproduced": (len(base) + len(base_extra) == len(by_id) and not top5_differ and not gold_differ
                           and measured == expected)}


def changes(base: list[dict], reranked: list[dict]) -> dict:
    """Per question, what reranking changed, and for the misses whether the reranker ever had the gold page."""
    after = {o["id"]: o for o in reranked}
    answerable = [o for o in base if o["kind"] == "answerable"]
    missed = [o for o in answerable if not after[o["id"]]["gold_page_in_top5"]]
    return {
        "gained": [o["id"] for o in answerable if not o["gold_page_in_top5"] and after[o["id"]]["gold_page_in_top5"]],
        "lost": [o["id"] for o in answerable if o["gold_page_in_top5"] and not after[o["id"]]["gold_page_in_top5"]],
        "missed_with_gold_among_candidates": [o["id"] for o in missed if o["gold_in_first_n"]],
        "missed_with_gold_outside_candidates": [o["id"] for o in missed if not o["gold_in_first_n"]],
        "top5_changed": [o["id"] for o in base
                         if [h["id"] for h in o["hits"]] != [h["id"] for h in after[o["id"]]["hits"]]],
    }


def permission_summary(passes: dict[str, tuple[list[dict], list[dict]]], embedded: list[str]) -> dict:
    """Every unauthorized counter per pass; all must be 0 and both extra questions must return nothing."""
    out: dict[str, Any] = {}
    for name, (main, extra) in passes.items():
        both = main + extra
        counters = {key: sum(o[key] for o in both) for key in (
            "store_rows_seen", "store_rows_forbidden", "store_rows_restricted", "citations_seen",
            "citations_forbidden", "hits_restricted")}
        for key in ("passages", "passages_with_restricted_marker", "passages_not_readable"):
            counters[f"reranker_{key}"] = sum(o.get("reranker", {}).get(key, 0) for o in both)
        extras = []
        for o in extra:
            reranker_calls = o.get("reranker", {}).get("calls", 0)
            extras.append({"id": o["id"], "check": o.get("check"), "principal": o["principal"], "scope": o["scope"],
                           "query": o["query"], "retrieval_error": o["error"],
                           "store_rows_seen": o["store_rows_seen"], "hits": len(o["hits"]),
                           "reranker_calls": reranker_calls,
                           "passed": o["store_rows_seen"] == 0 and not o["hits"] and reranker_calls == 0})
        leaks = sum(counters[key] for key in LEAK_COUNTERS)
        out[name] = counters | {"extra_questions": extras, "leak_total": leaks,
                                "passed": leaks == 0 and all(e["passed"] for e in extras)}
    with_marker = sum(bool(qa._markers(text)) for text in embedded)
    leak_total = sum(out[name]["leak_total"] for name in passes) + with_marker
    return out | {"embedder_texts": len(embedded), "embedder_texts_with_restricted_marker": with_marker,
                  "leak_total": leak_total,
                  "passed": with_marker == 0 and all(out[name]["passed"] for name in passes)}


def decide(base_summary: dict, rerank_summary: dict, permissions: dict, guardrail: dict | None) -> dict:
    """The registered rule: at least 3 more questions with a gold page in the top 5, nothing unauthorized, guardrail."""
    rule = REGISTERED["decision_rule"]
    before, after = base_summary["page_recall_at_5"]["all"], rerank_summary["page_recall_at_5"]["all"]
    gain = after["n"] - before["n"]
    passed = {"page_recall_gain": gain >= rule["min_page_recall_gain_questions"],
              "unauthorized_zero": bool(permissions["passed"]),
              "guardrail": guardrail["passed_all"] if guardrail else None}
    if False in passed.values():
        adopt: bool | None = False
    elif None in passed.values():
        adopt = None  # the guardrail report was not given: undecided, not adopted
    else:
        adopt = True
    return {"rule": rule,
            "measured": {"page_recall_at_5_hybrid": f"{before['n']}/{before['of']}",
                         "page_recall_at_5_hybrid_rerank": f"{after['n']}/{after['of']}",
                         "gain_questions": gain, "leak_total": permissions["leak_total"]},
            "guardrail": guardrail, "passed": passed, "adopt": adopt}


# --------------------------------------------------------------------------- the two passes


def evaluate(*, store, questions: list[dict], extra_questions: list[dict], people: dict[str, Principal],
             named: dict[str, Scope], asker: str, corpus_scope: str, languages: dict[str, str], vector,
             rrf_k: int, top_k: int, warmup_query: str, settings: dict, make_reranker, sealed: dict | None,
             expected_page_recall: str, enforce_parity: bool, log=print) -> dict:
    """Pass 1 (hybrid), the reproduction check, then pass 2 (hybrid + rerank) and its audit."""
    analyst, scope = people[asker], named[corpus_scope]
    readable = {str(row["content"]) for row in store.document_rows(analyst, scope)}
    audit = qa.AuditStore(store)
    ranking = RecordingRanker(HybridRanker(vector, rrf_k))
    candidates = settings["candidates"]

    def ask_all(service: RankedKnowledgeService, timed: support.TimedReranker | None, label: str):
        main = []
        for q in questions:
            main.append(retrieve(q, analyst, scope, service, audit, ranking, timed, readable, top_k, candidates))
            last = main[-1]
            log(f"[{label}] {q['id']} {last['ms']:.0f} ms gold-page={last.get('gold_page_in_top5')} "
                f"gold-among-first-{candidates}={last.get('gold_in_first_n')} "
                f"first-gold-rank={last.get('first_gold_rrf_rank')}")
        extra = []
        for x in extra_questions:
            outcome = retrieve({"id": x["id"], "kind": "permission", "lang": x["lang"], "query": x["query"]},
                               people[x["principal"]], named[x["scope"]], service, audit, ranking, timed, set(),
                               top_k, candidates)
            outcome["check"] = x["check"]
            extra.append(outcome)
        return main, extra

    hybrid = RankedKnowledgeService(audit, ranking)
    began = time.perf_counter()  # embeds every row the analyst may read, in batches, once
    hybrid.search(analyst, scope, warmup_query, top_k)
    result: dict[str, Any] = {"index_ms": round((time.perf_counter() - began) * 1000, 1)}
    base, base_extra = ask_all(hybrid, None, "hybrid")
    parity = baseline_parity(base, base_extra, sealed, expected_page_recall) if sealed else None
    result |= {"baseline_parity": parity, "summary": {"hybrid": summarize(base, languages)},
               "questions": {"hybrid": base}, "extra_questions": {"hybrid": base_extra}}
    if enforce_parity and not (parity and parity["reproduced"]):
        result["status"] = "BASELINE_NOT_REPRODUCED"  # the reranker is never loaded or called
        result["permissions"] = permission_summary({"hybrid": (base, base_extra)}, vector.embedded_texts)
        return result

    reranker = make_reranker()
    result["reranker_timing"] = support.load_and_warm(reranker)
    result["reranker"] = support.reranker_detail(reranker)
    if hasattr(reranker, "load"):
        mismatch = support.settings_mismatch(result["reranker"], settings)
        if mismatch:
            raise SystemExit("loaded reranker differs from the registered settings: " + "; ".join(mismatch))
    timed = support.TimedReranker(reranker)
    service = RankedKnowledgeService(audit, RerankRanker(ranking, timed, candidates))
    reranked, reranked_extra = ask_all(service, timed, "hybrid+rerank")
    result["summary"]["hybrid_rerank"] = summarize(reranked, languages)
    result["questions"]["hybrid_rerank"] = reranked
    result["extra_questions"]["hybrid_rerank"] = reranked_extra
    result["changes"] = changes(base, reranked)
    result["latency"] = {
        "hybrid_search": qa.pct([o["ms"] for o in base]),
        "hybrid_rerank_search": qa.pct([o["ms"] for o in reranked]),
        "reranker_inference": qa.pct([o["reranker"]["ms"] for o in reranked]),
        "questions_with_n_candidates": qa.rate([o["reranker"]["passages"] == candidates for o in reranked]),
    }
    result["truncation"] = support.truncation(reranker, timed.calls)
    result["permissions"] = permission_summary(
        {"hybrid": (base, base_extra), "hybrid_rerank": (reranked, reranked_extra)}, vector.embedded_texts)
    result["status"] = "COMPLETED"
    return result


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, required=True, help="folder with <key>.pdf (fetch_real_reports.sh)")
    parser.add_argument("--embedder", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--reranker", choices=["cross-encoder", "fake"], default="cross-encoder")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--cache", type=Path, help="embedding cache file (default: <reports>/.cache/embeddings.sqlite)")
    parser.add_argument("--guardrail-report", type=Path, help="report.json written by run_guardrail.py")
    parser.add_argument("--local-files-only", action="store_true", help="load the reranker from the local cache only")
    parser.add_argument("--limit", type=int, help="plumbing only: first N questions")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    args = parser.parse_args()
    reports = args.reports.expanduser().resolve()

    settings = support.registered_reranker()
    earlier = REGISTERED["base"]["preregistered"]
    if support.sha256(PROJECT / earlier["path"]) != earlier["sha256"]:
        raise SystemExit("../preregistered.json differs from the registration this one builds on")
    retrieval_cfg = qa.PREREG["retrieval"]
    if REGISTERED["retrieval"]["top_k"] != retrieval_cfg["top_k"]:
        raise SystemExit("top_k differs from the earlier registration")
    sealed = load_sealed()
    inputs = qa.verify_inputs(reports)
    questions = qa.load_questions()
    real = args.embedder == "ollama" and args.reranker == "cross-encoder"
    if args.limit:
        if real:
            raise SystemExit("--limit is for plumbing runs only")
        questions = questions[:args.limit]
    guardrail = load_guardrail(args.guardrail_report, real)

    ollama = None
    if args.embedder == "ollama":
        ollama = qa.ollama_detail(args.ollama_url, [retrieval_cfg["embedder"]])
        digest = next((m["digest"] for m in ollama["models"]
                       if m["name"] in {retrieval_cfg["embedder"], retrieval_cfg["embedder"] + ":latest"}), None)
        if not digest:
            raise SystemExit("embedder model not found in ollama")
        inner = OllamaEmbedder(retrieval_cfg["embedder"], args.ollama_url, timeout=300.0)
        model_key = f"{inner.model_id}@{digest}"
    else:
        inner, model_key = qa.FakeEmbedder(), qa.FakeEmbedder.model_id
    cache_file = (args.cache or reports / ".cache" / "embeddings.sqlite") if args.embedder == "ollama" else None
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
    embedder = qa.CachedBatchEmbedder(inner, cache_file, retrieval_cfg["embed_batch"], model_key)

    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    output = args.out / (f"rerank-{stamp}" if real else f"rerank-{stamp}-fake")
    output.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="s4-rerank-") as scratch:
        store = EVTBoundStore(f"sqlite:///{Path(scratch) / 'store.sqlite'}")
        store.create_schema()
        try:
            began = time.perf_counter()
            ingest_log, languages = qa.ingest(store, reports, Path(scratch))
            ingest_log["total_ms"] = round((time.perf_counter() - began) * 1000, 1)
            vector = VectorRanker(embedder, retrieval_cfg["min_similarity"])
            result = evaluate(
                store=store, questions=questions, extra_questions=qa.PREREG["permission_checks"]["extra_questions"],
                people=qa.principals(), named=qa.scopes(), asker=qa.PREREG["asker"],
                corpus_scope=qa.PREREG["corpus"]["scope"], languages=languages, vector=vector,
                rrf_k=retrieval_cfg["rrf_k"], top_k=retrieval_cfg["top_k"],
                warmup_query=retrieval_cfg["warmup_query"], settings=settings,
                make_reranker=lambda: support.build_reranker(args.reranker, settings, args.local_files_only),
                sealed=sealed, expected_page_recall=REGISTERED["base"]["sealed_run"]["page_recall_at_5"]["all"],
                enforce_parity=real)
        finally:
            store.engine.dispose()

    completed = result["status"] == "COMPLETED"
    status = result["status"] if not completed else ("REAL" if real else "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS")
    decision = None
    if completed and len(questions) == 40:
        decision = decide(result["summary"]["hybrid"], result["summary"]["hybrid_rerank"], result["permissions"],
                          guardrail)
    report = {
        "recorded_at": datetime.now(UTC).isoformat(), "status": status, "git_head": support.git_head(),
        "preregistered": REGISTERED, "preregistered_sha256": support.sha256(HERE / "preregistered.json"),
        "reranker_settings": settings, "earlier_registration": qa.PREREG["version"],
        "code_sha256": {name: support.sha256(PROJECT / name) for name in [
            "src/forgeflow/retrieval.py", "src/forgeflow/pdf_ingest.py", "preparation/s4/real-reports/run_qa.py",
            "preparation/s4/real-reports/rerank/run_rerank.py",
            "preparation/s2/evaluation/rerank/rerank_support.py"]},
        "embedder": embedder.model_id, "embedder_cache_key": model_key,
        "embedder_cache_key_in_sealed_run": sealed["embedder_cache_key"], "ollama": ollama,
        "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                        "packages": support.package_versions(["pydantic", "sqlalchemy", "httpx", "pdfplumber"])},
        "inputs_verified": inputs, "inputs_unchanged_after": qa.verify_inputs(reports) == inputs,
        "questions_run": len(questions), "ingest": ingest_log,
        "index": {"ms": result.pop("index_ms"), "embedder": embedder.summary(),
                  "texts_sent_to_embedder": len(vector.embedded_texts)},
        "decision": decision,
    } | {key: value for key, value in result.items() if key != "status"}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")

    brief: dict[str, Any] = {"output": str(output), "status": status, "baseline_parity": {
        key: result["baseline_parity"][key] for key in ("top5_differ", "gold_page_differ", "page_recall_at_5",
                                                        "reproduced")},
        "index_ms": report["index"]["ms"], "permissions_passed": result["permissions"]["passed"],
        "leak_total": result["permissions"]["leak_total"]}
    if completed:
        for name, summary in result["summary"].items():
            brief[name] = {
                "page_recall_at_5": {k: f"{v['n']}/{v['of']}" for k, v in summary["page_recall_at_5"].items()},
                "mrr_at_5": summary["mrr_at_5"]["all"], "latency": summary["latency"]}
        ceiling = result["summary"]["hybrid"]["candidate_ceiling"]
        brief |= {"candidate_ceiling": {k: f"{v['n']}/{v['of']}" for k, v in ceiling.items()},
                  "changes": {k: v for k, v in result["changes"].items() if k != "top5_changed"},
                  "reranker_inference": result["latency"]["reranker_inference"],
                  "reranker_timing": result["reranker_timing"], "truncation": result["truncation"],
                  "decision": decision and {k: decision[k] for k in ("measured", "passed", "adopt")}}
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 2 if status == "BASELINE_NOT_REPRODUCED" else 0


if __name__ == "__main__":
    sys.exit(main())
