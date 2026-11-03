"""Run the frozen S2 retrieval set with lexical, vector and hybrid ranking; nothing frozen is modified.

The frozen evaluator (../evaluator.py) is imported unchanged: same seeding, same per-question checks,
same summary. Only the reader differs: forgeflow.retrieval.RankedKnowledgeService with one ranker per
mode. Settings come from PREREGISTRATION.md, fixed before the first vector run.

    python preparation/s2/evaluation/hybrid/run_modes.py --modes lexical vector hybrid
    python preparation/s2/evaluation/hybrid/run_modes.py --embedder fake   # mechanics only, numbers meaningless
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import statistics
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import evaluator as ev  # noqa: E402 -- frozen evaluator, imported unchanged

from forgeflow.contracts import DomainError, Scope  # noqa: E402
from forgeflow.retrieval import (  # noqa: E402
    HybridRanker,
    OllamaEmbedder,
    RankedKnowledgeService,
    VectorRanker,
    lexical_ranker,
)

PREREGISTERED = {"model": "bge-m3", "min_similarity": 0.50, "rrf_k": 60, "top_k": 5}
SLICES = ["synonym", "cross-language", "exact-token", "chinese", "english", "unanswerable", "acl-negative"]


class FakeEmbedder:
    """Character-trigram hashing. Deterministic, no semantics: only for exercising the plumbing."""

    model_id = "fake/trigram-hash-64"

    def embed(self, texts):
        out = []
        for text in texts:
            vector = [0.0] * 64
            padded = f"  {text.casefold()}  "
            for i in range(len(padded) - 2):
                vector[int(hashlib.sha256(padded[i : i + 3].encode()).hexdigest(), 16) % 64] += 1.0
            out.append(vector)
        return out


SEALED_BASELINE = ev.ROOT / "runs" / "20261005T170825-lexical-v1-891375a5" / "questions.jsonl"


def verify_inputs() -> dict:
    """The frozen benchmark must be byte-identical; the sealed S1 code is described, not assumed.

    ev.verify_freeze() refuses to run on today's master for two reasons, both recorded here instead:
    8 of the 95 sealed files are the owner's original materials under sources/ (gitignored, absent from
    any clone), and 9 sealed files changed after 2026-10-05. In the retrieval path those changes are a
    clock source (datetime.now -> contracts.utcnow) and new fixture ids; ranking, ACL and citation code
    did not change. That claim is checked empirically: the lexical mode must reproduce the sealed
    2026-10-05 run question by question (see lexical_parity), or no vector/hybrid number is reported.
    """
    freeze = json.loads((ev.ROOT / "freeze.json").read_text())
    if freeze["protocol_version"] != ev.PROTOCOL_VERSION:
        raise ValueError("Frozen protocol version mismatch")
    for name, expected in freeze["files"].items():
        if ev.sha256(ev.ROOT / name) != expected:
            raise ValueError("Frozen benchmark changed: " + name)
    if ev.sha256(ev.S1_MANIFEST) != freeze["protected_s1"]["manifest_sha256"]:
        raise ValueError("Sealed S1 manifest changed")
    manifest = json.loads(ev.S1_MANIFEST.read_text())
    absent = [row["path"] for row in manifest["files"] if not (ev.PROJECT / row["path"]).exists()]
    changed = [row["path"] for row in manifest["files"]
               if row["path"] not in absent and ev.sha256(ev.PROJECT / row["path"]) != row["sha256"]]
    return {"frozen_benchmark_files": freeze["files"], "s1_files_total": len(manifest["files"]),
            "s1_absent_from_clone": absent, "s1_changed_since_seal": changed}


def _hit_keys(result: dict) -> list:
    return [(h["id"], h["version"], h["source_hash"], h["status"]) for h in result["hits"]]


def lexical_parity(results: list[dict]) -> dict:
    sealed = {r["id"]: r for r in map(json.loads, SEALED_BASELINE.read_text().splitlines())}
    differ = [r["id"] for r in results if _hit_keys(r) != _hit_keys(sealed[r["id"]]) or r["error"] != sealed[r["id"]]["error"]]
    return {"against": str(SEALED_BASELINE.relative_to(ev.PROJECT)), "questions": len(results), "differ": differ}


def slice_summary(results: list[dict]) -> dict:
    by_tag = {}
    for tag in SLICES:
        rows = [r for r in results if tag in r["tags"]]
        positives = [r for r in rows if r["metrics"]["relevant_total"]]
        by_tag[tag] = {
            "questions": len(rows),
            "positives_fully_recalled": ev.rate(
                sum(r["metrics"]["relevant_found"] == r["metrics"]["relevant_total"] for r in positives), len(positives)
            ),
            "macro_recall_at_5": statistics.mean(r["metrics"]["recall_at_5"] for r in positives) if positives else None,
            "nonempty_context": ev.rate(sum(bool(r["hits"]) for r in rows), len(rows)),
        }
    return by_tag


def similarity_trace(data, store, identities, vector: VectorRanker) -> list[dict]:
    """Raw cosine scores for every authorized row, per question, for the exploratory floor analysis."""
    from forgeflow.context import _active_rows

    out = []
    for q in data["questions"]:
        scope = Scope.model_validate(data["scopes"][q["scope"]])
        try:
            rows = _active_rows(store.document_rows(identities[q["principal"]], scope), identities[q["principal"]],
                                scope, "document")
        except DomainError:
            continue
        gold = {(r["id"], str(r["version"])) for r in q["relevant"]}
        out.append({
            "id": q["id"], "tags": q["tags"], "query": q["query"],
            "scores": [
                {"id": row["id"], "version": str(row.get("version", "1")), "cosine": round(score, 4),
                 "relevant": (row["id"], str(row.get("version", "1"))) in gold}
                for score, row in vector.similarities(q["query"], rows)
            ],
        })
    return out


def model_detail(embedder) -> dict | None:
    """Name, digest and size of the Ollama model actually used, so the run can be repeated exactly."""
    if not isinstance(embedder, OllamaEmbedder):
        return None
    for model in embedder._client.get("/api/tags").json().get("models", []):
        if model.get("name", "").split(":")[0] == PREREGISTERED["model"]:
            return {key: model.get(key) for key in ("name", "digest", "size", "details")}
    return {"error": "model not listed by /api/tags"}


def run(mode: str, embedder, label: str) -> Path:
    inputs = verify_inputs()
    data = ev.validate_dataset(json.loads((ev.ROOT / "dataset.json").read_text()))
    output = HERE / "runs" / f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{label}-{mode}-{uuid4().hex[:8]}"
    output.mkdir(parents=True)
    vector = VectorRanker(embedder, PREREGISTERED["min_similarity"]) if embedder else None
    ranker = {"lexical": lexical_ranker, "vector": vector, "hybrid": HybridRanker(vector, PREREGISTERED["rrf_k"])
              if vector else None}[mode]
    if ranker is None:
        raise SystemExit(f"mode {mode} needs an embedder")
    with tempfile.TemporaryDirectory(prefix="sqlite-", dir=output) as scratch:
        store, seeded = ev.seed_store(data, "sqlite:///" + str(Path(scratch) / "evaluation.db"))
        trace = ev.RecordingStore(store)
        reader = RankedKnowledgeService(trace, ranker)
        identities = ev.principals(data)
        for q in data["questions"]:  # warmup, as in the frozen runner; it also fills the vector cache
            try:
                reader.search(identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]]),
                              q["query"], PREREGISTERED["top_k"])
            except DomainError:
                pass
        results = [ev.evaluate_question(data, q, store, seeded, reader, trace, identities[q["principal"]])
                   for q in data["questions"]]
        if vector and mode == "vector":
            (output / "similarities.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in similarity_trace(data, store, identities, vector)))
        store.engine.dispose()
    end_inputs = verify_inputs()
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "FAKE_EMBEDDER_PLUMBING_ONLY" if label == "fake" else f"REAL_{mode.upper()}_SYNTHETIC",
        "mode": mode,
        "preregistered": PREREGISTERED,
        "embedder": getattr(embedder, "model_id", None),
        "embedder_model_detail": model_detail(embedder),
        "dataset_id": data["id"],
        "freeze_sha256": ev.sha256(ev.ROOT / "freeze.json"),
        "inputs_verified": inputs,
        "lexical_parity_with_sealed_run": lexical_parity(results) if mode == "lexical" else None,
        "environment": {
            "python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "packages": {name: importlib.metadata.version(name) for name in ["pydantic", "sqlalchemy", "httpx"]},
            "store": "ephemeral fresh SQLite; synthetic fixture principals",
            "retriever": f"RankedKnowledgeService({mode}); S1 ACL and citation checks; embeddings local only",
        },
        "metrics": ev.summarize(results),
        "slices": slice_summary(results),
        "questions": results,
        "texts_sent_to_embedder": len(vector.embedded_texts) if vector else 0,
        "benchmark_and_present_s1_unchanged_after": end_inputs == inputs,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--modes", nargs="+", default=["lexical", "vector", "hybrid"],
                        choices=["lexical", "vector", "hybrid"])
    parser.add_argument("--embedder", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    embedder = FakeEmbedder() if args.embedder == "fake" else OllamaEmbedder(PREREGISTERED["model"], args.ollama_url)
    label = "fake" if args.embedder == "fake" else "real"
    for mode in args.modes:
        output = run(mode, embedder, label)
        report = json.loads((output / "report.json").read_text())
        m, s = report["metrics"], report["slices"]
        print(json.dumps({
            "mode": mode, "output": str(output.relative_to(ev.PROJECT)),
            "macro_recall_at_5": round(m["macro_recall_at_5"], 4),
            "core_content_recall_at_5": round(m["core_content_recall_at_5"], 4),
            "synonym": s["synonym"]["positives_fully_recalled"], "cross_language": s["cross-language"]["positives_fully_recalled"],
            "exact_token": s["exact-token"]["positives_fully_recalled"],
            "unanswerable_nonempty": m["unanswerable_nonempty_context"], "all_query_leak": m["all_query_leak"],
            "forbidden_candidates": m["forbidden_candidate_rows"],
            "latency_ms": m["latency_search_including_service_citation"],
            "inputs_unchanged": report["benchmark_and_present_s1_unchanged_after"],
            "parity_differ": (report["lexical_parity_with_sealed_run"] or {}).get("differ"),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
