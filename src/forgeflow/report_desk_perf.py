"""Small, reproducible local timings for the annual-report desk.

This deliberately measures only PDF ingestion, lexical retrieval and the in-process
``ReportDesk.ask`` boundary.  It does not start the Redis/RabbitMQ/gRPC serving
topology and it does not call a chat or embedding model, so its numbers are not a
production-capacity claim.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import math
import time
from collections.abc import Callable, Sequence
from typing import Any


def percentile_ms(samples: Sequence[float], percentile: float) -> float:
    """Nearest-rank percentile, useful for the deliberately small local samples."""
    if not samples:
        raise ValueError("at least one timing is required")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(samples)
    return round(ordered[math.ceil(len(ordered) * percentile / 100) - 1], 1)


def _call(ask: Callable[[str], dict[str, Any]], question: str) -> dict[str, Any]:
    started = time.perf_counter()
    result = ask(question)
    elapsed = (time.perf_counter() - started) * 1000
    if result.get("question") != question:
        raise RuntimeError("request/result question mismatch; concurrent result isolation failed")
    return {
        "question": question,
        "wall_ms": round(elapsed, 1),
        "status": result.get("status"),
        "found": bool(result.get("found")),
        "citation_ids": [str(item.get("id")) for item in result.get("citations", [])],
        "kept_ids": [str(item.get("id")) for item in result.get("kept", [])],
        "answer_sha256": hashlib.sha256(str(result.get("answer", "")).encode()).hexdigest(),
    }


def _summary(records: Sequence[dict[str, Any]]) -> dict[str, float | int]:
    samples = [float(row["wall_ms"]) for row in records]
    return {"requests": len(records), "p50_wall_ms": percentile_ms(samples, 50),
            "p95_wall_ms": percentile_ms(samples, 95), "max_wall_ms": round(max(samples), 1)}


def run_query_plan(ask: Callable[[str], dict[str, Any]], questions: Sequence[str], *, repeats: int = 3,
                   workers: int = 3) -> dict[str, Any]:
    """Measure same question, different questions, and an in-process concurrent burst.

    Every returned record carries the request text and returned citation IDs.  Exact
    request text is asserted at the call boundary; the archive therefore catches a
    response accidentally crossing from one concurrent request to another.
    """
    if not questions:
        raise ValueError("at least one question is required")
    if repeats < 2 or workers < 2:
        raise ValueError("repeats and workers must both be at least two")
    same = [_call(ask, questions[0]) for _ in range(repeats)]
    different = [_call(ask, question) for question in questions]
    burst_questions = [questions[index % len(questions)] for index in range(workers)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        burst = list(pool.map(lambda question: _call(ask, question), burst_questions))
    expected = {row["question"]: (row["status"], row["found"], row["citation_ids"], row["kept_ids"],
                                   row["answer_sha256"]) for row in different}
    for row in burst:
        observed = (row["status"], row["found"], row["citation_ids"], row["kept_ids"], row["answer_sha256"])
        if observed != expected[row["question"]]:
            raise RuntimeError("concurrent response differs from its serial same-question baseline")
    return {
        "same_question": {"records": same, "summary": _summary(same)},
        "different_questions": {"records": different, "summary": _summary(different)},
        "concurrent_in_process": {"workers": workers, "records": burst, "summary": _summary(burst)},
    }
