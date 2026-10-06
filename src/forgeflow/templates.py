"""Trusted, synthetic checker templates; these are not generated agent code."""

from __future__ import annotations

import hashlib

EVT_CHECK = """import json, os, sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text())
if not isinstance(manifest, dict):
    raise ValueError("manifest must be an object")
result = {"source": "SYNTHETIC_FIXTURE", "schema_version": "evt-check-v1",
          "manifest": manifest,
          "runtime": {"uid": os.getuid(), "gid": os.getgid(),
                      "seccomp": next((x.split(":", 1)[1].strip() for x in
                          Path("/proc/self/status").read_text().splitlines()
                          if x.startswith("Seccomp:")), "unknown")}}
Path("/outputs/result.json").write_text(json.dumps(result, sort_keys=True))
print("Synthetic manifest validated; trusted control-plane rules evaluate readiness.")
"""

TIMEOUT = """import time
print("timeout fixture started", flush=True)
while True:
    time.sleep(0.05)
"""

NETWORK_PROBE = """import json, os, socket
from pathlib import Path

results = {}
for name, address in (("tcp", ("1.1.1.1", 443)),
                      ("metadata", ("169.254.169.254", 80))):
    sock = socket.socket()
    sock.settimeout(0.8)
    try:
        sock.connect(address)
        results[name] = "UNEXPECTED_CONNECTED"
    except OSError:
        results[name] = "DENIED"
    finally:
        sock.close()
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.sendto(b"synthetic probe", ("1.1.1.1", 53))
    results["udp"] = "UNEXPECTED_SENT"
except OSError:
    results["udp"] = "DENIED"
finally:
    sock.close()
try:
    socket.getaddrinfo("forgeflow-invalid.example", 443)
    results["dns"] = "UNEXPECTED_RESOLVED"
except OSError:
    results["dns"] = "DENIED"
try:
    Path("/forgeflow-readonly-probe").write_text("synthetic")
    results["rootfs"] = "UNEXPECTED_WRITABLE"
except OSError:
    results["rootfs"] = "DENIED"
results["uid"] = os.getuid()
results["socket_present"] = Path("/var/run/docker.sock").exists()
results["seccomp"] = next((x.split(":", 1)[1].strip() for x in
    Path("/proc/self/status").read_text().splitlines() if x.startswith("Seccomp:")), "unknown")
Path("/outputs/result.json").write_text(json.dumps(results, sort_keys=True))
print(json.dumps(results, sort_keys=True))
"""

OUTPUT_FLOOD = """import sys
while True:
    sys.stdout.write("x" * 4096)
    sys.stdout.flush()
"""

TEMPLATES = {
    "evt-check": EVT_CHECK,
    "timeout": TIMEOUT,
    "network-probe": NETWORK_PROBE,
    "output-flood": OUTPUT_FLOOD,
}


def template_source(template_id: str) -> str:
    try:
        return TEMPLATES[template_id]
    except KeyError:
        raise ValueError("Unknown template") from None


def template_hash(template_id: str) -> str:
    return hashlib.sha256(template_source(template_id).encode()).hexdigest()


RUNNER = """import math, os, resource, signal, subprocess, time
resource.setrlimit(resource.RLIMIT_FSIZE, (2097152, 2097152))
timeout = float(os.environ["FORGEFLOW_CHECK_TIMEOUT"])
# An unpaused orphan container exits independently; recovery also handles paused orphans.
signal.alarm(math.ceil(timeout + 15))
try:
    result = subprocess.run(["/usr/local/bin/python", "-I", "/inputs/check.py", "/inputs/input.json"],
                            timeout=timeout)
    print("\\nFORGEFLOW_DONE:" + str(result.returncode), flush=True)
except subprocess.TimeoutExpired:
    print("\\nFORGEFLOW_TIMEOUT", flush=True)
# Keep tmpfs alive until the controller freezes and exports the bounded artifact.
while True:
    time.sleep(1)
"""
