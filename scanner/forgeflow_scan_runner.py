"""Trusted scanner image entry point. Source is data; never imported or executed."""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

VERSION = "1.179.0"
MAX_SOURCE = 128 * 1024
MAX_INPUT = MAX_SOURCE * 6 + 2048
MAX_OUTPUT = 192 * 1024


def main() -> None:
    # Independent lifetime bound remains active if the attaching worker dies.
    signal.alarm(28)
    version = importlib.metadata.version("semgrep")
    rules = Path("/opt/rules.yml")
    rules_hash = hashlib.sha256(rules.read_bytes()).hexdigest()
    payload = json.loads(sys.stdin.buffer.read(MAX_INPUT + 1))
    source = payload["source"].encode("utf-8")
    source_hash = hashlib.sha256(source).hexdigest()
    evidence = {
        "version": version,
        "source_hash": source_hash,
        "rules_hash": rules_hash,
        "findings": [],
        "errors": [],
        "status": "ERROR",
        "reason": "",
        "engine_executed": False,
    }
    if (
        version != VERSION
        or payload["source_hash"] != source_hash
        or payload["rules_hash"] != rules_hash
        or not source
        or len(source) > MAX_SOURCE
    ):
        evidence["reason"] = "RUNNER_INPUT_PROVENANCE_MISMATCH"
        print(json.dumps(evidence))
        return
    # Semgrep may recover from some invalid syntax without reporting errors.
    # Parse (never execute) in this same resource-bounded container, fail closed.
    try:
        ast.parse(source, filename="submitted.py")
    except (SyntaxError, ValueError, RecursionError):
        evidence["reason"] = "PYTHON_SYNTAX_INVALID"
        print(json.dumps(evidence))
        return
    Path("/work/submitted.py").write_bytes(source)
    command = [
        "semgrep",
        "scan",
        "--oss-only",
        "--config",
        str(rules),
        "--json",
        "--quiet",
        "--strict",
        "--error",
        "--disable-nosem",
        "--disable-version-check",
        "--metrics",
        "off",
        "--no-rewrite-rule-ids",
        "--no-git-ignore",
        "--jobs",
        "1",
        "--timeout",
        "5",
        "--max-memory",
        "384",
        "--max-target-bytes",
        str(MAX_SOURCE),
        "/work/submitted.py",
    ]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    evidence["engine_executed"] = True
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    assert proc.stdout is not None and proc.stderr is not None
    for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    deadline = time.monotonic() + 24
    failed = ""
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failed = "SCANNER_INNER_TIMEOUT"
            break
        for key, _ in selector.select(min(0.05, remaining)):
            try:
                chunk = os.read(key.fileobj.fileno(), 16384)
            except BlockingIOError:
                continue
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            buffer = output[key.data]
            if len(buffer) + len(chunk) > MAX_OUTPUT:
                failed = "SCANNER_INNER_OUTPUT_LIMIT"
                break
            buffer.extend(chunk)
        if failed:
            break
    selector.close()
    if failed:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        failed = "SCANNER_INNER_TIMEOUT"
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=1)
    if failed:
        evidence.update(status="TIMED_OUT" if "TIMEOUT" in failed else "ERROR", reason=failed)
    else:
        try:
            report = json.loads(output["stdout"])
            evidence["errors"] = report.get("errors", [])[:16]
            found = report.get("results", [])
            scanned = report.get("paths", {}).get("scanned", [])
            if len(found) > 128:
                evidence["reason"] = "TOO_MANY_FINDINGS"
            elif evidence["errors"] or proc.returncode not in (0, 1) or scanned != ["/work/submitted.py"]:
                evidence["reason"] = "SCAN_INCOMPLETE_OR_ERROR"
            else:
                evidence["findings"] = [
                    {
                        "rule_id": item["check_id"],
                        "line": item["start"]["line"],
                        "column": item["start"]["col"],
                        "message": item["extra"]["message"],
                        "severity": item["extra"]["severity"],
                    }
                    for item in found
                ]
                evidence["status"] = "REJECT" if found else "PASS"
                evidence["reason"] = "RULE_FINDINGS" if found else "NO_RULE_FINDINGS"
        except (ValueError, TypeError, KeyError):
            evidence["reason"] = "SCAN_JSON_INVALID"
    print(json.dumps(evidence, separators=(",", ":")))


if __name__ == "__main__":
    main()
