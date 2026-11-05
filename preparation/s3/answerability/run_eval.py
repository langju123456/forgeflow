"""Hybrid retrieval alone vs hybrid + answerability gate, on the new held-out set and the frozen 47.

Nothing frozen is modified. The S2 evaluator is imported unchanged and scores both modes the same way
(retrieval, citations, ACL traces, latency); the frozen S2 dataset supplies the corpus for both sets.
Settings, prompt hash and decision rule come from preregistered.json / PREREGISTRATION.md, committed
before the first real run; the runner refuses to start if the prompt or either question set changed.

    python preparation/s3/answerability/run_eval.py                       # real: local Ollama, bge-m3 + qwen3:8b
    python preparation/s3/answerability/run_eval.py --llm fake --embedder fake --out /tmp/x   # plumbing only
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
S2 = PROJECT / "preparation/s2/evaluation"
sys.path.insert(0, str(S2))
sys.path.insert(0, str(S2 / "hybrid"))

import evaluator as ev  # noqa: E402 -- frozen evaluator, imported unchanged
from run_modes import FakeEmbedder, verify_inputs  # noqa: E402

from forgeflow import answerability as ans  # noqa: E402
from forgeflow.contracts import ContextHit, DomainError, Principal, Scope  # noqa: E402
from forgeflow.retrieval import HybridRanker, OllamaEmbedder, RankedKnowledgeService, VectorRanker  # noqa: E402

PREREG = json.loads((HERE / "preregistered.json").read_text())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_heldout() -> dict:
    freeze = json.loads((HERE / "freeze.json").read_text())
    for name, expected in freeze["files"].items():
        if sha256(HERE / name) != expected:
            raise ValueError("Held-out set changed: " + name)
    if sha256(PROJECT / freeze["corpus"]["path"]) != freeze["corpus"]["sha256"]:
        raise ValueError("Corpus changed")
    if ans.prompt_sha256() != PREREG["prompt_sha256"]:
        raise ValueError("Prompt differs from the preregistered one")
    return freeze


def validate_heldout(corpus: dict, heldout: dict) -> list[dict]:
    """Gold must exist, be the current version and be readable by the question's principal."""
    identities = ev.principals(corpus)
    docs = {(d["scope"], d["id"], d["version"]): d for d in corpus["documents"]}
    current = {(d["scope"], d["id"]): d["version"] for d in corpus["documents"]}
    ids = [q["id"] for q in heldout["questions"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate question id")
    for q in heldout["questions"]:
        if q["principal"] not in identities or q["scope"] not in corpus["scopes"]:
            raise ValueError(q["id"] + ": unknown principal or scope")
        kinds = {"answerable", "unanswerable", "acl-negative"} & set(q["tags"])
        if len(kinds) != 1 or ("answerable" in kinds) != bool(q["relevant"]):
            raise ValueError(q["id"] + ": category and gold disagree")
        scope = Scope.model_validate(corpus["scopes"][q["scope"]])
        for ref in q["relevant"]:
            doc = docs[(ref["scope"], ref["id"], ref["version"])]
            if current[(ref["scope"], ref["id"])] != ref["version"] or doc["source_hash"] != ref["source_hash"]:
                raise ValueError(q["id"] + ": gold is not the current version")
            if not identities[q["principal"]].permits(scope, set(doc["roles"]), doc["classification"]):
                raise ValueError(q["id"] + ": gold unreadable by principal")
    return heldout["questions"]


class FakeChatModel:
    """Plumbing only: 'answers' when a query word of 4+ letters appears in the passage. Meaningless numbers."""

    model_id = "fake/word-overlap"

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, messages, schema):
        user = messages[-1]["content"]
        query = user.split("\n", 1)[0].removeprefix("Question: ").casefold()
        words = [w.strip("?,.") for w in query.split() if len(w.strip("?,.")) >= 4]
        verdicts = []
        for line in user.split("Passages:\n", 1)[1].splitlines():
            number, text = line[1:].split("] ", 1)
            hit = any(w in text.casefold() for w in words)
            verdicts.append({"passage": int(number), "answers": hit, "quote": text if hit else ""})
        self.calls.append({"ms": 0.0, "finish_reason": "stop", "usage": None, "reasoning_returned": False})
        return json.dumps({"verdicts": verdicts})


class RealigningGatedReader:
    """The gated service, adapted to the frozen evaluator's trace.

    The evaluator pairs hit i with the i-th citation the store recorded during that search. The gate drops
    hits, so after gating the evaluator-facing trace keeps only the citations of the kept hits, in the same
    order; that is the only adaptation. ACL accounting does not depend on it: the evaluator still counts
    every candidate row, and a separate audit store below records every row and citation unfiltered.
    """

    def __init__(self, gated: ans.GatedKnowledgeService, trace: ev.RecordingStore):
        self.gated, self.trace = gated, trace

    def search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> list[ContextHit]:
        result = self.gated.answer_search(principal, scope, query, top_k)
        citations = self.trace.citations
        if len(citations) != len(self.gated.gate.seen[-1][1]):
            raise RuntimeError("citation trace does not match the retrieved hits")
        self.trace.citations = [citations[i] for i in result.kept]
        return list(result.hits)


def readable_contents(store, identity: Principal, scope: Scope) -> set[str]:
    try:
        return {str(row["content"]) for row in store.document_rows(identity, scope)}
    except DomainError:
        return set()


def question_outcome(q: dict, r: dict, gate_results: list[ans.AnswerabilityResult] | None,
                     gate_inputs: list[tuple[ContextHit, ...]] | None = None) -> dict:
    # Scored on the first repetition, like every other evaluator metric; later-repetition errors reported apart.
    gate_error = bool(gate_results) and gate_results[0].status == "GATE_ERROR"
    out = {
        "id": q["id"], "query": q["query"], "principal": q["principal"], "tags": q["tags"],
        "gold": [ref["id"] + "@" + ref["version"] for ref in q["relevant"]],
        "returned": [h["id"] + "@" + h["version"] for h in r["hits"]],
        "error": r["error"],
        "gold_returned": r["metrics"]["relevant_found"] > 0 and not gate_error,
        "empty": not r["hits"] and not gate_error,
        "gate_error": gate_error,
        "gate_error_any_repetition": any(g.status == "GATE_ERROR" for g in gate_results or []),
        "leak": r["leak"],
        "stable": r["stable"],
        "latency_ms": r["latency_ms"],
    }
    if gate_results:
        first = gate_results[0]
        out["gate"] = {
            "status": first.status, "error": first.error, "model_called": first.model_called,
            "elapsed_ms": round(first.elapsed_ms, 1),
            "seen": [h.id + "@" + h.version for h in (gate_inputs or [()])[0]],
            "verdicts": [vars(v) for v in first.verdicts], "raw_output": first.raw_output,
            "same_across_repetitions": len({(g.status, g.kept) for g in gate_results}) == 1,
        }
    return out


def category(q: dict) -> str:
    if "unanswerable" in q["tags"]:
        return "unanswerable"
    if "acl-negative" in q["tags"]:
        return "acl-negative"
    if q["relevant"]:
        return "answerable"
    return "other"


def rate(items: list[bool]) -> dict:
    return {"numerator": sum(items), "denominator": len(items), "rate": sum(items) / len(items) if items else None}


def summarize_mode(outcomes: list[dict], questions: list[dict], frozen: bool, results: list[dict],
                   audit: dict, model_calls: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = {}
    for q, o in zip(questions, outcomes, strict=True):
        by_cat.setdefault(category(q), []).append(o)
    summary = {
        "answerable_gold_returned": rate([o["gold_returned"] for o in by_cat.get("answerable", [])]),
        "unanswerable_correctly_empty": rate([o["empty"] for o in by_cat.get("unanswerable", [])]),
        "acl_negative_correctly_empty": rate([o["empty"] for o in by_cat.get("acl-negative", [])]),
        "gate_errors": sum(o["gate_error"] for o in outcomes),
        "gate_errors_any_repetition": sum(o["gate_error_any_repetition"] for o in outcomes),
        "evaluator": {key: value for key, value in ev.summarize(results).items()
                      if key not in {"retrieval_slices"}},
        "audit": audit,
        "stable": rate([o["stable"] for o in outcomes]),
        "missed_answerable": [o["id"] for o in by_cat.get("answerable", []) if not o["gold_returned"]],
        "nonempty_unanswerable": [o["id"] for o in by_cat.get("unanswerable", []) if not o["empty"]],
    }
    reasons: dict[str, int] = {}
    for o in outcomes:
        for v in o.get("gate", {}).get("verdicts", []):
            reasons[v["reason"]] = reasons.get(v["reason"], 0) + 1
    summary["verdict_reasons_first_repetition"] = dict(sorted(reasons.items()))
    summary["gate_status_first_repetition"] = {
        status: sum(o.get("gate", {}).get("status") == status for o in outcomes)
        for status in ("SUPPORTED", "NO_ANSWER_IN_KNOWLEDGE_BASE", "GATE_ERROR")
    }
    if frozen:
        content = [o for q, o in zip(questions, outcomes, strict=True)
                   if q["relevant"] and "acl-control" not in q["tags"]]
        summary["frozen_content_gold_returned"] = rate([o["gold_returned"] for o in content])
    if model_calls:
        ms = sorted(c["ms"] for c in model_calls)
        summary["model_calls"] = {
            "n": len(ms), "median_ms": statistics.median(ms), "p95_ms": ms[max(0, (95 * len(ms) + 99) // 100 - 1)],
            "max_ms": ms[-1], "not_stopped": sum(c["finish_reason"] != "stop" for c in model_calls),
            "reasoning_returned": sum(c["reasoning_returned"] for c in model_calls),
        }
    return summary


def run_set(name: str, corpus: dict, questions: list[dict], embedder, model, scratch: Path) -> dict:
    data = dict(corpus, questions=questions)
    store, seeded = ev.seed_store(data, "sqlite:///" + str(scratch / f"{name}.db"))
    try:
        identities = ev.principals(corpus)
        audit_store = ev.RecordingStore(store)  # every row and citation, never filtered
        trace = ev.RecordingStore(audit_store)  # what the frozen evaluator reads
        vector = VectorRanker(embedder, PREREG["retrieval"]["min_similarity"])
        service = RankedKnowledgeService(trace, HybridRanker(vector, PREREG["retrieval"]["rrf_k"]))
        top_k = PREREG["retrieval"]["top_k"]
        for q in questions:  # warmup as in the frozen runner; fills the document vector cache
            try:
                service.search(identities[q["principal"]], Scope.model_validate(corpus["scopes"][q["scope"]]),
                               q["query"], top_k)
            except DomainError:
                pass
        modes = {}
        for mode in ("hybrid", "hybrid+answerability"):
            gate = ans.AnswerabilityGate(model) if mode != "hybrid" else None
            gated = ans.GatedKnowledgeService(service, gate) if gate else None
            reader = RealigningGatedReader(gated, trace) if gated else service
            run_data = dict(data, repetitions=PREREG["repetitions"][mode])
            calls_before = len(getattr(model, "calls", []))
            results, outcomes = [], []
            audit = {"rows_seen": 0, "forbidden_rows": 0, "citations_seen": 0, "forbidden_citations": 0,
                     "gate_inputs": 0, "gate_saw_unreadable_text": 0, "gate_hits_not_from_retrieval": 0}
            for q in questions:
                identity = identities[q["principal"]]
                scope = Scope.model_validate(corpus["scopes"][q["scope"]])
                audit_store.reset()
                seen_before = len(gate.seen) if gate else 0
                results_before = len(gated.results) if gated else 0
                r = ev.evaluate_question(run_data, q, store, seeded, reader, trace, identity)
                audit["rows_seen"] += len(audit_store.rows)
                audit["citations_seen"] += len(audit_store.citations)
                audit["forbidden_rows"] += sum(not ev.row_authorized(x, identity, scope) for x in audit_store.rows)
                audit["forbidden_citations"] += sum(
                    not ev.row_authorized(x, identity, scope) for x in audit_store.citations)
                gate_results = gate_inputs = None
                if gate:
                    readable = readable_contents(store, identity, scope)
                    for _, hits in gate.seen[seen_before:]:
                        audit["gate_inputs"] += len(hits)
                        audit["gate_saw_unreadable_text"] += sum(h.content not in readable for h in hits)
                    gate_results = gated.results[results_before:]
                    gate_inputs = [hits for _, hits in gate.seen[seen_before:]]
                    for g, hits in zip(gate_results, gate_inputs, strict=True):
                        audit["gate_hits_not_from_retrieval"] += sum(
                            not any(h is s for s in hits) for h in g.hits)
                results.append(r)
                outcomes.append(question_outcome(q, r, gate_results, gate_inputs))
            calls = list(getattr(model, "calls", []))[calls_before:] if gate else []
            modes[mode] = {"summary": summarize_mode(outcomes, questions, name == "frozen47", results, audit, calls),
                           "questions": outcomes}
        return {"questions": len(questions), "texts_sent_to_embedder": len(vector.embedded_texts), "modes": modes}
    finally:
        store.engine.dispose()


def decide(heldout: dict, frozen: dict | None) -> dict:
    rule = PREREG["decision_rule"]
    hy, gt = heldout["modes"]["hybrid"]["summary"], heldout["modes"]["hybrid+answerability"]["summary"]
    leaks = []
    for result in filter(None, (heldout, frozen)):
        for mode in result["modes"].values():
            s = mode["summary"]
            leaks.append(s["evaluator"]["all_query_leak"]["numerator"] + s["evaluator"]["forbidden_candidate_rows"]
                         + s["evaluator"]["forbidden_citation_rows"] + s["audit"]["forbidden_rows"]
                         + s["audit"]["forbidden_citations"] + s["audit"]["gate_saw_unreadable_text"]
                         + s["audit"]["gate_hits_not_from_retrieval"])
    checks = {
        "acl_zero": sum(leaks) == 0,
        "unanswerable_improves": gt["unanswerable_correctly_empty"]["numerator"]
        >= hy["unanswerable_correctly_empty"]["numerator"] + rule["min_unanswerable_gain_questions"],
        "answerable_drop_within_margin": gt["answerable_gold_returned"]["numerator"]
        >= hy["answerable_gold_returned"]["numerator"] - rule["max_answerable_drop_questions"],
    }
    return {"rule": rule, "checks": checks, "adopt_gate": all(checks.values()), "frozen_included": frozen is not None}


def ollama_detail(base_url: str) -> dict:
    import httpx

    with httpx.Client(base_url=base_url, trust_env=False, timeout=10) as client:
        version = client.get("/api/version").json()
        models = client.get("/api/tags").json().get("models", [])
    wanted = {PREREG["model"]["name"], PREREG["retrieval"]["embedder"] + ":latest"}
    return {"ollama_version": version.get("version"),
            "models": [{k: m.get(k) for k in ("name", "digest", "size", "details")} for m in models
                       if m.get("name") in wanted]}


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--llm", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--embedder", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--sets", nargs="+", choices=["heldout", "frozen47"], default=["heldout", "frozen47"])
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    args = parser.parse_args()

    inputs = {"frozen_s2": verify_inputs(), "heldout_freeze": verify_heldout()}
    corpus = ev.validate_dataset(json.loads((S2 / "dataset.json").read_text()))
    heldout = validate_heldout(corpus, json.loads((HERE / "heldout.json").read_text()))
    real = args.llm == "ollama" and args.embedder == "ollama"
    embedder = OllamaEmbedder(PREREG["retrieval"]["embedder"], args.ollama_url) if args.embedder == "ollama" \
        else FakeEmbedder()
    if args.llm == "ollama":
        model = ans.OllamaChatModel(PREREG["model"]["name"], args.ollama_url,
                                    max_tokens=PREREG["model"]["max_tokens"])
        if model.settings() != PREREG["model"]["settings"]:
            raise SystemExit("model settings differ from the preregistered ones")
        # Load the model once with an unrelated prompt so the first question does not pay the load time.
        warm = [ContextHit(id="warm", version="1", content="The test bench is painted blue.",
                           source_hash="0" * 64, classification=0, status="CURRENT")]
        ans.AnswerabilityGate(model).check("What colour is the test bench?", warm)
        model.calls.clear()
    else:
        model = FakeChatModel()

    label = "real" if real else "fake"
    output = args.out / f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{label}-{uuid4().hex[:8]}"
    output.mkdir(parents=True)
    sets = {}
    with tempfile.TemporaryDirectory(prefix="sqlite-", dir=output) as scratch:
        if "heldout" in args.sets:
            sets["heldout"] = run_set("heldout", corpus, heldout, embedder, model, Path(scratch))
        if "frozen47" in args.sets:
            sets["frozen47"] = run_set("frozen47", corpus, corpus["questions"], embedder, model, Path(scratch))
    end_inputs = {"frozen_s2": verify_inputs(), "heldout_freeze": verify_heldout()}
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "REAL_SYNTHETIC" if real else "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS",
        "git_head": git_head(),
        "preregistered": PREREG,
        "prompt_sha256": ans.prompt_sha256(),
        "code_sha256": {name: sha256(PROJECT / name) for name in
                        ["src/forgeflow/answerability.py", "src/forgeflow/retrieval.py",
                         "preparation/s3/answerability/run_eval.py"]},
        "llm": model.model_id,
        "embedder": embedder.model_id,
        "ollama": ollama_detail(args.ollama_url) if args.llm == "ollama" or args.embedder == "ollama" else None,
        "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                        "packages": {n: importlib.metadata.version(n) for n in ["pydantic", "sqlalchemy", "httpx"]}},
        "inputs_verified": inputs,
        "inputs_unchanged_after": end_inputs == inputs,
        "decision": decide(sets["heldout"], sets.get("frozen47")) if "heldout" in sets else None,
        "sets": sets,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    brief = {"output": str(output), "decision": report["decision"]}
    for set_name, result in sets.items():
        for mode, body in result["modes"].items():
            s = body["summary"]
            brief[f"{set_name}/{mode}"] = {
                "answerable": f'{s["answerable_gold_returned"]["numerator"]}/{s["answerable_gold_returned"]["denominator"]}',
                "unanswerable_empty": f'{s["unanswerable_correctly_empty"]["numerator"]}/'
                                      f'{s["unanswerable_correctly_empty"]["denominator"]}',
                "acl_negative_empty": f'{s["acl_negative_correctly_empty"]["numerator"]}/'
                                      f'{s["acl_negative_correctly_empty"]["denominator"]}',
                "leak": s["evaluator"]["all_query_leak"]["numerator"], "gate_errors": s["gate_errors"],
                "latency_median_ms": s["evaluator"]["latency_search_including_service_citation"]["median_ms"],
            }
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
