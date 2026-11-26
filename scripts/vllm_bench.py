#!/usr/bin/env python3
"""Serve a model with vLLM on this machine's GPUs and measure the service: TTFT, TPOT, throughput and
p50 / p95 at concurrency 1 to 16, plus GPU memory. Standard library only; vLLM is the one thing to install.

    python scripts/vllm_bench.py --model Qwen/Qwen3-8B --model Qwen/Qwen3-8B-AWQ --model Qwen/Qwen3-4B

The models are candidates in order of preference: the first one that fits and starts is measured, the others
are recorded as skipped or failed. On a 16 GB T4 the fp16 weights of Qwen3-8B (about 16 GB) do not fit on one
card, so with `--tensor-parallel-size auto` (the default) it is spread over two T4s when the notebook has
them, and otherwise the next candidate (a quantized build or a smaller model) is used. On Kaggle run it
through scripts/kaggle_vllm_bench.sh, which installs vLLM first and packs the results.

What is measured, per concurrency level, by vLLM's own `vllm bench serve` (random-token prompts of a fixed
length, a fixed number of output tokens with EOS ignored, all requests released at once and capped at the
level's concurrency):
  TTFT  time from sending a request to its first streamed token
  TPOT  time per output token after the first: (end-to-end - TTFT) / (output tokens - 1)
  throughput  output tokens per second and requests per second over the whole level
If that command cannot run (other flags in this version, a missing extra), `--tool auto` falls back to the
built-in client in this file, which streams /v1/chat/completions itself and uses the same definitions; the
report says which tool produced the numbers. GPU memory is read from nvidia-smi once a second. vLLM reserves
`--gpu-memory-utilization` of each card for weights plus KV cache when it starts, so the nvidia-smi figure is
that reservation; what the weights alone take is in the server log lines the report keeps.

Outputs go to <out>/<time>-<model>/: report.json, summary.md, server.log and one bench file per level.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import random
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

GIB = 1024**3
MEMORY_LINE = re.compile(r"GiB|KV cache|Maximum concurrency|out of memory|CUDA error", re.I)
REQUIRED_BENCH_FLAGS = ("--backend", "--endpoint", "--base-url", "--model", "--dataset-name", "--random-input-len",
                        "--random-output-len", "--num-prompts", "--max-concurrency", "--save-result",
                        "--result-dir", "--result-filename")
OPTIONAL_BENCH_FLAGS = ("--request-rate", "--ignore-eos", "--seed", "--percentile-metrics", "--metric-percentiles",
                        "--extra-body", "--temperature", "--num-warmups", "--disable-tqdm")
WORDS = ("revenue margin report annual quarter segment growth cost asset equity cash flow tax share dividend "
         "audit note total net gross operating income loss balance sheet period year customer product region "
         "service market price volume rate debt lease capital reserve profit expense supply demand unit").split()


# --------------------------------------------------------------------------- GPUs


def read_gpus() -> list[dict]:
    """One entry per GPU from nvidia-smi; an empty list when it is not there."""
    query = "index,name,memory.total,memory.used,utilization.gpu,driver_version"
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        index, name, total, used, util, driver = (part.strip() for part in line.split(","))
        gpus.append({"index": int(index), "name": name, "memory_total_mib": int(float(total)),
                     "memory_used_mib": int(float(used)), "utilization_pct": int(float(util or 0)),
                     "driver": driver})
    return gpus


class GpuSampler(threading.Thread):
    """Reads used memory per GPU once a second; peak(since) is the highest reading from that moment on."""

    def __init__(self, interval: float = 1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.samples: list[tuple[float, list[int]]] = []
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            gpus = read_gpus()
            if gpus:
                self.samples.append((time.time(), [gpu["memory_used_mib"] for gpu in gpus]))
            self._halt.wait(self.interval)

    def stop(self) -> None:
        self._halt.set()

    def peak(self, since: float = 0.0) -> list[int] | None:
        rows = [used for stamp, used in self.samples if stamp >= since]
        return [max(column) for column in zip(*rows, strict=True)] if rows else None


# --------------------------------------------------------------------------- does it fit


def weights_bytes(model: str) -> int | None:
    """Size of the weight files: a local folder is measured, a hub id is looked up; None when unknown."""
    local = Path(model)
    if local.is_dir():
        files = [p for p in local.rglob("*") if p.suffix in {".safetensors", ".bin"}]
        safetensors = [p for p in files if p.suffix == ".safetensors"]
        return sum(p.stat().st_size for p in (safetensors or files)) or None
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    try:
        with urllib.request.urlopen(f"{endpoint}/api/models/{model}?blobs=true", timeout=30) as response:
            files = json.load(response).get("siblings", [])
    except (OSError, ValueError):
        return None
    sizes = {f["rfilename"]: f.get("size") or 0 for f in files}
    safetensors = [size for name, size in sizes.items() if name.endswith(".safetensors")]
    other = [size for name, size in sizes.items() if name.endswith(".bin")]
    return sum(safetensors or other) or None


def pick_tensor_parallel(requested: str, weights: int | None, gpus: list[dict], utilization: float,
                         headroom_gib: float = 2.0) -> int | None:
    """How many GPUs to spread the model over; None when it does not fit on the GPUs this machine has.

    `auto` takes the smallest power of two for which each card holds its share of the weights and still has
    `headroom_gib` left for the KV cache inside the fraction vLLM may use. Unknown weight size means 1.
    """
    if requested != "auto":
        return int(requested)
    if weights is None or not gpus:
        return 1
    per_gpu = min(gpu["memory_total_mib"] for gpu in gpus) * 1024**2 * utilization
    for count in (1, 2, 4, 8):
        if count <= len(gpus) and weights / count + headroom_gib * GIB <= per_gpu:
            return count
    return None


# --------------------------------------------------------------------------- the server


def http_json(url: str, payload: dict | None = None, timeout: float = 600.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class Server:
    """`vllm serve` as a child process group, with its output in a log file."""

    def __init__(self, command: list[str], log_path: Path, base_url: str):
        self.command, self.log_path, self.base_url = command, log_path, base_url
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        self._log = self.log_path.open("w")
        self.process = subprocess.Popen(self.command, stdout=self._log, stderr=subprocess.STDOUT,
                                        start_new_session=True)

    def wait_ready(self, timeout: float) -> str | None:
        """None once /health answers; otherwise why it did not (exited, or still not up at the deadline)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                return f"vllm serve exited with code {self.process.returncode}"
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=5) as response:
                    if response.status == 200:
                        return None
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(2)
        return f"not ready after {timeout:.0f} s"

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
                try:
                    os.killpg(self.process.pid, sig)
                    self.process.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except ProcessLookupError:
                    break
        if getattr(self, "_log", None):
            self._log.close()

    def log_lines(self) -> list[str]:
        return self.log_path.read_text(errors="replace").splitlines() if self.log_path.exists() else []


