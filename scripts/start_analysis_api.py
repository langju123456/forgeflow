"""Start only ForgeFlow's loopback DEV API; never stop another listening process."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
runtime = ROOT / ".runtime"
runtime.mkdir(exist_ok=True)
if runtime.is_symlink() or runtime.resolve().parent != ROOT.resolve():
    raise SystemExit("Task-owned runtime directory required")


def ready():
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get("http://127.0.0.1:8769/analysis/health")
            data = response.json()
            return response.status_code == 200 and data.get("profile") == "SYNTHETIC_LOCAL_QWEN3"
    except (httpx.HTTPError, ValueError):
        return False


if ready():
    print(json.dumps({"status": "ALREADY_LISTENING", "url": "http://127.0.0.1:8769/docs"}))
    raise SystemExit(0)

log_path = runtime / "analysis-api.log"
descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
with os.fdopen(descriptor, "ab") as logfile:
    process = subprocess.Popen(
        [sys.executable, "-m", "forgeflow.analysis_api"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=logfile,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
deadline = time.monotonic() + 10
while process.poll() is None and time.monotonic() < deadline:
    if ready():
        descriptor = os.open(
            runtime / "analysis-api.pid", os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as pidfile:
            pidfile.write(str(process.pid) + "\n")
        print(json.dumps({"status": "RUNNING", "pid": process.pid, "url": "http://127.0.0.1:8769/docs"}))
        raise SystemExit(0)
    time.sleep(0.1)
if process.poll() is None:
    process.terminate()
    process.wait(timeout=5)
raise SystemExit("API did not become ready; inspect only the task-owned .runtime/analysis-api.log")
