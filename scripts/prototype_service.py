"""Start the unified API; retire only verified task-owned historical API modules."""

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
URL = "http://127.0.0.1:8773"
MODULE = "forgeflow.prototype_api"
HISTORICAL = [("analysis", 8769, "forgeflow.analysis_api"),
              ("grounded", 8770, "forgeflow.grounded_selection_api"),
              ("s2c", 8771, "forgeflow.s2c_api"), ("sre", 8772, "forgeflow.sre_api")]


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def own_pid(name, port, module):
    try:
        raw = json.loads((RUNTIME / f"{name}-api.pid").read_text())
        pid = raw if type(raw) is int else raw["pid"]
        if type(pid) is not int or pid <= 1:
            return None
        def read(args):
            return subprocess.run(args, capture_output=True, text=True, timeout=2, check=False).stdout.splitlines()
        command = read(["ps", "-p", str(pid), "-o", "command="])
        cwd = read(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        ports = read(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"])
        if (command and shlex.split(command[0])[-2:] == ["-m", module]
                and "n" + str(ROOT) in cwd and f"n127.0.0.1:{port}" in ports):
            return pid
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        pass
    return None


def health():
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(URL + "/health")
            paths = client.get(URL + "/openapi.json").json()["paths"]
        data = response.json()
        if (response.status_code == 200 and data.get("revision") == "domain-source-contract-v1"
                and all(path in paths for path in ("/s2c/parents", "/runs/{run_id}/grounded-analysis",
                                                   "/runs/{run_id}/sre-review", "/runs/{run_id}/domain-review"))):
            return data
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


def start():
    if listening(8773):
        data, pid = health(), own_pid("prototype", 8773, MODULE)
        if data and pid and data["process_id"] == pid:
            return {"status": "ALREADY_RUNNING", "pid": pid, "url": URL + "/docs"}
        raise SystemExit("Port 8773 occupied; existing process untouched")
    descriptor = os.open(RUNTIME / "prototype-api.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        child = subprocess.Popen([sys.executable, "-m", MODULE], cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        data = health()
        if data and data["process_id"] == child.pid:
            pid_file = RUNTIME / "prototype-api.pid"
            pid_file.write_text(str(child.pid) + "\n")
            pid_file.chmod(0o600)
            return {"status": "RUNNING", "pid": child.pid, "url": URL + "/docs"}
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
    raise SystemExit("New prototype child failed; historical services untouched")


def consolidate():
    data, pid = health(), own_pid("prototype", 8773, MODULE)
    if not data or not pid or data["process_id"] != pid:
        raise SystemExit("Verified unified API required before retiring historical services")
    retired = []
    for name, port, module in HISTORICAL:
        old_pid = own_pid(name, port, module)
        if old_pid is None:
            retired.append({"port": port, "status": "UNVERIFIED_LEFT_UNTOUCHED" if listening(port) else "ALREADY_OFFLINE"})
            continue
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while listening(port) and time.monotonic() < deadline:
            time.sleep(0.1)
        retired.append({"module": module, "pid": old_pid, "port": port,
                        "ownership": "PID_RECORD_COMMAND_CWD_LISTENER_MATCH",
                        "status": "OFFLINE" if not listening(port) else "SIGTERM_SENT_STILL_LISTENING"})
    return {"current": {"pid": pid, "port": 8773, "url": URL + "/docs"}, "historical": retired,
            "scope": "ONLY_FIXED_TASK_OWNED_API_MODULES"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "consolidate"])
    args = parser.parse_args()
    print(json.dumps(start() if args.command == "start" else consolidate(), indent=2))