def serve_command(args: argparse.Namespace, model: str, tensor_parallel: int) -> list[str]:
    command = [args.vllm, "serve", model, "--host", args.host, "--port", str(args.port), "--dtype", args.dtype,
               "--max-model-len", str(args.max_model_len),
               "--gpu-memory-utilization", str(args.gpu_memory_utilization),
               "--tensor-parallel-size", str(tensor_parallel), "--seed", "0"]
    return command + list(args.serve_arg)


# --------------------------------------------------------------------------- measuring


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, the same rule the repository's evaluators use for p95."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, int(-(-q * len(ordered) // 100)) - 1)]


def spread(values_ms: list[float]) -> dict:
    if not values_ms:
        return {"n": 0}
    return {"n": len(values_ms), "mean": round(statistics.mean(values_ms), 2),
            "p50": round(statistics.median(values_ms), 2), "p95": round(percentile(values_ms, 95), 2),
            "p99": round(percentile(values_ms, 99), 2)}


def chat_body(model: str, prompt: str, max_tokens: int, thinking: bool) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "temperature": 0, "chat_template_kwargs": {"enable_thinking": thinking}}


def random_prompt(seed: int, words: int) -> str:
    """Different from the first word on for every seed, so the server's prefix cache cannot answer for it."""
    rng = random.Random(seed)
    return " ".join(f"{rng.choice(WORDS)}{rng.randrange(1000)}" for _ in range(words))


