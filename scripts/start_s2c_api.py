"""Start the fixed S2C DEV module on unused loopback 8771; leave older services."""

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
from start_grounded_api import private_descriptor

from forgeflow.analysis_provider import MODEL_DIGEST
from forgeflow.s2c_contracts import REVISION
from forgeflow.s2c_workflow import PROMPT_REVISION

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MODULE = "forgeflow.s2c_api"
PORT = 8771
URL = f"http://127.0.0.1:{PORT}"


def health():
    try:
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            response = client.get(URL + "/s2c/health")
            data = response.json()
        if (response.status_code == 200 and type(data) is dict
            and data.get("identity") == "DEV_ONLY" and data.get("analysis_revision") == REVISION
            and data.get("prompt_revision") == PROMPT_REVISION and data.get("pinned_digest") == MODEL_DIGEST
            and data.get("automatic_business_actions") is False and type(data.get("process_id")) is int):
            return data
    except (httpx.HTTPError, ValueError):
        pass
    return None


def listening():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", PORT)) == 0


def own_pid():
    try:
        descriptor = os.open(RUNTIME / "s2c-api.pid", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor) as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                return None
            raw = file.read(24)
        if not raw.strip().isdigit() or int(raw) <= 1:
            return None
        pid = int(raw)
        command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                                 text=True, timeout=2, check=False)
        cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True,
                             text=True, timeout=2, check=False)
        if (command.returncode == 0 and shlex.split(command.stdout.strip())[-2:] == ["-m", MODULE]
            and cwd.returncode == 0 and "n" + str(ROOT.resolve()) in cwd.stdout.splitlines()):
            return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def main():
    RUNTIME.mkdir(exist_ok=True)
    if (RUNTIME.is_symlink() or RUNTIME.resolve().parent != ROOT.resolve()
        or RUNTIME.stat().st_uid != os.getuid()):
        raise SystemExit("Task-owned runtime directory required")
    if listening():
        data, pid = health(), own_pid()
        if data is not None and pid is not None and data["process_id"] == pid:
            print(json.dumps({"status": "ALREADY_LISTENING", "pid": pid, "url": URL + "/docs"}))
            return
        raise SystemExit("Port 8771 is occupied; no process was stopped")
    descriptor = private_descriptor(RUNTIME / "s2c-api.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen([sys.executable, "-m", MODULE], cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        data = health()
        if data is not None and data["process_id"] == process.pid:
            descriptor = private_descriptor(RUNTIME / "s2c-api.pid", os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
            with os.fdopen(descriptor, "w") as file:
                file.write(str(process.pid) + "\n")
            print(json.dumps({"status": "RUNNING", "pid": process.pid, "url": URL + "/docs"}))
            return
        time.sleep(0.1)
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    raise SystemExit("New S2C child failed to start; prior services were untouched")


if __name__ == "__main__":
    main()
