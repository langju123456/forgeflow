from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time

import pytest

import forgeflow.scanner as scanner_module
from forgeflow.scanner import ENGINE_VERSION, ProcessResult, Scanner, bounded_process

IMAGE = "sha256:" + "a" * 64
SOURCE = 'import json\nprint(json.dumps({"ok": True}))\n'
HASH = hashlib.sha256(SOURCE.encode()).hexdigest()


@pytest.mark.parametrize(
    "text,confirmed",
    [
        (b"error: no such object: ffs-abc", True),
        (b"Error: No such container: ffs-abc", True),
        (b"connect to docker.sock: no such file or directory", False),
        (b"error: no such object: unrelated", False),
        (b"error: no such object: ffs-abc-other", False),
    ],
)
def test_cleanup_absence_requires_exact_object_not_daemon_error(text, confirmed):
    assert Scanner._confirmed_absent(ProcessResult(1, stderr=text), "ffs-abc") is confirmed


def test_cancelled_inspect_never_confirms_cleanup():
    result = ProcessResult(1, stderr=b"error: no such object: ffs-abc", cancelled=True)
    assert not Scanner._confirmed_absent(result, "ffs-abc")


IDENTITY = {
    "scope": {"tenant_id": "tenant-a", "workspace_id": "workspace-a", "project_id": "aurora"},
    "run_id": "run-a",
    "step_id": "scan-step",
    "attempt": 1,
}


@pytest.fixture(autouse=True)
def isolated_lifecycle_root(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner_module, "LIFECYCLE_ROOT", tmp_path / "private-lifecycle")


def config():
    return {
        "Config": {
            "User": "10001:10001",
            "Labels": {"forgeflow.owner": "forgeflow-task6", "forgeflow.kind": "scanner"},
        },
        "Mounts": [],
        "HostConfig": {
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "Privileged": False,
            "Binds": None,
            "LogConfig": {"Type": "none"},
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges=true"],
            "Memory": 536870912,
            "MemorySwap": 536870912,
            "PidsLimit": 64,
            "NanoCpus": 1000000000,
            "Init": True,
            "Tmpfs": {
                "/work": "rw,nosuid,nodev,noexec,size=16777216,uid=10001,gid=10001,mode=0700",
                "/tmp": "rw,nosuid,nodev,noexec,size=67108864,uid=10001,gid=10001,mode=0700",
            },
            "PortBindings": {},
        },
    }


class FakeScanner(Scanner):
    """Protocol fixture, not a real Semgrep scan. Real cases are separately marked integration."""

    def __init__(self, *, envelope=None, scan=None, unsafe=False, cleanup_fail=False):
        super().__init__(IMAGE)
        self.calls = []
        self.envelope = envelope
        self.scan = scan
        self.unsafe = unsafe
        self.cleanup_fail = cleanup_fail
        self.labels = {}
        self.created = False

    def _run(self, args, *, timeout=10, data=b"", cancel_check=None):
        self.calls.append((args, data))
        if args[:2] == ["image", "inspect"]:
            labels = {
                "forgeflow.scanner.version": ENGINE_VERSION,
                "forgeflow.scanner.rules_hash": hashlib.sha256(self.rules_path.read_bytes()).hexdigest(),
            }
            return ProcessResult(0, json.dumps([{"Config": {"Labels": labels}}]).encode())
        if args[0] == "create":
            self.labels = {
                args[i + 1].split("=", 1)[0]: args[i + 1].split("=", 1)[1]
                for i, value in enumerate(args)
                if value == "--label"
            }
            self.created = True
            return ProcessResult(0, b"id")
        if args[0] == "inspect":
            if not self.created:
                return ProcessResult(1, stderr=("Error: No such object: " + args[1]).encode())
            value = config()
            value["Config"]["Labels"] = self.labels
            if self.unsafe:
                value["HostConfig"]["NetworkMode"] = "host"
            return ProcessResult(0, json.dumps([value]).encode())
        if args[0] == "start":
            if self.scan:
                return self.scan
            evidence = {
                "source_hash": HASH,
                "rules_hash": hashlib.sha256(self.rules_path.read_bytes()).hexdigest(),
                "version": ENGINE_VERSION,
                "findings": [],
                "errors": [],
                "status": "PASS",
                "engine_executed": True,
            }
            evidence.update(self.envelope or {})
            return ProcessResult(0, json.dumps(evidence).encode())
        if args[0] == "rm":
            if not self.cleanup_fail:
                self.created = False
            return ProcessResult(1 if self.cleanup_fail else 0)
        raise AssertionError(args)