def stream_once(host: str, port: int, body: dict, timeout: float) -> dict:
    """One streamed chat completion: when the first token arrived, when the last did, and the token counts."""
    payload = body | {"stream": True, "stream_options": {"include_usage": True}, "ignore_eos": True}
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    started = time.perf_counter()
    try:
        connection.request("POST", "/v1/chat/completions", body=json.dumps(payload),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return {"ok": False, "error": f"HTTP {response.status}: {response.read(300)!r}"}
        stamps, usage = [], None
        for raw in response:
            line = raw.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            chunk = json.loads(data)
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
                    stamps.append(time.perf_counter())
    except (OSError, ValueError, http.client.HTTPException) as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}
    finally:
        connection.close()
    if not stamps or not usage:
        return {"ok": False, "error": "no tokens or no usage in the stream"}
    return {"ok": True, "ttft_ms": (stamps[0] - started) * 1000, "e2el_ms": (stamps[-1] - started) * 1000,
            "itl_ms": [(b - a) * 1000 for a, b in zip(stamps, stamps[1:], strict=False)],
            "prompt_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}


def builtin_level(args: argparse.Namespace, model: str, concurrency: int, prompts: int, words: int,
                  seed: int) -> dict:
    """One concurrency level with the built-in client: all requests queued at once, `concurrency` in flight."""
    bodies = [chat_body(model, random_prompt(seed * 100_000 + i, words), args.output_len, args.enable_thinking)
              for i in range(prompts)]
    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(lambda body: stream_once(args.host, args.port, body, args.request_timeout), bodies))
    duration = time.perf_counter() - began
    done = [r for r in results if r["ok"]]
    tpot = [(r["e2el_ms"] - r["ttft_ms"]) / (r["output_tokens"] - 1) for r in done if (r["output_tokens"] or 0) > 1]
    output_tokens = sum(r["output_tokens"] or 0 for r in done)
    input_tokens = sum(r["prompt_tokens"] or 0 for r in done)
    return {
        "tool": "builtin client", "concurrency": concurrency, "requests": prompts, "completed": len(done),
        "failed": len(results) - len(done), "errors": sorted({r["error"] for r in results if not r["ok"]})[:5],
        "duration_s": round(duration, 2),
        "ttft_ms": spread([r["ttft_ms"] for r in done]), "tpot_ms": spread(tpot),
        "itl_ms": spread([gap for r in done for gap in r["itl_ms"]]),
        "e2el_ms": spread([r["e2el_ms"] for r in done]),
        "output_tokens_per_s": round(output_tokens / duration, 2), "requests_per_s": round(len(done) / duration, 3),
        "total_tokens_per_s": round((input_tokens + output_tokens) / duration, 2),
        "mean_input_tokens": round(input_tokens / len(done), 1) if done else None,
        "mean_output_tokens": round(output_tokens / len(done), 1) if done else None,
    }


def words_for(args: argparse.Namespace, model: str, base_url: str) -> int:
    """How many prompt words give about `--input-len` prompt tokens, from one request's usage."""
    probe = 400
    body = chat_body(model, random_prompt(-1, probe), 1, args.enable_thinking)
    used = http_json(base_url + "/v1/chat/completions", body, args.request_timeout)["usage"]["prompt_tokens"]
    return max(1, round(args.input_len * probe / max(used, 1)))


def bench_flags(vllm: str) -> set[str]:
    """The options this vLLM version's `vllm bench serve` accepts, read from its --help."""
    done = subprocess.run([vllm, "bench", "serve", "--help"], capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f"`{vllm} bench serve --help` failed: {(done.stderr or done.stdout)[-300:]}")
    return set(re.findall(r"--[a-z][a-z0-9-]+", done.stdout))


