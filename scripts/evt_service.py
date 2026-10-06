"""Replace only the verified previous prototype on 8773 with the EVT-contract API."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time

import httpx
from prototype_service import ROOT, RUNTIME, listening, own_pid

from forgeflow.evt_review import REVISION

MODULE = "forgeflow.evt_api"
URL = "http://127.0.0.1:8773"


def health():
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(URL + "/health")
            if response.status_code == 200:
                return response.json()
    except (httpx.HTTPError, ValueError):
        pass
    return None


def launch(module, name, revision):
    descriptor = os.open(RUNTIME / f"{name}-api.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "ab") as log:
        child = subprocess.Popen([sys.executable, "-m", module], cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = time.monotonic() + 10
    while child.poll() is None and time.monotonic() < deadline:
        data = health()
        if data and data.get("revision") == revision and data.get("process_id") == child.pid:
            path = RUNTIME / f"{name}-api.pid"
            path.write_text(str(child.pid) + "\n")
            path.chmod(0o600)
            return child.pid
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
    return None


def main(restart=False):
    data = health()
    old_pid = None
    restore = ("forgeflow.prototype_api", "prototype", "domain-source-contract-v1")
    if listening(8773):
        if data and data.get("revision") == REVISION:
            old_pid = own_pid("evt", 8773, MODULE)
            if old_pid and data.get("process_id") == old_pid and not restart:
                print(json.dumps({"status": "ALREADY_RUNNING", "pid": old_pid, "url": URL + "/docs"}))
                return
            restore = (MODULE, "evt", REVISION)
        else:
            old_pid = own_pid("prototype", 8773, "forgeflow.prototype_api")
        if not old_pid or not data or data.get("revision") != restore[2] or data.get("process_id") != old_pid:
            raise SystemExit("Unverified port occupant left untouched")
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while listening(8773) and time.monotonic() < deadline:
            time.sleep(0.1)
        if listening(8773):
            raise SystemExit("Verified old prototype still listening; no new process started")
    pid = launch(MODULE, "evt", REVISION)
    if not pid:
        restored = launch(*restore) if old_pid else None
        raise SystemExit("EVT API failed; prior prototype restore PID=" + str(restored))
    print(json.dumps({"status": "RUNNING", "pid": pid, "url": URL + "/docs", "replaced_own_pid": old_pid,
                      "ownership": "PID_COMMAND_CWD_LISTENER_AND_HEALTH_MATCH", "other_project_actions": "NONE"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true")
    main(parser.parse_args().restart)
