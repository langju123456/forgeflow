"""Measure the desk service's serving layer: cache, joining, bounded concurrency, crash recovery, backpressure.

    FORGEFLOW_SERVICE_LIVE=1 .venv/bin/python scripts/desk_service_bench.py --out preparation/s5/desk-service

Real Redis, RabbitMQ, gRPC and HTTP on loopback (tests/desk_service_harness.py); the model is the stand-in from
forgeflow.desk_service.testing with a fixed per-chunk delay, so the numbers describe the serving layer only:
how long a cached answer takes against a computed one, how many model calls duplicates cost, how many model calls
run at once, what happens when a worker dies mid-answer and when the queue is full. They say nothing about answer
quality, and a real local model makes every "computed" latency far longer (seconds to tens of seconds).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from desk_service_harness import Harness  # noqa: E402


def timed_answer(harness: Harness, question: str) -> tuple[float, dict]:
    started = time.perf_counter()
    reply = harness.ask(question).json()
    events = harness.events(reply["job_id"])
    return (time.perf_counter() - started) * 1000, {"state": reply["state"], "final": events[-1]}


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))], 1)


def up(tmp: Path, **kwargs) -> Harness:
    harness = Harness(tmp, **kwargs)
    harness.start_model()
    harness.start_retrieval()
    harness.start_gateway()
    return harness


def cache_and_join(tmp: Path, n: int) -> dict:
    harness = up(tmp)
    try:
        harness.start_workers(1)
        misses = [timed_answer(harness, f"total net sales {i}")[0] for i in range(n)]
        hits = []
        for i in range(n):
            ms, info = timed_answer(harness, f"total net sales {i}")
            assert info["state"] == "cached"
            hits.append(ms)
        calls_before = harness.model.calls
        harness.model.chunk_delay_s = 0.05
        with concurrent.futures.ThreadPoolExecutor(n) as pool:
            replies = [r.json() for r in pool.map(lambda _: harness.ask("operating income"), range(n))]
        harness.events(replies[0]["job_id"])
        return {
            "requests": n,
            "computed_ms": {"median": round(statistics.median(misses), 1), "p95": pct(misses, 0.95)},
            "cached_ms": {"median": round(statistics.median(hits), 1), "p95": pct(hits, 0.95)},
            "model_calls_for_cached": 0,
            "identical_concurrent": {"requests": n, "jobs": len({r["job_id"] for r in replies}),
                                     "joined": sum(r["state"] == "joined" for r in replies),
                                     "model_calls": harness.model.calls - calls_before},
        }
    finally:
        harness.close()


def concurrency(tmp: Path, workers: int, prefetch: int, n: int) -> dict:
    harness = up(tmp, prefetch=prefetch)
    harness.model.chunk_delay_s = 0.05
    try:
        harness.start_workers(workers)
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(n) as pool:
            jobs = [r.json()["job_id"] for r in pool.map(lambda i: harness.ask(f"revenue question {i}"), range(n))]
            finals = list(pool.map(lambda j: harness.events(j)[-1]["data"], jobs))
        wall = (time.perf_counter() - started) * 1000
        queue_ms = [f["timings"].get("queue_ms", 0.0) for f in finals]
        return {"workers": workers, "prefetch": prefetch, "questions": n,
                "peak_concurrent_model_calls": harness.model.peak_concurrent, "wall_ms": round(wall, 1),
                "queue_ms": {"median": round(statistics.median(queue_ms), 1), "max": round(max(queue_ms), 1)},
                "all_answered": all(f.get("status") == "ANSWERED" for f in finals)}
    finally:
        harness.close()


def crash(tmp: Path) -> dict:
    harness = up(tmp)
    harness.model.chunk_delay_s = 0.4
    try:
        doomed = harness.start_worker_process()
        job_id = harness.ask("total net sales").json()["job_id"]
        while harness.model.calls == 0:
            time.sleep(0.05)
        time.sleep(0.5)
        killed_at = time.perf_counter()
        doomed.kill()
        doomed.wait()
        harness.model.chunk_delay_s = 0.01
        harness.start_workers(1)
        events = harness.events(job_id)
        return {"killed": "SIGKILL during the answer stream", "final_status": events[-1]["data"].get("status"),
                "deliveries": harness.job(job_id)["deliveries"],
                "workers_seen": sorted({e["data"]["worker"] for e in events if e["type"] == "started"}).__len__(),
                "kill_to_final_ms": round((time.perf_counter() - killed_at) * 1000, 1),
                "model_calls": harness.model.calls}
    finally:
        harness.close()


def backpressure(tmp: Path) -> dict:
    harness = up(tmp, max_queue=5)
    try:
        codes = [harness.ask(f"question number {i}").status_code for i in range(10)]
        return {"max_queue": 5, "requests": 10, "accepted_202": codes.count(202), "refused_429": codes.count(429),
                "waiting_in_queue": harness.queue_state()[0]}
    finally:
        harness.close()


def outage(tmp: Path) -> dict:
    harness = up(tmp, retry_delay_ms=300)
    try:
        harness.start_workers(1)
        harness.model.down_calls = 2
        events = harness.events(harness.ask("total net sales").json()["job_id"])
        recovered = {"model_503_calls": 2, "attempts": max(e["data"]["attempt"] for e in events),
                     "final_status": events[-1]["data"].get("status")}
        harness.model.mode = "down"
        events = harness.events(harness.ask("operating income").json()["job_id"])
        time.sleep(0.5)
        gave_up = {"attempts": max(e["data"]["attempt"] for e in events), "final_event": events[-1]["type"],
                   "code": events[-1]["data"].get("code"),
                   "dead_letter_messages": harness.queue_state(harness.config.dead_queue)[0]}
        return {"recovers": recovered, "stays_down": gave_up}
    finally:
        harness.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ROOT / "preparation" / "s5" / "desk-service")
    parser.add_argument("-n", type=int, default=20)
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="desk-bench-"))
    started = time.time()
    results = {
        "cache_and_join": cache_and_join(tmp / "a", args.n),
        "concurrency": [concurrency(tmp / f"c{w}{p}", w, p, 12) for w, p in ((1, 1), (1, 2), (2, 2))],
        "crash_recovery": crash(tmp / "k"),
        "backpressure": backpressure(tmp / "b"),
        "transient_failures": outage(tmp / "o"),
    }
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    results["run"] = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(started)),
                      "commit": commit.stdout.strip(), "python": platform.python_version(),
                      "machine": f"{platform.system()} {platform.machine()}",
                      "model": "stand-in (forgeflow.desk_service.testing), not a real model"}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bench.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
