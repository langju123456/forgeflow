"""Shared by the two rerank runners: run_guardrail.py here and preparation/s4/real-reports/rerank/run_rerank.py.

The registered reranker settings (which must be identical in both registrations), a wrapper that times
the reranker and keeps what it was sent, a fake reranker for plumbing runs, and a description of the model
that was actually loaded.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import statistics
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[3]
REGISTRATIONS = {
    "s2": PROJECT / "preparation/s2/evaluation/rerank/preregistered.json",
    "s4": PROJECT / "preparation/s4/real-reports/rerank/preregistered.json",
}
WARMUP = ("What colour is the test bench?", "The test bench is painted blue.")  # unrelated to either corpus


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registered_reranker() -> dict:
    """The reranker settings. Both registrations state them; a difference means one was edited: do not run."""
    blocks = {name: json.loads(path.read_text())["reranker"] for name, path in REGISTRATIONS.items()}
    if blocks["s2"] != blocks["s4"]:
        raise SystemExit("the two preregistered.json files disagree on the reranker settings")
    return blocks["s2"]


def package_versions(names: list[str]) -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class FakeReranker:
    """Plumbing only: counts character trigrams shared by query and passage. No semantics; numbers meaningless."""

    model_id = "fake/trigram-overlap"

    @staticmethod
    def _grams(text: str) -> set[str]:
        folded = text.casefold()
        return {folded[i:i + 3] for i in range(len(folded) - 2)}

    def score(self, query, passages):
        wanted = self._grams(query)
        return [float(len(wanted & self._grams(passage))) for passage in passages]


class TimedReranker:
    """Passes calls through and keeps, per call, the query, the passages, the scores and the time taken."""

    def __init__(self, inner):
        self.inner = inner
        self.model_id = inner.model_id
        self.calls: list[dict] = []

    def score(self, query, passages):
        began = time.perf_counter()
        scores = [float(value) for value in self.inner.score(query, passages)]
        self.calls.append({"query": query, "passages": list(passages), "scores": scores,
                           "ms": (time.perf_counter() - began) * 1000})
        return scores


def build_reranker(kind: str, settings: dict, local_files_only: bool = False):
    """The registered cross-encoder, or the fake one for plumbing runs."""
    if kind == "fake":
        return FakeReranker()
    from forgeflow.retrieval import CrossEncoderReranker

    return CrossEncoderReranker(settings["model"], device=settings["device"], max_length=settings["max_length"],
                                batch_size=settings["batch_size"], local_files_only=local_files_only)


def load_and_warm(reranker) -> dict:
    """Load the model and score one pair unrelated to any corpus; neither time counts as question latency."""
    began = time.perf_counter()
    if hasattr(reranker, "load"):
        reranker.load()
    loaded = time.perf_counter()
    reranker.score(WARMUP[0], [WARMUP[1]])
    return {"load_ms": round((loaded - began) * 1000, 1), "warmup_ms": round((time.perf_counter() - loaded) * 1000, 1)}


def reranker_detail(reranker) -> dict:
    """What was loaded: library versions, device, dtype, activation, and the snapshot in the local hub cache."""
    detail: dict = {"model_id": reranker.model_id}
    if not hasattr(reranker, "load"):
        return detail
    encoder = reranker.load()
    detail |= {
        "device": str(reranker.device), "max_length": reranker.max_length, "batch_size": reranker.batch_size,
        "max_seq_length": getattr(encoder, "max_seq_length", None),
        "activation_fn": type(getattr(encoder, "activation_fn", None)).__name__,
        "dtype": str(next(encoder.parameters()).dtype).removeprefix("torch."),
        "packages": package_versions(["sentence-transformers", "transformers", "torch", "tokenizers",
                                      "huggingface-hub"]),
    }
    try:  # the commit the weights came from: the snapshot folder name in the local hub cache
        from huggingface_hub import snapshot_download

        snapshot = Path(snapshot_download(reranker.model_id.split("/", 1)[1], local_files_only=True))
        weights = snapshot / "model.safetensors"
        detail["snapshot"] = {"commit": snapshot.name,
                              "weights_sha256": sha256(weights) if weights.is_file() else None}
    except Exception as error:  # a local folder or an unusual cache layout: recorded, not fatal
        detail["snapshot"] = {"error": f"{type(error).__name__}: {error}"[:300]}
    return detail


def settings_mismatch(detail: dict, settings: dict) -> list[str]:
    """Where the loaded cross-encoder differs from the registered settings (empty when it matches)."""
    expected = {"device": settings["device"], "dtype": settings["dtype"], "max_length": settings["max_length"],
                "max_seq_length": settings["max_length"], "batch_size": settings["batch_size"],
                "activation_fn": "Identity", "model_id": f"sentence-transformers/{settings['model']}"}
    return [f"{key}: loaded {detail.get(key)!r}, registered {value!r}" for key, value in expected.items()
            if detail.get(key) != value]


def truncation(reranker, calls: list[dict]) -> dict:
    """How many (query, passage) pairs were longer than max_length tokens and therefore cut by the tokenizer."""
    pairs = [(call["query"], passage) for call in calls for passage in call["passages"]]
    if not hasattr(reranker, "load"):
        return {"pairs": len(pairs), "over_max_length": None}
    try:
        tokenizer = reranker.load().tokenizer
        lengths = [len(tokenizer(query, passage, truncation=False)["input_ids"]) for query, passage in pairs]
    except Exception as error:  # diagnostics only: never lose a finished run to it
        return {"pairs": len(pairs), "over_max_length": None, "error": f"{type(error).__name__}: {error}"[:300]}
    return {"pairs": len(pairs), "max_length": reranker.max_length,
            "over_max_length": sum(length > reranker.max_length for length in lengths),
            "median_tokens": statistics.median(lengths) if lengths else None,
            "max_tokens": max(lengths) if lengths else None}
