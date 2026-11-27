"""Fixed read-only export verifier, using the Manus Q5 cursor/buffer/PTY lifecycle.

This checks artifact integrity, not approval authority or numerical correctness.
The public interface accepts a file, never a shell command or arbitrary argv.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .contracts import canonical, digest


class RingBuffer:
    """Absolute byte cursors; reads are repeatable and report discarded bytes."""

    def __init__(self, capacity: int = 65536):
        if capacity < 4:
            raise ValueError("capacity must hold one UTF-8 codepoint")
        self.capacity, self.start, self.data = capacity, 0, bytearray()

    @property
    def end(self) -> int:
        return self.start + len(self.data)

    def append(self, value: bytes) -> None:
        self.data.extend(value)
        over = max(0, len(self.data) - self.capacity)
        # Do not retain a suffix beginning halfway through a codepoint.
        while over < len(self.data) and self.data[over] & 0xC0 == 0x80:
            over += 1
        if over:
            del self.data[:over]
            self.start += over

    def slice(self, cursor: int = 0) -> tuple[int, bool, bytes]:
        if type(cursor) is not int or cursor < 0 or cursor > self.end:
            raise ValueError("invalid absolute cursor")
        effective = max(cursor, self.start)
        offset = effective - self.start
        while offset < len(self.data) and self.data[offset] & 0xC0 == 0x80:
            offset += 1
        return self.start + offset, cursor < self.start, bytes(self.data[offset:])


def check_export(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
        ok = (isinstance(data, dict) and data.get("schema_version") == "forgeflow.research.export.v1"
              and isinstance(data.get("payload"), dict) and data.get("digest") == digest(data["payload"]))
    except (ValueError, UnicodeError):
        ok = False
    return {"ok": ok, "artifact_sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "scope": "artifact_integrity_only", "reason": "OK" if ok else "EXPORT_DIGEST_INVALID"}


def _argv(path: Path) -> list[str]:
    return [sys.executable, "-m", "forgeflow.pty_capture", "--_verify", str(path)]


def _stop_group(proc: subprocess.Popen) -> None:
    # The verifier is our own session leader. Kill its group, including a child
    # that might have survived its parent; never target the caller's group.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=2)


def capture_export(path: Path, timeout: float = 10.0) -> dict[str, Any]:
    path = path.resolve(strict=True)
    if not path.is_file() or timeout <= 0:
        raise ValueError("a file and positive timeout are required")
    artifact = path.read_bytes()
    metadata = {"artifact_sha256": hashlib.sha256(artifact).hexdigest(), "bytes": len(artifact),
                "scope": "artifact_integrity_only"}
    try:
        master, slave = os.openpty()
    except OSError as exc:
        return metadata | {"ok": False, "reason": "VERIFIER_PROCESS_ERROR", "error_type": type(exc).__name__,
                           "exit_code": None, "cursor": 0, "start_cursor": 0, "overflow": False, "utf8_valid": True}
    proc = None
    launch_error = None
    buf, timed_out = RingBuffer(), False
    try:
        proc = subprocess.Popen(_argv(path), stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
                                close_fds=True, start_new_session=True,
                                env={"PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                                     "PYTHONIOENCODING": "utf-8"})
        os.close(slave)
        slave = -1
        os.set_blocking(master, False)
        deadline = time.monotonic() + timeout
        eof = False
        while not eof:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            ready, _, _ = select.select([master], [], [], min(0.05, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            try:
                block = os.read(master, 65536)
                eof = not block
                buf.append(block)
            except OSError as exc:
                if exc.errno == errno.EIO:  # PTY EOF on macOS/Linux
                    eof = True
                elif exc.errno != errno.EAGAIN:
                    raise
        if not timed_out:
            try:
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
    except OSError as exc:
        launch_error = type(exc).__name__
    finally:
        if slave >= 0:
            os.close(slave)
        if proc is not None:
            _stop_group(proc)
        os.close(master)
    if launch_error:
        return metadata | {"ok": False, "reason": "VERIFIER_PROCESS_ERROR", "error_type": launch_error,
                           "exit_code": proc.returncode if proc else None, "cursor": buf.end,
                           "start_cursor": buf.start, "overflow": buf.start > 0, "utf8_valid": True}
    start, dropped, raw = buf.slice(0)
    try:
        output = raw.decode("utf-8")
        utf8_valid = True
    except UnicodeDecodeError:
        output, utf8_valid = raw.decode("utf-8", "replace"), False
    try:
        value = json.loads(output.strip())
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise ValueError("invalid verifier object")
    except ValueError:
        value = {"ok": False, "reason": "INVALID_VERIFIER_OUTPUT"}
    if timed_out:
        value = {"ok": False, "reason": "TIMEOUT"}
    elif proc.returncode != 0 or dropped or not utf8_valid:
        value["ok"] = False
        value["reason"] = "VERIFIER_FAILED"
    if value.get("ok") and value.get("artifact_sha256") != metadata["artifact_sha256"]:
        value.update(ok=False, reason="ARTIFACT_CHANGED_DURING_CHECK")
    return value | metadata | {"exit_code": proc.returncode, "cursor": buf.end, "start_cursor": start,
                    "overflow": dropped, "utf8_valid": utf8_valid}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?")
    parser.add_argument("--_verify")
    args = parser.parse_args()
    if bool(args.path) == bool(args._verify):
        parser.error("provide exactly one export path")
    try:
        target = Path(args._verify or args.path)
        result = check_export(target) if args._verify else capture_export(target)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "reason": type(exc).__name__}
    print(canonical(result))
    raise SystemExit(0 if result["ok"] else 2)


if __name__ == "__main__":
    main()