def vllm_bench_command(args: argparse.Namespace, model: str, base_url: str, concurrency: int, prompts: int,
                       seed: int, folder: Path, flags: set[str]) -> tuple[list[str], list[str]]:
    """The `vllm bench serve` call for one level, and the optional flags this version does not have."""
    missing = [flag for flag in REQUIRED_BENCH_FLAGS if flag not in flags]
    if missing:
        raise RuntimeError("`vllm bench serve` has no " + ", ".join(missing))
    command = [args.vllm, "bench", "serve", "--backend", "openai-chat", "--endpoint", "/v1/chat/completions",
               "--base-url", base_url, "--model", model, "--dataset-name", "random",
               "--random-input-len", str(args.input_len), "--random-output-len", str(args.output_len),
               "--num-prompts", str(prompts), "--max-concurrency", str(concurrency), "--save-result",
               "--result-dir", str(folder), "--result-filename", f"bench-c{concurrency}.json"]
    optional = {
        "--request-rate": ["inf"], "--ignore-eos": [], "--seed": [str(seed)],
        "--percentile-metrics": ["ttft,tpot,itl,e2el"], "--metric-percentiles": ["50,95,99"],
        "--extra-body": [json.dumps({"chat_template_kwargs": {"enable_thinking": args.enable_thinking}})],
        "--temperature": ["0"], "--num-warmups": ["2"], "--disable-tqdm": [],
    }
    dropped = []
    for flag in OPTIONAL_BENCH_FLAGS:
        if flag in flags:
            command += [flag, *optional[flag]]
        else:
            dropped.append(flag)
    return command, dropped


def vllm_bench_level(args: argparse.Namespace, model: str, base_url: str, concurrency: int, prompts: int,
                     seed: int, folder: Path, flags: set[str]) -> dict:
    """One concurrency level with `vllm bench serve`; its saved JSON, reduced to the figures reported here."""
    command, dropped = vllm_bench_command(args, model, base_url, concurrency, prompts, seed, folder, flags)
    with (folder / f"bench-c{concurrency}.log").open("w") as log:
        done = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.level_timeout)
    result_file = folder / f"bench-c{concurrency}.json"
    if done.returncode != 0 or not result_file.exists():
        tail = (folder / f"bench-c{concurrency}.log").read_text(errors="replace")[-600:]
        raise RuntimeError(f"`vllm bench serve` exited with code {done.returncode}: {tail}")
    raw = json.loads(result_file.read_text())

    def stat(metric: str) -> dict:
        return {"mean": raw.get(f"mean_{metric}_ms"), "p50": raw.get(f"p50_{metric}_ms", raw.get(f"median_{metric}_ms")),
                "p95": raw.get(f"p95_{metric}_ms"), "p99": raw.get(f"p99_{metric}_ms")}

    completed = raw.get("completed")
    return {
        "tool": "vllm bench serve", "concurrency": concurrency, "requests": prompts, "completed": completed,
        "failed": raw.get("failed", prompts - completed if isinstance(completed, int) else None),
        "duration_s": raw.get("duration"),
        "ttft_ms": stat("ttft"), "tpot_ms": stat("tpot"), "itl_ms": stat("itl"), "e2el_ms": stat("e2el"),
        "output_tokens_per_s": raw.get("output_throughput"), "requests_per_s": raw.get("request_throughput"),
        "total_tokens_per_s": raw.get("total_token_throughput"),
        "mean_input_tokens": round(raw["total_input_tokens"] / completed, 1)
        if completed and raw.get("total_input_tokens") else None,
        "mean_output_tokens": round(raw["total_output_tokens"] / completed, 1)
        if completed and raw.get("total_output_tokens") else None,
        "flags_not_in_this_version": dropped, "command": command,
    }


# --------------------------------------------------------------------------- one candidate


