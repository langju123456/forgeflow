"""scripts/vllm_bench.py against stand-ins for `vllm` and `nvidia-smi`: no GPU, no model, no network.

The stand-in `vllm serve` is a small HTTP server that streams tokens with known delays; the stand-in
`vllm bench serve` writes a result file with known figures. What is checked is the script's own work:
choosing a candidate that fits, starting and stopping the server, the fallback order, and the arithmetic.
"""

import importlib.util
import json
import os
import socket
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vllm_bench", ROOT / "scripts/vllm_bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

FAKE_VLLM = '''
import json, os, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
state = os.environ["STUB_DIR"]
BENCH_FLAGS = ("--backend --endpoint --base-url --model --dataset-name --random-input-len --random-output-len "
               "--num-prompts --max-concurrency --save-result --result-dir --result-filename --request-rate "
               "--ignore-eos --seed --percentile-metrics --metric-percentiles --extra-body --temperature "
               "--num-warmups --disable-tqdm").split()

def value(flag):
    return args[args.index(flag) + 1]

if args == ["--version"]:
    print("0.31.0")
elif args[:2] == ["bench", "serve"]:
    if "--help" in args:
        print("usage: vllm bench serve " + " ".join(f for f in BENCH_FLAGS if f not in os.environ.get("STUB_NO_FLAGS", "")))
        sys.exit(0)
    if os.environ.get("STUB_BENCH_FAIL"):
        print("ModuleNotFoundError: install vllm[bench]")
        sys.exit(3)
    c, n = int(value("--max-concurrency")), int(value("--num-prompts"))
    with open(os.path.join(state, f"bench-args-c{c}.json"), "w") as fh:
        json.dump(args, fh)
    result = {"completed": n, "failed": 0, "duration": 10.0, "total_input_tokens": n * int(value("--random-input-len")),
              "total_output_tokens": n * int(value("--random-output-len")), "request_throughput": n / 10.0,
              "output_throughput": 100.0 * c, "total_token_throughput": 1300.0 * c}
    for metric, base in (("ttft", 100.0), ("tpot", 20.0), ("itl", 21.0), ("e2el", 2600.0)):
        result |= {f"mean_{metric}_ms": base * c, f"median_{metric}_ms": base * c - 1, f"p50_{metric}_ms": base * c - 1,
                   f"p95_{metric}_ms": base * c + 5, f"p99_{metric}_ms": base * c + 9}
    with open(os.path.join(value("--result-dir"), value("--result-filename")), "w") as fh:
        json.dump(result, fh)
elif args[0] == "serve":
    with open(os.path.join(state, "serve-args.json"), "w") as fh:
        json.dump(args, fh)
    if "refuses" in args[1]:
        print("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB")
        sys.exit(1)
    print("INFO Model loading took 7.60 GiB and 12.3 seconds")
    print("INFO Available KV cache memory: 4.87 GiB", flush=True)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            prompt_tokens = 2 * len(body["messages"][0]["content"].split())
            assert body["chat_template_kwargs"] == {"enable_thinking": False}
            self.send_response(200)
            if not body.get("stream"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1}}).encode())
                return
            assert body["ignore_eos"] is True and body["stream_options"] == {"include_usage": True}
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            time.sleep(0.05)  # time to first token
            for index in range(body["max_tokens"]):
                if index:
                    time.sleep(0.004)  # time per further token
                chunk = {"choices": [{"delta": {"content": "tok"}}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\\n\\n")
                self.wfile.flush()
            usage = {"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": body["max_tokens"]}}
            self.wfile.write(b"data: " + json.dumps(usage).encode() + b"\\n\\ndata: [DONE]\\n\\n")

    open(os.path.join(state, "serving"), "w").close()
    ThreadingHTTPServer((value("--host"), int(value("--port"))), Handler).serve_forever()
'''

FAKE_SMI = '''
import os
used = 13800 if os.path.exists(os.path.join(os.environ["STUB_DIR"], "serving")) else 3
for index in range(int(os.environ.get("STUB_GPUS", "1"))):
    print(f"{index}, Tesla T4, 15360, {used}, 0, 580.178.04")
'''


@pytest.fixture()
def stand_ins(tmp_path, monkeypatch):
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, source in (("vllm", FAKE_VLLM), ("nvidia-smi", FAKE_SMI)):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(source))
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("STUB_DIR", str(state))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return {"state": state, "port": port, "out": tmp_path / "out"}


def model_folder(tmp_path, name, gib):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "model.safetensors").touch()
    os.truncate(folder / "model.safetensors", int(gib * bench.GIB))  # sparse: takes no disk space
    return str(folder)


