"""Fail-closed, Docker-isolated Semgrep CE adapter.

The control plane executes only fixed Docker CLI commands. Submitted Python is
sent as data to a pinned scanner image and is never executed on the host. A PASS
means this finite rule set found no matches, not that the program is safe.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ENGINE_VERSION = "1.179.0"
PROFILE = "checker-scan-v1"
OWNER = "forgeflow-task6"
MAX_SOURCE = 128 * 1024
OUTPUT_LIMIT = 256 * 1024
RULES_PATH = Path(__file__).resolve().parents[2] / "scanner" / "rules.yml"
LIFECYCLE_ROOT = Path("/tmp") / ("forgeflow-scanner-" + str(os.getuid()))


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    output_limit: bool = False
    unavailable: bool = False
    cancelled: bool = False


def bounded_process(
    argv: list[str],
    *,
    timeout: float,
    data: bytes = b"",
    output_limit: int = OUTPUT_LIMIT,
    cancel_check: Callable[[], bool] | None = None,
) -> ProcessResult:
    """Bound both streams while writing finite stdin; kill the CLI process group.

    Docker containers need independent exact-handle cleanup after this function.
    This helper does not run submitted code and is not a host sandbox.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return ProcessResult(None, unavailable=True)
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    if data:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        proc.stdin.close()
    pending = memoryview(data)
    deadline = time.monotonic() + timeout
    timed_out = limit_exceeded = cancelled = False
    try:
        while selector.get_map():
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(0.05, remaining)):
                if key.data == "stdin":
                    try:
                        written = os.write(key.fileobj.fileno(), pending[:16384])
                        pending = pending[written:]
                    except (BrokenPipeError, OSError):
                        pending = memoryview(b"")
                    if not pending:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fileobj.fileno(), 16384)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = streams[key.data]
                room = output_limit - len(target)
                target.extend(chunk[:room])
                if len(chunk) > room:
                    limit_exceeded = True
                    break
            if limit_exceeded:
                break
        if timed_out or limit_exceeded or cancelled:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=1)
    finally:
        selector.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if not stream.closed:
                stream.close()
    return ProcessResult(
        proc.returncode,
        bytes(streams["stdout"]),
        bytes(streams["stderr"]),
        timed_out,
        limit_exceeded,
        cancelled=cancelled,
    )