def test_hash_mismatch_rejected_before_engine():
    scanner = FakeScanner()
    result = scanner.scan_source(SOURCE, "b" * 64)
    assert result["status"] == "REJECT"
    assert result["reason"] == "SOURCE_HASH_MISMATCH"
    assert not scanner.calls


@pytest.mark.parametrize("image", [None, "semgrep:latest", "sha256:bad"])
def test_missing_or_mutable_image_never_passes(image):
    assert Scanner(image).scan_source(SOURCE, HASH)["status"] == "UNAVAILABLE"


def test_protocol_success_is_bound_to_source_and_rules():
    scanner = FakeScanner()
    result = scanner.scan_source(SOURCE, HASH)
    assert result["status"] == "PASS"
    assert result["version"] == ENGINE_VERSION and result["cleanup"] == "REMOVED"
    assert result["inspect"]["log_driver"] == "none"
    payload = json.loads(next(data for args, data in scanner.calls if args[0] == "start"))
    assert payload["source"] == SOURCE and payload["source_hash"] == HASH


def test_handle_is_persisted_before_any_create_and_callback_error_prevents_start():
    scanner = FakeScanner()
    seen = []

    def record(handle):
        assert not any(args[0] == "create" for args, _ in scanner.calls)
        seen.append(handle)

    scanner.on_handle = record
    result = scanner.scan_source(SOURCE, HASH)
    assert seen == [result["handle"]] and result["status"] == "PASS"
    blocked = FakeScanner()

    def cannot_persist(_handle):
        raise RuntimeError("database unavailable")

    blocked.on_handle = cannot_persist
    assert blocked.scan_source(SOURCE, HASH)["reason"] == "SCANNER_HANDLE_NOT_PERSISTED"
    assert not any(args[0] == "create" for args, _ in blocked.calls)


def test_recovery_rejects_untrusted_handle_without_docker():
    scanner = FakeScanner()
    assert scanner.recover("some-other-project")["cleanup"] == "FAILED"
    assert not scanner.calls