def measure(args: argparse.Namespace, model: str, base_url: str, folder: Path, sampler: GpuSampler) -> dict:
    """The smoke request and every concurrency level against a server that is already up."""
    smoke_body = chat_body(model, "Reply with the single word: ready", 16, args.enable_thinking)
    began = time.perf_counter()
    answer = http_json(base_url + "/v1/chat/completions", smoke_body, args.request_timeout)
    message = answer["choices"][0]["message"]
    smoke = {"ms": round((time.perf_counter() - began) * 1000, 1), "usage": answer.get("usage"),
             "finish_reason": answer["choices"][0].get("finish_reason"), "content": (message.get("content") or "")[:80],
             "reasoning_returned": bool(message.get("reasoning_content") or message.get("reasoning"))}
    tool, flags, notes = args.tool, set(), []
    if tool != "builtin":
        try:
            flags = bench_flags(args.vllm)
            vllm_bench_command(args, model, base_url, 1, 1, 0, folder, flags)  # fails here if a needed flag is gone
            tool = "vllm-bench"
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            if args.tool == "vllm-bench":
                raise
            notes.append(f"vllm bench serve not usable, built-in client used instead: {error}"[:400])
            tool = "builtin"
    words = words_for(args, model, base_url) if tool == "builtin" or args.tool == "auto" else None
    levels = []
    for index, concurrency in enumerate(args.levels):
        prompts = max(args.prompts_per_level, 5 * concurrency)
        since = time.time()
        if tool == "vllm-bench":
            try:
                level = vllm_bench_level(args, model, base_url, concurrency, prompts, index, folder, flags)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                if args.tool == "vllm-bench" or levels:  # never mix two tools across levels of one run
                    raise
                notes.append(f"vllm bench serve failed at the first level, built-in client used instead: {error}"[:600])
                tool = "builtin"
        if tool == "builtin":
            if index == 0:
                builtin_level(args, model, 1, 2, words, seed=999)  # two warm-up requests, not counted
                since = time.time()
            level = builtin_level(args, model, concurrency, prompts, words, seed=index)
        # A level shorter than the sampling interval may have no sample of its own: read once more.
        level["gpu_memory_peak_mib"] = sampler.peak(since) or [gpu["memory_used_mib"] for gpu in read_gpus()] or None
        levels.append(level)
        print(f"[bench] concurrency {concurrency}: ttft p50 {level['ttft_ms'].get('p50')} ms, "
              f"tpot p50 {level['tpot_ms'].get('p50')} ms, {level['output_tokens_per_s']} tok/s, "
              f"completed {level['completed']}/{prompts}", flush=True)
    return {"smoke": smoke, "tool": "vllm bench serve" if tool == "vllm-bench" else "builtin client",
            "notes": notes, "levels": levels}


