"""Small, secret-free trace records for the built-in annual-report MCP tool."""
from __future__ import annotations

import fcntl
import os
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import canonical

TRACE_VERSION = "forgeflow.report.trace.v1"
CALL_VERSION = "forgeflow.report.tool-call.v1"
RESULT_VERSION = "forgeflow.report.tool-result.v1"
TOOL_NAME = "annual_report.answer.v1"


def hashed(value: object) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def call_input(call_id: str, question: str, index_version: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema_version": CALL_VERSION, "call_id": call_id, "question": question,
            "index_version": index_version, "hits": hits}


def new_trace(*, run_id: str, case_id: str, attempt: int, question: str, index_version: str,
              hits: list[dict[str, Any]]) -> dict[str, Any]:
    trace_id, call_id = uuid4().hex, uuid4().hex
    request = call_input(call_id, question, index_version, hits)
    return {"schema_version": TRACE_VERSION, "trace_id": trace_id, "run_id": run_id,
            "case_id": case_id, "attempt": attempt,
            "producer": {"name": "forgeflow.report_plugin", "version": "v1"},
            "index_version": index_version,
            "request": {"question": question, "input_sha256": hashed(request), "hits": hits},
            "result": None, "terminal_state": "unknown", "events": [], "_call": request}


def event(trace: dict[str, Any], kind: str, status: str, **extra: Any) -> None:
    seq = len(trace["events"]) + 1
    trace["events"].append({"event_id": f"{trace['trace_id']}:{seq}", "seq": seq, "kind": kind,
                            "call_id": trace["_call"]["call_id"], "tool": TOOL_NAME,
                            "status": status, **extra})


def finish(trace: dict[str, Any], result: dict[str, Any] | None, state: str) -> dict[str, Any]:
    trace["result"], trace["terminal_state"] = result, state
    trace.pop("_call", None)
    return trace


def journal(directory: Path | str | None, trace: dict[str, Any]) -> None:
    """Append every terminal trace locally; exports remain the delivery authority."""
    if directory is None:
        return
    target = Path(directory) / "report-tool-traces.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.write(canonical(trace) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
