"""The desk service on the real annual reports with the real local model: a smoke run with timings.

    .venv-mac/bin/python preparation/s5/desk-service/run_real.py --reports ~/code/finreports

Starts the retrieval server, one worker and the gateway as child processes (Redis and RabbitMQ must already run,
see scripts/mac_desk_service_run.sh), waits until the index is ready, then:

1. asks four questions one after another (the 年报问答 page's examples, not the frozen evaluation questions) and
   records, from the client side, when the first draft token and the checked answer arrived;
2. asks the first one again (cache);
3. sends one new question three times at once (join).

Writes real-model-run.json next to this file. It is a smoke run of the serving path with the real model, not an
accuracy evaluation: answer quality is measured in preparation/s4/real-reports.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
QUESTIONS = [
    "宁德时代 2025 年经营活动产生的现金流量净额是多少？",
    "茅台 2025 年直销渠道的营业收入是多少？",
    "What was Apple's products gross margin percentage in fiscal 2025?",
    "腾讯 2025 年的毛利是多少？",
]
JOIN_QUESTION = "茅台 2025 年在日本的销售收入是多少？"


def follow(base: str, question: str) -> dict:
    started = time.perf_counter()
    accepted = httpx.post(f"{base}/v2/reports/ask", json={"question": question}, timeout=30, trust_env=False)
    accepted.raise_for_status()
    body = accepted.json()
    first_token = final = None
    events: list[str] = []
    with httpx.stream("GET", f"{base}{body['events']}", timeout=900, trust_env=False) as response:
        kind = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                kind = line[7:]
                events.append(kind)
                if kind == "token" and first_token is None:
                    first_token = (time.perf_counter() - started) * 1000
            elif line.startswith("data: ") and kind in {"final", "error"}:
                final = json.loads(line[6:])
                break
    total = (time.perf_counter() - started) * 1000
    return {"question": question, "state": body["state"], "job_id": body["job_id"],
            "client_first_token_ms": None if first_token is None else round(first_token, 1),
            "client_total_ms": round(total, 1), "event_counts": {k: events.count(k) for k in sorted(set(events))},
            "status": (final or {}).get("status") or (final or {}).get("code"), "found": (final or {}).get("found"),
            "answer": (final or {}).get("answer"),
            "citations": [{"company": c.get("company"), "page": c.get("page")} for c in (final or {}).get("citations", [])],
            "server_timings": (final or {}).get("timings"), "attempt": (final or {}).get("attempt")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports", default=str(Path.home() / "code" / "finreports"))
    parser.add_argument("--model", default="forgeflow-qwen3-8b-ctx16k")
    parser.add_argument("--ready-minutes", type=float, default=40)
    args = parser.parse_args()
    env = {**os.environ, "FORGEFLOW_DESK_REPORTS_DIR": args.reports, "FORGEFLOW_DESK_MODEL": args.model,
           "FORGEFLOW_DESK_PREFETCH": "1", "PYTHONUNBUFFERED": "1"}
    base = f"http://127.0.0.1:{env.get('FORGEFLOW_DESK_GATEWAY_PORT', '8790')}"
    command = [sys.executable, "-m", "forgeflow.desk_service"]
    log = open(HERE / "real-model-run.log", "w")  # noqa: SIM115 - children write to it until the end
    children = [subprocess.Popen([*command, part], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
                for part in ("retrieval", "worker", "gateway")]
    out: dict = {"run": {"started_at": time.strftime("%Y-%m-%d %H:%M:%S %z"), "model": args.model,
                         "machine": f"{platform.node()} {platform.machine()} {platform.mac_ver()[0]}",
                         "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                                  capture_output=True, text=True).stdout.strip(),
                         "workers": 1, "prefetch": 1}}
    try:
        started = time.monotonic()
        info: dict = {}
        while time.monotonic() - started < args.ready_minutes * 60:
            try:
                info = httpx.get(f"{base}/v2/reports", timeout=10, trust_env=False).json()
                if info.get("status", {}).get("state") == "ready" and info.get("queue", {}).get("workers"):
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(5)
        else:
            raise SystemExit(f"service not ready after {args.ready_minutes} min: {info}")
        out["ready_after_s"] = round(time.monotonic() - started, 1)
        out["index"] = {"mode": info.get("mode"), "rows": info["status"].get("rows"),
                        "index_version": info.get("index_version"), "gate_available": info.get("gate_available")}
        print(f"ready in {out['ready_after_s']} s: {out['index']}", flush=True)
        out["sequential"] = []
        for question in QUESTIONS:
            result = follow(base, question)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            out["sequential"].append(result)
        out["cached_repeat"] = follow(base, QUESTIONS[0])
        print(json.dumps(out["cached_repeat"], ensure_ascii=False), flush=True)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            out["join"] = list(pool.map(lambda _: follow(base, JOIN_QUESTION), range(3)))
        print(json.dumps(out["join"], ensure_ascii=False), flush=True)
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=40)
            except subprocess.TimeoutExpired:
                child.kill()
        log.close()
        (HERE / "real-model-run.json").write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