class Scanner:
    """Trusted configuration supplies an immutable image ID/digest, never a tool argument."""

    def __init__(
        self,
        image: str | None = None,
        *,
        docker: str = "docker",
        timeout_seconds: float = 30,
        rules_path: Path | None = None,
        on_handle: Callable[[str], None] | None = None,
    ):
        self.image = image
        self.docker = docker
        self.timeout_seconds = min(max(timeout_seconds, 0.01), 60)
        self.rules_path = rules_path or RULES_PATH
        # The orchestrator binds this callback to its authenticated scoped step,
        # atomically storing the handle before any container is created.
        self.on_handle = on_handle

    def _run(
        self,
        args: list[str],
        *,
        timeout: float = 10,
        data: bytes = b"",
        cancel_check: Callable[[], bool] | None = None,
    ) -> ProcessResult:
        return bounded_process([self.docker, *args], timeout=timeout, data=data, cancel_check=cancel_check)

    @staticmethod
    def _root() -> Path:
        LIFECYCLE_ROOT.mkdir(mode=0o700, exist_ok=True)
        details = LIFECYCLE_ROOT.lstat()
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
        ):
            raise PermissionError("Scanner lifecycle root ownership rejected")
        return LIFECYCLE_ROOT

    def _lock(self, handle: str, timeout: float = 10) -> int:
        if not re.fullmatch(r"ffs-[0-9a-f]{32}", handle):
            raise ValueError("Invalid scanner lifecycle handle")
        fd = os.open(self._root() / (handle + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or details.st_nlink != 1
        ):
            os.close(fd)
            raise PermissionError("Scanner lifecycle lock ownership rejected")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise TimeoutError("Scanner lifecycle lock timed out") from None
                time.sleep(0.02)

    @staticmethod
    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _revoke(self, handle: str) -> None:
        fd = os.open(self._root() / (handle + ".revoked"), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            details = os.fstat(fd)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) & 0o077
                or details.st_nlink != 1
            ):
                raise PermissionError("Scanner revocation ownership rejected")
            os.write(fd, b"revoked\n")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _revoked(self, handle: str) -> bool:
        try:
            (self._root() / (handle + ".revoked")).lstat()
            return True
        except FileNotFoundError:
            return False

    def _stop(self, handle: str, lease_deadline: float | None) -> bool:
        return self._revoked(handle) or (lease_deadline is not None and time.time() >= lease_deadline)

    @staticmethod
    def handle(scope: object, run_id: str, step_id: str, attempt: int) -> str:
        scope_value = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope
        material = {
            "scope": scope_value,
            "run": run_id,
            "step": step_id,
            "attempt": attempt,
            "kind": "scanner",
        }
        canonical = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return "ffs-" + hashlib.sha256(canonical.encode()).hexdigest()[:32]

    @staticmethod
    def _identity_labels(identity: dict) -> dict[str, str]:
        if set(identity) != {"scope", "run_id", "step_id", "attempt"}:
            raise ValueError("invalid identity fields")
        scope = identity["scope"]
        if hasattr(scope, "model_dump"):
            scope = scope.model_dump(mode="json")
        if not isinstance(scope, dict) or set(scope) != {"tenant_id", "workspace_id", "project_id"}:
            raise ValueError("invalid scope")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) for value in scope.values()
        ):
            raise ValueError("invalid scope values")
        if not all(
            isinstance(identity[key], str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identity[key])
            for key in ("run_id", "step_id")
        ):
            raise ValueError("invalid run or step")
        if type(identity["attempt"]) is not int or identity["attempt"] < 1:
            raise ValueError("invalid attempt")
        scope_hash = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        return {
            "forgeflow.owner": OWNER,
            "forgeflow.kind": "scanner",
            "forgeflow.scope_hash": scope_hash,
            "forgeflow.run": identity["run_id"],
            "forgeflow.step": identity["step_id"],
            "forgeflow.attempt": str(identity["attempt"]),
        }

    def scan(
        self,
        template_id: str,
        *,
        handle: str | None = None,
        identity: dict | None = None,
        lease_deadline: float | None = None,
    ) -> dict:
        """Compatibility adapter for the fixed, trusted S1/S2 template catalogue."""
        from forgeflow.templates import template_hash, template_source

        try:
            source = template_source(template_id)
            source_hash = template_hash(template_id)
        except (KeyError, ValueError):
            return {
                "status": "REJECT",
                "reason": "UNKNOWN_CHECK_TEMPLATE",
                "findings": [],
                "engine": "Semgrep CE",
                "version": None,
                "rules_hash": None,
                "source_hash": None,
                "profile": PROFILE,
                "cleanup": "NOT_STARTED",
            }
        return self.scan_source(
            source, source_hash, handle=handle, identity=identity, lease_deadline=lease_deadline
        )

    def scan_source(
        self,
        source: str,
        source_hash: str,
        *,
        handle: str | None = None,
        identity: dict | None = None,
        lease_deadline: float | None = None,
    ) -> dict:
        """Return a finite, evidence-bearing decision, failing closed on any uncertainty."""
        if not isinstance(source, str) or len(source) > MAX_SOURCE:
            return {
                "status": "REJECT",
                "reason": "SOURCE_SIZE_OUTSIDE_PROFILE",
                "findings": [],
                "engine": "Semgrep CE",
                "version": None,
                "rules_hash": None,
                "source_hash": source_hash,
                "profile": PROFILE,
                "cleanup": "NOT_STARTED",
            }
        try:
            encoded = source.encode("utf-8")
            rules_hash = hashlib.sha256(self.rules_path.read_bytes()).hexdigest()
        except (UnicodeError, OSError):
            return {
                "status": "ERROR",
                "reason": "INVALID_SOURCE_OR_RULES",
                "findings": [],
                "engine": "Semgrep CE",
                "version": None,
                "rules_hash": None,
                "source_hash": source_hash,
                "profile": PROFILE,
                "cleanup": "NOT_STARTED",
            }
        result = {
            "status": "ERROR",
            "reason": "",
            "findings": [],
            "engine": "Semgrep CE",
            "version": None,
            "rules_hash": rules_hash,
            "source_hash": source_hash,
            "profile": PROFILE,
            "cleanup": "NOT_STARTED",
            "image": self.image,
        }
        actual_hash = hashlib.sha256(encoded).hexdigest()
        if actual_hash != source_hash:
            return result | {"status": "REJECT", "reason": "SOURCE_HASH_MISMATCH"}
        if not encoded or len(encoded) > MAX_SOURCE:
            return result | {"status": "REJECT", "reason": "SOURCE_SIZE_OUTSIDE_PROFILE"}
        if not self.image or not re.fullmatch(r"(?:sha256:|[^\s]+@sha256:)[0-9a-f]{64}", self.image):
            return result | {"status": "UNAVAILABLE", "reason": "PINNED_SCANNER_IMAGE_REQUIRED"}
        image_info = self._run(["image", "inspect", self.image])
        if image_info.returncode != 0 or image_info.timed_out or image_info.output_limit:
            return result | {"status": "UNAVAILABLE", "reason": "SCANNER_IMAGE_UNAVAILABLE"}
        try:
            metadata = json.loads(image_info.stdout)[0]
            labels = metadata["Config"]["Labels"]
            if labels.get("forgeflow.scanner.version") != ENGINE_VERSION:
                raise ValueError("version")
            if labels.get("forgeflow.scanner.rules_hash") != rules_hash:
                raise ValueError("rules")
        except (KeyError, TypeError, ValueError, IndexError):
            return result | {"status": "ERROR", "reason": "SCANNER_IMAGE_METADATA_MISMATCH"}
        # Standalone diagnostic calls also have a full identity, but are not a
        # durable workflow claim. Production callers pass authenticated identity
        # and persist this deterministic handle before calling scan.
        if identity is None:
            if handle is not None:
                return result | {"status": "REJECT", "reason": "SCANNER_IDENTITY_REQUIRED"}
            identity = {
                "scope": {"tenant_id": "diagnostic", "workspace_id": "local", "project_id": "scanner"},
                "run_id": uuid.uuid4().hex,
                "step_id": "scan",
                "attempt": 1,
            }
        try:
            labels = self._identity_labels(identity)
            if hasattr(identity["scope"], "model_dump"):
                identity = identity | {"scope": identity["scope"].model_dump(mode="json")}
            expected_handle = self.handle(
                identity["scope"], identity["run_id"], identity["step_id"], identity["attempt"]
            )
        except (ValueError, TypeError, KeyError):
            return result | {"status": "REJECT", "reason": "SCANNER_IDENTITY_INVALID"}
        if handle is not None and handle != expected_handle:
            return result | {"status": "REJECT", "reason": "SCANNER_HANDLE_IDENTITY_MISMATCH"}
        handle = expected_handle
        result["handle"] = handle
        result["identity"] = identity
        if lease_deadline is not None and time.time() >= lease_deadline:
            return result | {"status": "REJECT", "reason": "SCANNER_LEASE_EXPIRED"}
        if self.on_handle is not None:
            try:
                self.on_handle(handle)
            except Exception:  # noqa: BLE001 - every persistence failure must prevent container creation
                return result | {"status": "ERROR", "reason": "SCANNER_HANDLE_NOT_PERSISTED"}
        if lease_deadline is not None and time.time() >= lease_deadline:
            return result | {"status": "REJECT", "reason": "SCANNER_LEASE_EXPIRED"}
        try:
            lifecycle_fd = self._lock(handle)
        except (OSError, ValueError, TimeoutError):
            return result | {"status": "ERROR", "reason": "SCANNER_LIFECYCLE_LOCK_FAILED"}
        try:
            if self._stop(handle, lease_deadline):
                return result | {"status": "REJECT", "reason": "SCANNER_REVOKED_OR_LEASE_EXPIRED"}
            return self._execute(source, result, handle, labels, lease_deadline)
        finally:
            self._unlock(lifecycle_fd)

    def _execute(
        self, source: str, result: dict, handle: str, labels: dict[str, str], lease_deadline: float | None
    ) -> dict:
        create = self._run(
            [
                "create",
                "--pull",
                "never",
                "-i",
                "--name",
                handle,
                *[item for key, value in labels.items() for item in ("--label", key + "=" + value)],
                "--read-only",
                "--user",
                "10001:10001",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--pids-limit",
                "64",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--log-driver",
                "none",
                "--init",
                "--tmpfs",
                "/work:rw,nosuid,nodev,noexec,size=16777216,uid=10001,gid=10001,mode=0700",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=67108864,uid=10001,gid=10001,mode=0700",
                "--env",
                "HOME=/tmp",
                "--env",
                "SEMGREP_SEND_METRICS=off",
                "--env",
                "SEMGREP_ENABLE_VERSION_CHECK=0",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--workdir",
                "/work",
                self.image,
                "python",
                "/opt/forgeflow_scan_runner.py",
            ]
        )
        # A create timeout can still create the named container; exact cleanup is attempted.
        if create.returncode != 0 or create.timed_out or create.output_limit:
            removed = self._remove(handle, labels)
            return result | {
                "status": "ERROR",
                "reason": "SCANNER_CREATE_FAILED",
                "cleanup": "REMOVED" if removed else "FAILED",
            }
        try:
            inspection = self._run(["inspect", handle])
            configuration = json.loads(inspection.stdout)[0]
            if (
                inspection.returncode != 0
                or not self._safe_configuration(configuration)
                or any(configuration["Config"]["Labels"].get(key) != value for key, value in labels.items())
            ):
                result.update(status="ERROR", reason="SCANNER_ISOLATION_MISMATCH")
            elif self._stop(handle, lease_deadline):
                result.update(status="REJECT", reason="SCANNER_REVOKED_OR_LEASE_EXPIRED")
            else:
                result["inspect"] = self._inspection_evidence(configuration)
                payload = json.dumps(
                    {
                        "source": source,
                        "source_hash": result["source_hash"],
                        "rules_hash": result["rules_hash"],
                    },
                    ensure_ascii=False,
                ).encode()
                scan = self._run(
                    ["start", "-ai", handle],
                    timeout=self.timeout_seconds,
                    data=payload,
                    cancel_check=lambda: self._stop(handle, lease_deadline),
                )
                result["stderr"] = scan.stderr.decode("utf-8", errors="replace")[:4096]
                if scan.cancelled:
                    result.update(status="REJECT", reason="SCANNER_REVOKED_OR_LEASE_EXPIRED")
                elif scan.timed_out:
                    result.update(status="TIMED_OUT", reason="SCANNER_WALL_TIMEOUT")
                elif scan.output_limit:
                    result.update(status="ERROR", reason="SCANNER_OUTPUT_LIMIT")
                elif scan.returncode != 0:
                    result.update(status="ERROR", reason="SCANNER_RUNNER_FAILED")
                else:
                    self._parse_output(scan.stdout, result)
        except (ValueError, TypeError, KeyError, IndexError, OSError):
            result.update(status="ERROR", reason="SCANNER_INVALID_RESPONSE")
        finally:
            removed = self._remove(handle, labels)
            result["cleanup"] = "REMOVED" if removed else "FAILED"
            if not removed:
                result.update(status="ERROR", reason="SCANNER_CLEANUP_FAILED")
        return result

    def cleanup(self, handle: str, *, identity: dict | None = None) -> dict:
        """Control-plane recovery for a previously persisted, scoped handle.

        Caller must authorize access to the step; this method has no user/tool API.
        It checks task ownership and never performs a global container sweep.
        """
        try:
            if identity is None:
                raise ValueError("missing identity")
            labels = self._identity_labels(identity)
            expected_handle = self.handle(
                identity["scope"], identity["run_id"], identity["step_id"], identity["attempt"]
            )
            if handle != expected_handle:
                raise ValueError("mismatch")
        except (ValueError, TypeError, KeyError):
            return {"cleanup": "FAILED", "reason": "INVALID_SCANNER_HANDLE"}
        try:
            self._revoke(handle)  # Durable tombstone first: prevents any late create by the old attempt.
            fd = self._lock(handle, timeout=self.timeout_seconds + 15)
        except (OSError, ValueError, TimeoutError):
            return {"handle": handle, "cleanup": "FAILED", "reason": "SCANNER_RECOVERY_LOCK_FAILED"}
        try:
            removed = self._remove(handle, labels)
        finally:
            self._unlock(fd)
        return {
            "handle": handle,
            "cleanup": "REMOVED" if removed else "FAILED",
            "reason": "" if removed else "SCANNER_RECOVERY_FAILED",
        }

    def recover(self, handle: str, *, identity: dict | None = None) -> dict:
        return self.cleanup(handle, identity=identity)

    def _remove(self, handle: str, expected_labels: dict[str, str]) -> bool:
        # Check ownership before deletion even when create returned an error.
        existing = self._run(["inspect", handle])
        if existing.returncode != 0:
            return self._confirmed_absent(existing, handle)
        try:
            labels = json.loads(existing.stdout)[0]["Config"]["Labels"]
            if any(labels.get(key) != value for key, value in expected_labels.items()):
                return False
        except (ValueError, TypeError, KeyError, IndexError):
            return False
        cleanup = self._run(["rm", "-f", handle])
        if cleanup.returncode == 0 and not cleanup.timed_out:
            return True
        # Absence after a failed create also counts as clean; never list/prune unrelated containers.
        check = self._run(["inspect", handle])
        return self._confirmed_absent(check, handle)

    @staticmethod
    def _confirmed_absent(result: ProcessResult, handle: str) -> bool:
        return (
            result.returncode not in (0, None)
            and not result.timed_out
            and not result.output_limit
            and not result.unavailable
            and not result.cancelled
            and bool(
                re.search(
                    r"\bno such (?:object|container):?\s*" + re.escape(handle.casefold()) + r"(?=\s|$)",
                    result.stderr.decode("utf-8", errors="replace").casefold(),
                )
            )
        )

    @staticmethod
    def _safe_configuration(value: dict) -> bool:
        host = value["HostConfig"]
        config = value["Config"]
        tmpfs = host.get("Tmpfs", {})
        security = host.get("SecurityOpt", [])
        return bool(
            config["User"] == "10001:10001"
            and host["ReadonlyRootfs"]
            and host["NetworkMode"] == "none"
            and not host["Privileged"]
            and not host.get("Binds")
            and not value.get("Mounts")
            and host["LogConfig"]["Type"] == "none"
            and "ALL" in host["CapDrop"]
            and not host.get("CapAdd")
            and any(item.startswith("no-new-privileges") for item in security)
            and not any("unconfined" in item for item in security)
            and host["Memory"] == 512 * 1024 * 1024
            and host["MemorySwap"] == host["Memory"]
            and host["PidsLimit"] == 64
            and host["NanoCpus"] == 1_000_000_000
            and host.get("Init")
            and set(tmpfs) == {"/work", "/tmp"}
            and not host.get("PortBindings")
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and not host.get("VolumesFrom")
            and host.get("PidMode", "") in {"", "private"}
            and host.get("IpcMode", "private") in {"", "private"}
            and all(
                "noexec" in item
                and "nosuid" in item
                and "nodev" in item
                and "uid=10001" in item
                and "gid=10001" in item
                for item in tmpfs.values()
            )
            and "size=16777216" in tmpfs["/work"]
            and "size=67108864" in tmpfs["/tmp"]
        )

    @staticmethod
    def _inspection_evidence(value: dict) -> dict:
        host = value["HostConfig"]
        return {
            "user": value["Config"]["User"],
            "readonly_rootfs": host["ReadonlyRootfs"],
            "network": host["NetworkMode"],
            "memory": host["Memory"],
            "memory_swap": host["MemorySwap"],
            "pids_limit": host["PidsLimit"],
            "nano_cpus": host["NanoCpus"],
            "log_driver": host["LogConfig"]["Type"],
            "cap_drop": host["CapDrop"],
            "security_opt": host["SecurityOpt"],
            "tmpfs": host["Tmpfs"],
            "mount_count": len(value.get("Mounts", [])),
        }

    @staticmethod
    def _parse_output(raw: bytes, result: dict) -> None:
        envelope = json.loads(raw)
        if (
            envelope.get("source_hash") != result["source_hash"]
            or envelope.get("rules_hash") != result["rules_hash"]
            or envelope.get("version") != ENGINE_VERSION
        ):
            raise ValueError("provenance mismatch")
        result["version"] = envelope["version"]
        status = envelope.get("status")
        if status not in {"PASS", "REJECT", "ERROR", "TIMED_OUT"}:
            raise ValueError("invalid scanner status")
        findings = envelope.get("findings")
        if not isinstance(findings, list) or len(findings) > 128:
            raise ValueError("invalid findings")
        if status == "PASS" and (findings or envelope.get("errors")):
            raise ValueError("contradictory pass")
        if status in {"PASS", "REJECT"} and envelope.get("engine_executed") is not True:
            raise ValueError("engine did not execute")
        result.update(
            status=status,
            findings=findings,
            reason=envelope.get("reason", ""),
            engine_executed=envelope.get("engine_executed", False),
            errors=envelope.get("errors", []),
            raw_output_hash=hashlib.sha256(raw).hexdigest(),
        )