def attempt(args: argparse.Namespace, model: str, gpus: list[dict], out: Path, stamp: str) -> dict:
    """Try one candidate: check it fits, start the server, measure, stop the server."""
    weights = weights_bytes(model)
    record: dict = {"model": model, "weights_gib": round(weights / GIB, 2) if weights else None}
    tensor_parallel = pick_tensor_parallel(args.tensor_parallel_size, weights, gpus, args.gpu_memory_utilization)
    if tensor_parallel is None:
        return record | {"outcome": "skipped", "reason": "the weights do not fit on the GPUs of this machine "
                         f"({len(gpus)} x {min(g['memory_total_mib'] for g in gpus)} MiB)"}
    folder = out / f"{stamp}-{re.sub(r'[^A-Za-z0-9._-]+', '-', model).strip('-')}"
    folder.mkdir(parents=True, exist_ok=True)
    base_url = f"http://{args.host}:{args.port}"
    command = serve_command(args, model, tensor_parallel)
    record |= {"tensor_parallel_size": tensor_parallel, "serve_command": command, "folder": str(folder)}
    before = [gpu["memory_used_mib"] for gpu in gpus]
    server = Server(command, folder / "server.log", base_url)
    sampler = GpuSampler()
    began = time.time()
    print(f"[bench] starting: {' '.join(command)}", flush=True)
    server.start()
    sampler.start()
    try:
        problem = server.wait_ready(args.startup_timeout)
        if problem:
            return record | {"outcome": "failed", "reason": problem, "log_tail": server.log_lines()[-25:],
                             "memory_lines": [line for line in server.log_lines() if MEMORY_LINE.search(line)][-10:]}
        record["startup_s"] = round(time.time() - began, 1)
        after_load = [gpu["memory_used_mib"] for gpu in read_gpus()]
        print(f"[bench] ready after {record['startup_s']} s; GPU memory used {after_load} MiB", flush=True)
        try:
            measured = measure(args, model, base_url, folder, sampler)
        except Exception as error:  # the server came up but could not be measured: recorded, next candidate
            return record | {"outcome": "failed", "reason": f"{type(error).__name__}: {error}"[:800],
                             "log_tail": server.log_lines()[-25:]}
        record |= measured | {
            "outcome": "measured",
            "gpu_memory": {
                "gpu_memory_utilization": args.gpu_memory_utilization,
                "total_mib": [gpu["memory_total_mib"] for gpu in gpus], "before_mib": before,
                "after_load_mib": after_load, "peak_mib": sampler.peak(),
                "server_log_lines": [line for line in server.log_lines() if MEMORY_LINE.search(line)][:20]},
        }
        return record
    finally:
        sampler.stop()
        server.stop()


# --------------------------------------------------------------------------- report


def summary_markdown(report: dict) -> str:
    def pair(stat: dict) -> str:
        return f"{stat.get('p50')} / {stat.get('p95')}"

    lines = [f"# vLLM 服务指标：{report.get('model') or '没有模型跑成'}", ""]
    for record in report["attempts"]:
        reason = f"：{record['reason']}" if record.get("reason") else ""
        lines.append(f"- {record['model']}（权重 {record.get('weights_gib')} GiB，"
                     f"tensor parallel {record.get('tensor_parallel_size')}）：{record['outcome']}{reason}")
    chosen = next((r for r in report["attempts"] if r["outcome"] == "measured"), None)
    if not chosen:
        return "\n".join(lines) + "\n"
    work, memory = report["workload"], chosen["gpu_memory"]
    lines += ["", f"GPU：{', '.join(g['name'] for g in report['host']['gpus'])}；vLLM {report['host']['vllm_version']}；"
                  f"启动 {chosen['startup_s']} 秒；测量工具：{chosen['tool']}。",
              f"负载：每个请求约 {work['input_len']} 个输入 token、固定 {work['output_len']} 个输出 token"
              f"（ignore_eos），enable_thinking = {str(work['enable_thinking']).lower()}，每档至少 "
              f"{work['prompts_per_level']} 个请求。",
              f"显存（MiB，每张卡）：加载前 {memory['before_mib']}，加载后 {memory['after_load_mib']}，"
              f"压测峰值 {memory['peak_mib']}，总量 {memory['total_mib']}。vLLM 启动时按 "
              f"gpu_memory_utilization = {memory['gpu_memory_utilization']} 预留权重和 KV cache，"
              "所以这个数字是预留量；权重本身占多少看下面的日志行。", ""]
    lines += [f"    {line.strip()[-160:]}" for line in memory["server_log_lines"][:8]]
    lines += ["", "| 并发 | 完成 / 请求 | TTFT p50 / p95 (ms) | TPOT p50 / p95 (ms) | 端到端 p50 / p95 (ms) | "
                  "输出 token/s | 请求/s | 显存峰值 (MiB) |", "|---|---|---|---|---|---|---|---|"]
    for level in chosen["levels"]:
        lines.append(f"| {level['concurrency']} | {level['completed']} / {level['requests']} | {pair(level['ttft_ms'])} | "
                     f"{pair(level['tpot_ms'])} | {pair(level['e2el_ms'])} | {level['output_tokens_per_s']} | "
                     f"{level['requests_per_s']} | {level['gpu_memory_peak_mib']} |")
    lines += [f"- {note}" for note in chosen["notes"]]
    return "\n".join(lines) + "\n"


