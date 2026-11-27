"""Fixed-command PTY integrity checks and inherited Manus cursor/lifecycle guarantees."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from forgeflow import pty_capture as pty
from forgeflow.contracts import digest


@pytest.fixture
def export_file(tmp_path):
    payload = {"answer": "合成导出"}
    path = tmp_path / "has spaces.json"
    path.write_text(json.dumps({"schema_version": "forgeflow.research.export.v1", "payload": payload,
                               "digest": digest(payload)}, ensure_ascii=False))
    return path


def test_cursor_overflow_and_split_utf8():
    buf = pty.RingBuffer(8)
    raw = "前后甲乙丙".encode()
    buf.append(raw[:7])
    buf.append(raw[7:])
    assert buf.end == len(raw)
    start, dropped, data = buf.slice(0)
    assert dropped and start == 9 and data.decode() == "乙丙"
    assert buf.slice(0) == (start, dropped, data)  # no destructive read
    assert buf.slice(10) == (12, False, "丙".encode())
    assert buf.slice(buf.end) == (buf.end, False, b"")
    with pytest.raises(ValueError):
        buf.slice(buf.end + 1)


def test_real_pty_verifies_only_integrity_and_nonzero_on_tamper(export_file):
    result = pty.capture_export(export_file)
    assert result["ok"] and result["exit_code"] == 0 and result["utf8_valid"]
    assert result["scope"] == "artifact_integrity_only"
    data = json.loads(export_file.read_text())
    data["payload"]["answer"] = "changed after digest"
    export_file.write_text(json.dumps(data))
    result = pty.capture_export(export_file)
    assert not result["ok"] and result["exit_code"] == 2
    public = subprocess.run([sys.executable, "-m", "forgeflow.pty_capture", str(export_file)], capture_output=True)
    assert public.returncode == 2 and not json.loads(public.stdout)["ok"]


def test_timeout_reaps_process_group(export_file, tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    # Private fault factory is test-only. Production API still takes just a path.
    code = f"import os,time;open({str(pidfile)!r},'w').write(str(os.getpid()));time.sleep(100)"
    monkeypatch.setattr(pty, "_argv", lambda path: [sys.executable, "-c", code])
    result = pty.capture_export(export_file, timeout=.3)
    assert not result["ok"] and result["reason"] == "TIMEOUT" and result["exit_code"] < 0
    assert len(result["artifact_sha256"]) == 64 and result["bytes"] == export_file.stat().st_size
    assert result["scope"] == "artifact_integrity_only"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize("failure", ["spawn", "allocate"])
def test_spawn_error_is_structured_and_keeps_artifact_binding(export_file, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise OSError("injected spawn failure")
    if failure == "spawn":
        monkeypatch.setattr(pty.subprocess, "Popen", fail)
    else:
        monkeypatch.setattr(pty.os, "openpty", fail)
    result = pty.capture_export(export_file)
    assert not result["ok"] and result["reason"] == "VERIFIER_PROCESS_ERROR"
    assert result["exit_code"] is None and len(result["artifact_sha256"]) == 64
    assert result["bytes"] == export_file.stat().st_size


def test_unknown_path_never_becomes_command(tmp_path):
    path = tmp_path / "$(touch should-not-exist).json"
    with pytest.raises(FileNotFoundError):
        pty.capture_export(path)
    assert not Path("should-not-exist").exists()