def test_deterministic_full_scope_handle_matches_canonical_contract():
    material = {
        "scope": IDENTITY["scope"],
        "run": "run-a",
        "step": "scan-step",
        "attempt": 1,
        "kind": "scanner",
    }
    expected = (
        "ffs-"
        + hashlib.sha256(
            json.dumps(
                material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()[:32]
    )
    assert Scanner.handle(IDENTITY["scope"], "run-a", "scan-step", 1) == expected
    scanner = FakeScanner()
    result = scanner.scan_source(SOURCE, HASH, handle=expected, identity=IDENTITY)
    assert result["status"] == "PASS" and result["handle"] == expected
    assert scanner.labels["forgeflow.run"] == "run-a"
    assert scanner.cleanup(expected, identity=IDENTITY)["cleanup"] == "REMOVED"


def test_cross_scope_handle_and_cleanup_rejected_before_create_or_delete():
    handle = Scanner.handle(IDENTITY["scope"], "run-a", "scan-step", 1)
    wrong_scope = IDENTITY["scope"] | {"project_id": "other-project"}
    wrong_identity = IDENTITY | {"scope": wrong_scope}
    scanner = FakeScanner()
    assert scanner.scan_source(SOURCE, HASH, handle=handle, identity=wrong_identity)["status"] == "REJECT"
    assert not any(args[0] == "create" for args, _ in scanner.calls)
    scanner.calls.clear()
    assert scanner.cleanup(handle, identity=wrong_identity)["cleanup"] == "FAILED"
    assert not scanner.calls


def test_expired_lease_prevents_create_even_if_it_expires_in_persist_callback():
    handle = Scanner.handle(IDENTITY["scope"], "run-a", "scan-step", 1)
    scanner = FakeScanner()
    assert (
        scanner.scan_source(SOURCE, HASH, handle=handle, identity=IDENTITY, lease_deadline=time.time() - 1)[
            "reason"
        ]
        == "SCANNER_LEASE_EXPIRED"
    )
    assert not any(args[0] == "create" for args, _ in scanner.calls)
    scanner.on_handle = lambda _: time.sleep(0.02)
    assert (
        scanner.scan_source(
            SOURCE, HASH, handle=handle, identity=IDENTITY, lease_deadline=time.time() + 0.01
        )["reason"]
        == "SCANNER_LEASE_EXPIRED"
    )
    assert not any(args[0] == "create" for args, _ in scanner.calls)


def test_recovery_of_not_yet_created_attempt_permanently_blocks_late_create():
    scanner = FakeScanner()
    handle = Scanner.handle(IDENTITY["scope"], "run-a", "scan-step", 1)
    assert scanner.cleanup(handle, identity=IDENTITY)["cleanup"] == "REMOVED"
    assert scanner._revoked(handle)
    scanner.calls.clear()
    result = scanner.scan_source(SOURCE, HASH, handle=handle, identity=IDENTITY)
    assert result["status"] == "REJECT" and result["reason"] == "SCANNER_REVOKED_OR_LEASE_EXPIRED"
    assert not any(args[0] == "create" for args, _ in scanner.calls)


def test_recovery_revokes_before_lock_then_waits_for_inflight_create_cleanup():
    entered = threading.Event()
    release = threading.Event()

    class PausedCreate(FakeScanner):
        def _run(self, args, **kwargs):
            if args[0] == "create":
                entered.set()
                assert release.wait(timeout=2)
            return super()._run(args, **kwargs)

    scanner = PausedCreate()
    handle = Scanner.handle(IDENTITY["scope"], "run-a", "scan-step", 1)
    outcomes = {}
    worker = threading.Thread(
        target=lambda: outcomes.update(
            scan=scanner.scan_source(SOURCE, HASH, handle=handle, identity=IDENTITY)
        )
    )
    worker.start()
    assert entered.wait(timeout=1)
    cleanup = threading.Thread(
        target=lambda: outcomes.update(cleanup=scanner.cleanup(handle, identity=IDENTITY))
    )
    cleanup.start()
    deadline = time.monotonic() + 1
    while not scanner._revoked(handle) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scanner._revoked(handle) and cleanup.is_alive()
    release.set()
    worker.join(timeout=2)
    cleanup.join(timeout=2)
    assert not worker.is_alive() and not cleanup.is_alive()
    assert outcomes["scan"]["status"] == "REJECT"
    assert outcomes["cleanup"]["cleanup"] == "REMOVED"
    assert not scanner.created
    assert not any(args[0] == "start" for args, _ in scanner.calls)


def test_private_lifecycle_root_rejects_symlink_without_create(tmp_path, monkeypatch):
    other = tmp_path / "other-project"
    other.mkdir()
    bad = tmp_path / "scanner-root-link"
    bad.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(scanner_module, "LIFECYCLE_ROOT", bad)
    scanner = FakeScanner()
    result = scanner.scan_source(SOURCE, HASH)
    assert result["status"] == "ERROR" and result["reason"] == "SCANNER_LIFECYCLE_LOCK_FAILED"
    assert not any(args[0] == "create" for args, _ in scanner.calls)
    assert not list(other.iterdir())


def test_bounded_attach_can_stop_on_revocation():
    result = bounded_process(
        [sys.executable, "-c", "import time; time.sleep(20)"], timeout=2, cancel_check=lambda: True
    )
    assert result.cancelled and result.returncode is not None


@pytest.mark.parametrize(
    "envelope",
    [
        {"source_hash": "b" * 64},
        {"rules_hash": "b" * 64},
        {"version": "wrong"},
        {"findings": [{"rule_id": "bad"}]},
        {"errors": [{"type": "parse"}]},
        {"engine_executed": False},
    ],
)
def test_untrusted_provenance_or_contradictory_pass_fails_closed(envelope):
    result = FakeScanner(envelope=envelope).scan_source(SOURCE, HASH)
    assert result["status"] == "ERROR" and result["cleanup"] == "REMOVED"


@pytest.mark.parametrize(
    "scan, status, reason",
    [
        (ProcessResult(-9, timed_out=True), "TIMED_OUT", "SCANNER_WALL_TIMEOUT"),
        (ProcessResult(-9, output_limit=True), "ERROR", "SCANNER_OUTPUT_LIMIT"),
        (ProcessResult(2), "ERROR", "SCANNER_RUNNER_FAILED"),
        (ProcessResult(0, b"not-json"), "ERROR", "SCANNER_INVALID_RESPONSE"),
    ],
)
def test_faults_are_fail_closed_and_cleaned(scan, status, reason):
    result = FakeScanner(scan=scan).scan_source(SOURCE, HASH)
    assert (result["status"], result["reason"], result["cleanup"]) == (status, reason, "REMOVED")


def test_isolation_mismatch_prevents_start():
    scanner = FakeScanner(unsafe=True)
    assert scanner.scan_source(SOURCE, HASH)["status"] == "ERROR"
    assert not any(args[0] == "start" for args, _ in scanner.calls)


def test_cleanup_failure_prevents_pass():
    result = FakeScanner(cleanup_fail=True).scan_source(SOURCE, HASH)
    assert (result["status"], result["reason"], result["cleanup"]) == (
        "ERROR",
        "SCANNER_CLEANUP_FAILED",
        "FAILED",
    )


def test_stream_flood_is_capped_without_unbounded_communicate():
    result = bounded_process(
        [sys.executable, "-c", "import os; os.write(1, b'x'*1000000)"], timeout=2, output_limit=1024
    )
    assert result.output_limit and len(result.stdout) == 1024


def test_wall_timeout_kills_local_cli_group():
    result = bounded_process([sys.executable, "-c", "import time; time.sleep(20)"], timeout=0.05)
    assert result.timed_out and result.returncode is not None


def test_binary_missing_never_passes():
    result = bounded_process(["/no/forgeflow-binary"], timeout=1)
    assert result.unavailable and result.returncode is None


@pytest.mark.skipif(not os.getenv("FORGEFLOW_SCANNER_IMAGE"), reason="Pinned Semgrep image not configured")
@pytest.mark.parametrize(
    "source, expected",
    [
        (SOURCE, "PASS"),
        ('eval("2+2")\n', "REJECT"),
        ('eval("2+2")  # nosemgrep\n', "REJECT"),
        ('import os\nos.system("echo unsafe")\n', "REJECT"),
        ("def syntax(:\n", "ERROR"),
    ],
)
def test_real_isolated_semgrep(source, expected):
    scanner = Scanner(os.environ["FORGEFLOW_SCANNER_IMAGE"])
    result = scanner.scan_source(source, hashlib.sha256(source.encode()).hexdigest())
    assert result["status"] == expected, result
    assert result["version"] == ENGINE_VERSION, result
    assert result["cleanup"] == "REMOVED", result
    assert result["inspect"]["network"] == "none"


@pytest.mark.skipif(not os.getenv("FORGEFLOW_SCANNER_IMAGE"), reason="Pinned Semgrep image not configured")
def test_real_isolated_scanner_wall_timeout():
    result = Scanner(os.environ["FORGEFLOW_SCANNER_IMAGE"], timeout_seconds=0.01).scan_source(SOURCE, HASH)
    assert result["status"] == "TIMED_OUT" and result["cleanup"] == "REMOVED", result