def run(stand_ins, models, *extra):
    argv = [arg for model in models for arg in ("--model", model)]
    argv += ["--port", str(stand_ins["port"]), "--out", str(stand_ins["out"]), "--concurrency", "1,2",
             "--prompts-per-level", "6", "--input-len", "200", "--output-len", "8", "--startup-timeout", "60", *extra]
    code = bench.main(argv)
    reports = sorted(stand_ins["out"].glob("**/report.json"))
    return code, json.loads(reports[-1].read_text())


def port_is_closed(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def test_tensor_parallel_auto_spreads_a_model_that_does_not_fit_on_one_card_or_says_it_cannot_fit():
    t4 = {"memory_total_mib": 15360}
    qwen3_8b_fp16, quantized = int(16.4e9), int(6.2e9)
    assert bench.pick_tensor_parallel("auto", qwen3_8b_fp16, [t4, t4], 0.90) == 2
    assert bench.pick_tensor_parallel("auto", qwen3_8b_fp16, [t4], 0.90) is None  # the next candidate is tried
    assert bench.pick_tensor_parallel("auto", quantized, [t4, t4], 0.90) == 1
    assert bench.pick_tensor_parallel("auto", None, [t4], 0.90) == 1  # size unknown: let vLLM decide
    assert bench.pick_tensor_parallel("2", qwen3_8b_fp16, [t4], 0.90) == 2  # an explicit value is not second-guessed


def test_weight_size_comes_from_the_hub_listing_and_is_unknown_when_the_hub_cannot_be_reached(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    listing = {"siblings": [{"rfilename": "model-00001-of-00002.safetensors", "size": 9_000_000_000},
                            {"rfilename": "model-00002-of-00002.safetensors", "size": 7_400_000_000},
                            {"rfilename": "pytorch_model.bin", "size": 16_400_000_000},  # a second copy: not added
                            {"rfilename": "tokenizer.json", "size": 11_000_000}, {"rfilename": "README.md"}]}
    asked = []

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            asked.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(listing).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("HF_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}")
        assert bench.weights_bytes("Qwen/Qwen3-8B") == 16_400_000_000
        assert asked == ["/api/models/Qwen/Qwen3-8B?blobs=true"]
    finally:
        server.shutdown()
        server.server_close()
    assert bench.weights_bytes("Qwen/Qwen3-8B") is None  # nothing listens there any more
    assert bench.weights_bytes(model_folder(tmp_path, "local", 3)) == 3 * bench.GIB


def test_nearest_rank_percentiles_and_token_rates():
    values = [float(v) for v in range(1, 41)]
    assert (bench.percentile(values, 50), bench.percentile(values, 95), bench.percentile(values, 99)) == (20.0, 38.0, 40.0)
    assert bench.spread(values) == {"n": 40, "mean": 20.5, "p50": 20.5, "p95": 38.0, "p99": 40.0}
    assert bench.spread([]) == {"n": 0}


def test_flags_this_vllm_version_lacks_are_left_out_and_a_missing_required_flag_is_fatal(tmp_path):
    args = bench.argparse.Namespace(vllm="vllm", input_len=1536, output_len=128, enable_thinking=False)
    everything = set(bench.REQUIRED_BENCH_FLAGS) | set(bench.OPTIONAL_BENCH_FLAGS)
    command, dropped = bench.vllm_bench_command(args, "m", "http://127.0.0.1:8000", 4, 40, 2, tmp_path, everything)
    assert dropped == [] and command[:3] == ["vllm", "bench", "serve"]
    joined = " ".join(command)
    assert "--backend openai-chat --endpoint /v1/chat/completions" in joined
    assert "--random-input-len 1536 --random-output-len 128 --num-prompts 40 --max-concurrency 4" in joined
    assert "--ignore-eos" in command and "--metric-percentiles 50,95,99" in joined
    assert json.loads(command[command.index("--extra-body") + 1]) == {"chat_template_kwargs": {"enable_thinking": False}}
    command, dropped = bench.vllm_bench_command(args, "m", "http://127.0.0.1:8000", 4, 40, 2, tmp_path,
                                                everything - {"--num-warmups", "--extra-body"})
    assert dropped == ["--extra-body", "--num-warmups"] and "--num-warmups" not in command
    with pytest.raises(RuntimeError, match="--max-concurrency"):
        bench.vllm_bench_command(args, "m", "http://x", 4, 40, 2, tmp_path, everything - {"--max-concurrency"})


def test_first_candidate_that_fits_and_starts_is_measured_and_the_rest_are_on_record(stand_ins, tmp_path):
    too_big = model_folder(tmp_path, "too-big", 17)  # more than one 15 GiB card holds
    refuses = model_folder(tmp_path, "refuses-to-load", 1)  # fits on paper, the server dies on start
    small = model_folder(tmp_path, "small", 6)
    code, report = run(stand_ins, [too_big, refuses, small], "--tool", "vllm-bench")
    assert code == 0 and report["status"] == "COMPLETED" and report["model"] == small
    skipped, failed, measured = report["attempts"]
    assert skipped["outcome"] == "skipped" and "do not fit" in skipped["reason"] and skipped["weights_gib"] == 17.0
    assert failed["outcome"] == "failed" and failed["reason"] == "vllm serve exited with code 1"
    assert any("out of memory" in line for line in failed["log_tail"])
    serve = measured["serve_command"]
    assert serve[:3] == ["vllm", "serve", small] and measured["tensor_parallel_size"] == 1
    assert " ".join(serve[3:]) == (f"--host 127.0.0.1 --port {stand_ins['port']} --dtype half --max-model-len 16384 "
                                   "--gpu-memory-utilization 0.9 --tensor-parallel-size 1 --seed 0")
    assert measured["tool"] == "vllm bench serve" and [level["concurrency"] for level in measured["levels"]] == [1, 2]
    two = measured["levels"][1]
    assert two["requests"] == 10 and two["completed"] == 10  # 6 asked for, raised to 5 x concurrency
    assert two["ttft_ms"] == {"mean": 200.0, "p50": 199.0, "p95": 205.0, "p99": 209.0}
    assert two["tpot_ms"]["p95"] == 45.0 and two["output_tokens_per_s"] == 200.0 and two["mean_output_tokens"] == 8.0
    assert two["gpu_memory_peak_mib"] == [13800]
    memory = measured["gpu_memory"]
    assert memory["before_mib"] == [3] and memory["after_load_mib"] == [13800] and memory["total_mib"] == [15360]
    assert any("Model loading took 7.60 GiB" in line for line in memory["server_log_lines"])
    assert measured["smoke"]["content"] == "ready" and measured["smoke"]["reasoning_returned"] is False
    assert port_is_closed(stand_ins["port"])  # the server was stopped
    summary = (Path(measured["folder"]) / "summary.md").read_text()
    assert "| 2 | 10 / 10 | 199.0 / 205.0 | 39.0 / 45.0 |" in summary and "skipped" in summary


def test_builtin_client_times_the_first_token_and_the_tokens_after_it(stand_ins, tmp_path):
    code, report = run(stand_ins, [model_folder(tmp_path, "small", 6)], "--tool", "builtin")
    assert code == 0
    measured = report["attempts"][0]
    assert measured["tool"] == "builtin client" and not (stand_ins["state"] / "bench-args-c1.json").exists()
    one = measured["levels"][0]
    assert one["completed"] == one["requests"] == 6 and one["failed"] == 0 and one["mean_output_tokens"] == 8.0
    assert one["mean_input_tokens"] == 200.0  # the prompt length was calibrated from the server's own count
    # The stand-in waits 50 ms before the first token and 4 ms before each later one; a busy machine only adds.
    assert 50 <= one["ttft_ms"]["p50"] < 5000 and 4 <= one["tpot_ms"]["p50"] < 1000
    assert one["e2el_ms"]["p50"] >= one["ttft_ms"]["p50"] + 7 * 4
    assert one["output_tokens_per_s"] == pytest.approx(6 * 8 / one["duration_s"], rel=0.05)
    assert port_is_closed(stand_ins["port"])


def test_auto_falls_back_to_the_builtin_client_when_vllm_bench_cannot_run_and_says_so(stand_ins, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_BENCH_FAIL", "1")
    code, report = run(stand_ins, [model_folder(tmp_path, "small", 6)])
    measured = report["attempts"][0]
    assert code == 0 and measured["tool"] == "builtin client" and len(measured["levels"]) == 2
    assert "vllm bench serve failed at the first level" in measured["notes"][0]
    assert all(level["tool"] == "builtin client" for level in measured["levels"])  # never two tools in one run


def test_no_candidate_measured_is_an_error_with_every_attempt_on_record(stand_ins, tmp_path):
    code, report = run(stand_ins, [model_folder(tmp_path, "too-big", 17), model_folder(tmp_path, "refuses-to-load", 1)])
    assert code == 1 and report["status"] == "NO_CANDIDATE_MEASURED" and report["model"] is None
    assert [record["outcome"] for record in report["attempts"]] == ["skipped", "failed"]
    assert port_is_closed(stand_ins["port"])
