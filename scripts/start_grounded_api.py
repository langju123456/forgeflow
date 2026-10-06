"""Start only the task-owned grounded DEV API on loopback port 8770.

An occupied port is never reclaimed. Only the child started by this invocation
can be terminated after a failed health check; existing processes are untouched.
"""

import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.grounded_contracts import REVISION

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MODULE = "forgeflow.grounded_api"
URL = "http://127.0.0.1:8770"


def health():
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get(URL + "/grounded/health")
            data = response.json()
        if (
            type(data) is dict
            and response.status_code == 200
            and data.get("identity") == "DEV_ONLY"
            and data.get("analysis_revision") == REVISION
            and data.get("pinned_digest") == MODEL_DIGEST
            and data.get("automatic_business_actions") is False
            and type(data.get("process_id")) is int
        ):
            return data
    except (httpx.HTTPError, ValueError):
        pass
    return None


def listening():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", 8770)) == 0


def own_pid():
    try:
        descriptor = os.open(RUNTIME / "grounded-api.pid", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as pidfile:
            metadata = os.fstat(pidfile.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                return None
            raw = pidfile.read(24)
        if not raw.strip().isdigit():
            return None
        pid = int(raw)
        if pid <= 1:
            return None
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2, check=False,
        )
        args = shlex.split(result.stdout.strip())
        if result.returncode == 0 and args[-2:] == ["-m", MODULE]:
            return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def private_descriptor(path, flags):
    descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        os.close(descriptor)
        raise SystemExit("Task-owned regular runtime file required")
    os.fchmod(descriptor, 0o600)
    return descriptor


def main():
    RUNTIME.mkdir(exist_ok=True)
    if (
        RUNTIME.is_symlink() or RUNTIME.resolve().parent != ROOT.resolve()
        or RUNTIME.stat().st_uid != os.getuid()
    ):
        raise SystemExit("Task-owned runtime directory required")
    if listening():
        data = health()
        pid = own_pid()
        if data is not None and pid is not None and data["process_id"] == pid:
            print(json.dumps({"status": "ALREADY_LISTENING", "pid": pid, "url": URL + "/docs"}))
            return
        raise SystemExit("Port 8770 is occupied; no process was stopped")
    descriptor = private_descriptor(
        RUNTIME / "grounded-api.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY
    )
    with os.fdopen(descriptor, "ab") as logfile:
        process = subprocess.Popen(
            [sys.executable, "-m", MODULE], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        data = health()
        if data is not None and data["process_id"] == process.pid:
            descriptor = private_descriptor(
                RUNTIME / "grounded-api.pid", os.O_CREAT | os.O_WRONLY | os.O_TRUNC
            )
            with os.fdopen(descriptor, "w") as pidfile:
                pidfile.write(str(process.pid) + "\n")
            print(json.dumps({"status": "RUNNING", "pid": process.pid, "url": URL + "/docs"}))
            return
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    raise SystemExit("API did not become ready; inspect the task-owned .runtime/grounded-api.log")


if __name__ == "__main__":
    main()