def vllm_version(vllm: str) -> str | None:
    try:
        done = subprocess.run([vllm, "--version"], capture_output=True, text=True, timeout=300)
        return (done.stdout or done.stderr).strip().splitlines()[-1] if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", required=True,
                        help="hub id or local folder; repeat to give fallbacks in order of preference")
    parser.add_argument("--tensor-parallel-size", default="auto", help="auto, or a number of GPUs")
    parser.add_argument("--dtype", default="half", help="half by default: a T4 has no bfloat16")
    parser.add_argument("--max-model-len", type=int, default=16384)
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.90)
    parser.add_argument("--serve-arg", action="append", default=[], help="extra argument for `vllm serve`, verbatim")
    parser.add_argument("--concurrency", default="1,2,4,8,16", help="comma-separated levels")
    parser.add_argument("--input-len", type=int, default=1536, help="prompt tokens per request")
    parser.add_argument("--output-len", type=int, default=128, help="output tokens per request (EOS ignored)")
    parser.add_argument("--prompts-per-level", type=int, default=40, help="raised to 5 x concurrency when lower")
    parser.add_argument("--enable-thinking", action="store_true", help="leave Qwen3 thinking on (off by default)")
    parser.add_argument("--tool", choices=["auto", "vllm-bench", "builtin"], default="auto")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--startup-timeout", type=float, default=1800.0, help="seconds, download included")
    parser.add_argument("--request-timeout", type=float, default=600.0)
    parser.add_argument("--level-timeout", type=float, default=3600.0)
    parser.add_argument("--vllm", default="vllm", help="the vllm executable")
    parser.add_argument("--out", type=Path, default=None, help="default: /kaggle/working/vllm-bench on Kaggle")
    args = parser.parse_args(argv)
    args.levels = [int(value) for value in args.concurrency.split(",") if value.strip()]
    out = args.out or (Path("/kaggle/working/vllm-bench") if Path("/kaggle/working").is_dir() else Path("vllm-bench"))
    out.mkdir(parents=True, exist_ok=True)
    if shutil.which(args.vllm) is None:
        print(f"[bench] `{args.vllm}` not found: install vLLM first (scripts/kaggle_vllm_bench.sh does)")
        return 1
    gpus = read_gpus()
    if not gpus:
        print("[bench] nvidia-smi reports no GPU: on Kaggle set Accelerator to GPU T4 x2")
        return 1
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    report: dict = {
        "recorded_at": None, "status": None, "model": None,
        "host": {"platform": platform.platform(), "python": sys.version, "gpus": gpus,
                 "vllm_version": vllm_version(args.vllm)},
        "workload": {"input_len": args.input_len, "output_len": args.output_len, "concurrency": args.levels,
                     "prompts_per_level": args.prompts_per_level, "enable_thinking": args.enable_thinking,
                     "dtype": args.dtype, "max_model_len": args.max_model_len, "requested_tool": args.tool},
        "attempts": [],
    }
    for model in args.model:
        record = attempt(args, model, gpus, out, stamp)
        report["attempts"].append(record)
        print(f"[bench] {model}: {record['outcome']} {record.get('reason', '')}", flush=True)
        if record["outcome"] == "measured":
            report["model"] = model
            break
    report["status"] = "COMPLETED" if report["model"] else "NO_CANDIDATE_MEASURED"
    report["recorded_at"] = datetime.now(UTC).isoformat()
    chosen = next((r for r in report["attempts"] if r["outcome"] == "measured"), None)
    target = Path(chosen["folder"]) if chosen else out
    (target / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    summary = summary_markdown(report)
    (target / "summary.md").write_text(summary)
    print(summary)
    print(f"[bench] report: {target / 'report.json'}")
    return 0 if chosen else 1


if __name__ == "__main__":
    sys.exit(main())
