"""Replace only the verified task-owned EVT or Fabric API on loopback 8773."""

import argparse
import json
import os
import signal
import time

from evt_service import URL, health, launch
from prototype_service import listening, own_pid

from forgeflow.evt_review import REVISION as EVT_REVISION
from forgeflow.fabric_contract import REVISION

MODULE = "forgeflow.fabric_api"
PRIOR = ("forgeflow.evt_api", "evt", EVT_REVISION)
CURRENT = (MODULE, "fabric", REVISION)


def main(restart=False):
    old_pid, restore = None, None
    if listening(8773):
        data = health()
        if data and data.get("revision") == REVISION:
            restore = CURRENT
        elif data and data.get("revision") == EVT_REVISION:
            restore = PRIOR
        else:
            raise SystemExit("Unverified port occupant left untouched")
        old_pid = own_pid(restore[1], 8773, restore[0])
        if not old_pid or data.get("process_id") != old_pid:
            raise SystemExit("Unverified port occupant left untouched")
        if restore == CURRENT and not restart:
            print(json.dumps({"status": "ALREADY_RUNNING", "pid": old_pid, "url": URL + "/docs"}))
            return
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while listening(8773) and time.monotonic() < deadline:
            time.sleep(0.1)
        if listening(8773):
            raise SystemExit("Verified old API still listening; no new process started")
    failure = None
    try:
        pid = launch(*CURRENT)
    except OSError as error:
        pid, failure = None, type(error).__name__
    if not pid:
        try:
            restored = launch(*restore) if old_pid else None
        except OSError as error:
            raise SystemExit("Fabric API failed; prior module restore failed: " + type(error).__name__) from error
        raise SystemExit("Fabric API failed " + str(failure) + "; matching prior module restore PID=" + str(restored))
    print(json.dumps({"status": "RUNNING", "pid": pid, "url": URL + "/docs", "replaced_own_pid": old_pid,
                      "ownership": "PID_COMMAND_CWD_LISTENER_AND_HEALTH_MATCH", "other_project_actions": "NONE"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true")
    main(parser.parse_args().restart)
