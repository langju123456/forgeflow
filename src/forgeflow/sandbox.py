"""Hardened local Docker provider. No host execution fallback or daemon-wide mutation."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import selectors
import stat
import subprocess
import tarfile
import time
from collections.abc import Callable
from pathlib import Path

from .contracts import (
    ExecutionResult,
    ExecutionSpec,
    SandboxArtifact,
    canonical,
    digest,
)
from .templates import RUNNER, template_hash, template_source

NAMESPACE = "forgeflow-local-v1"
OUTPUT_CAP = 256 * 1024
ARTIFACT_CAP = 2 * 1024 * 1024
HANDLE_PATTERN = re.compile(r"^ff-[0-9a-f]{32}$")


class SandboxFailure(Exception):
    pass


def parse_artifact_tar(payload: bytes, expected: str = "result.json") -> SandboxArtifact:
    """Never extract archives: accept exactly one bounded, flat regular file."""
    if len(payload) > ARTIFACT_CAP + 65536:
        raise SandboxFailure("Artifact archive exceeds limit")
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            entries = archive.getmembers()
            if len(entries) != 1:
                raise SandboxFailure("Artifact must contain exactly one file")
            member = entries[0]
            if member.name != expected or not member.isreg() or member.size > ARTIFACT_CAP:
                raise SandboxFailure("Artifact path/type/size rejected")
            if member.linkname or member.name.startswith("/") or ".." in Path(member.name).parts:
                raise SandboxFailure("Artifact link/path rejected")
            stream = archive.extractfile(member)
            if stream is None:
                raise SandboxFailure("Artifact has no data")
            data = stream.read(ARTIFACT_CAP + 1)
            if len(data) != member.size or len(data) > ARTIFACT_CAP:
                raise SandboxFailure("Artifact data size rejected")
            content = data.decode("utf-8")
            json.loads(content)  # Only the fixed JSON artifact is accepted.
            return SandboxArtifact(
                path=expected, sha256=hashlib.sha256(data).hexdigest(), size=len(data), content=content
            )
    except (tarfile.TarError, UnicodeError, ValueError) as exc:
        raise SandboxFailure("Invalid artifact archive or JSON") from exc


class DockerSandbox:
    def __init__(
        self, image: str = "python:3.12-slim", docker: str = "docker", scratch_root: str | None = None
    ):
        self.image, self.docker = image, docker
        self.scratch_root = Path(scratch_root or f"/tmp/forgeflow-sandbox-{os.getuid()}")

    def _root(self) -> Path:
        try:
            self.scratch_root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = self.scratch_root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise SandboxFailure("Private scratch root ownership/mode rejected")
        return self.scratch_root

    def _scratch(self, handle: str) -> Path:
        if not HANDLE_PATTERN.fullmatch(handle):
            raise SandboxFailure("Invalid scratch handle")
        return self._root() / handle

    def _revoked(self, handle: str) -> bool:
        marker = self._root() / (handle + ".revoked")
        return marker.exists() or marker.is_symlink()

    def _revoke(self, handle: str) -> None:
        target = self._root() / (handle + ".revoked")
        fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise SandboxFailure("Revocation ownership rejected")
            os.write(fd, b"revoked\n")
            os.fsync(fd)
        finally:
            os.close(fd)

    def _lock(self, handle: str, timeout: float = 10) -> int:
        if not HANDLE_PATTERN.fullmatch(handle):
            raise SandboxFailure("Invalid resource handle")
        fd = os.open(self._root() / (handle + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            os.close(fd)
            raise SandboxFailure("Lifecycle lock ownership rejected")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise SandboxFailure("Lifecycle lock timed out") from None
                time.sleep(0.025)

    @staticmethod
    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _prepare_scratch(self, handle: str, spec: ExecutionSpec) -> tuple[Path, int]:
        directory = self._scratch(handle)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            raise SandboxFailure("Attempt scratch already exists; recovery required") from None
        manifest = directory / "ownership.json"
        manifest.write_text(
            canonical(
                {
                    "namespace": NAMESPACE,
                    "handle": handle,
                    "scope": spec.scope.model_dump(),
                    "run": spec.run_id,
                    "step": spec.step_id,
                    "attempt": spec.attempt,
                }
            )
        )
        manifest.chmod(0o600)
        inputs = directory / "inputs"
        inputs.mkdir(mode=0o700)
        for name, content in (
            ("runner.py", RUNNER),
            ("check.py", template_source(spec.template_id)),
            ("input.json", canonical(spec.input_manifest)),
        ):
            target = inputs / name
            target.write_text(content)
            target.chmod(0o444)
        inputs.chmod(0o555)
        output_fd = os.open(
            directory / "result.json", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            os.fchmod(output_fd, 0o666)  # The isolated UID can write this exact file; parent stays 0700.
        except OSError:
            os.close(output_fd)
            raise
        return directory, output_fd

    def _purge_scratch(self, handle: str) -> bool:
        try:
            directory = self._scratch(handle)
            if not directory.exists() and not directory.is_symlink():
                return True
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                return False
            manifest = directory / "ownership.json"
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 4096:
                return False
            owner = json.loads(manifest.read_text())
            if owner.get("namespace") != NAMESPACE or owner.get("handle") != handle:
                return False
            if {p.name for p in directory.iterdir()} - {"ownership.json", "inputs", "result.json"}:
                return False
            inputs = directory / "inputs"
            if inputs.exists():
                if inputs.is_symlink() or not inputs.is_dir():
                    return False
                if {p.name for p in inputs.iterdir()} - {"runner.py", "check.py", "input.json"}:
                    return False
                for target in inputs.iterdir():
                    if target.is_symlink() or not target.is_file():
                        return False
                inputs.chmod(0o700)
                for target in inputs.iterdir():
                    target.unlink()
                inputs.rmdir()
            output = directory / "result.json"
            if output.exists() or output.is_symlink():
                if output.is_symlink() or not stat.S_ISREG(output.lstat().st_mode):
                    return False
                output.unlink()
            manifest.unlink()
            directory.rmdir()
            return not directory.exists()
        except (OSError, ValueError, SandboxFailure):
            return False

    @staticmethod
    def _read_output(directory: Path, original_fd: int) -> SandboxArtifact:
        original = os.fstat(original_fd)
        fd = os.open(directory / "result.json", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            current = os.fstat(fd)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
                or current.st_size > ARTIFACT_CAP
            ):
                raise SandboxFailure("Exact artifact inode/type/size rejected")
            data = bytearray()
            while len(data) <= ARTIFACT_CAP:
                chunk = os.read(fd, min(65536, ARTIFACT_CAP + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) != current.st_size or len(data) > ARTIFACT_CAP:
                raise SandboxFailure("Artifact bounded read rejected")
            content = bytes(data).decode("utf-8")
            json.loads(content)
            return SandboxArtifact(
                path="result.json", sha256=hashlib.sha256(data).hexdigest(), size=len(data), content=content
            )
        finally:
            os.close(fd)

    def _command(self, args: list[str], timeout: float = 10, max_bytes: int = 128 * 1024) -> bytes:
        """Bounded streaming for every Docker CLI command, including artifact export."""
        proc = subprocess.Popen([self.docker, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        selector = selectors.DefaultSelector()
        output, error = bytearray(), bytearray()
        assert proc.stdout and proc.stderr
        selector.register(proc.stdout, selectors.EVENT_READ, output)
        selector.register(proc.stderr, selectors.EVENT_READ, error)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise SandboxFailure("Docker operation timed out")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buf = key.data
                        if len(buf) + len(chunk) > max_bytes:
                            raise SandboxFailure("Docker output exceeds limit")
                        buf.extend(chunk)
            try:
                code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise SandboxFailure("Docker operation timed out") from exc
            if code:
                raise SandboxFailure(error.decode(errors="replace")[:2000] or "Docker operation failed")
            return bytes(output)
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)
            proc.stdout.close()
            proc.stderr.close()

    def available(self) -> bool:
        try:
            self._command(["info", "--format", "{{.ServerVersion}}"], timeout=3)
            return True
        except (OSError, SandboxFailure):
            return False

    def resolve_image(self) -> str:
        """Offline inspect only: never implicitly pull an image."""
        data = json.loads(self._command(["image", "inspect", self.image]))[0]
        # The content-addressed local image ID is immutable and usable by docker create.
        image_id = data["Id"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise SandboxFailure("Image digest unavailable")
        return image_id

    @staticmethod
    def template_hash(template_id: str) -> str:
        return template_hash(template_id)

    @staticmethod
    def handle(spec: ExecutionSpec) -> str:
        identity = {
            "scope": spec.scope.model_dump(),
            "run": spec.run_id,
            "step": spec.step_id,
            "attempt": spec.attempt,
        }
        return "ff-" + digest(identity)[:32]

    def _inspect(self, handle: str) -> dict:
        return json.loads(self._command(["inspect", handle]))[0]

    def cleanup(self, handle: str) -> bool:
        """Exact ownership checked before removal. Unknown resources are never removed."""
        if not HANDLE_PATTERN.fullmatch(handle):
            return False
        fd = None
        try:
            # Revoke before waiting: the executing owner observes this and stops itself.
            self._revoke(handle)
            fd = self._lock(handle)
            return self._cleanup_owned(handle)
        except (OSError, SandboxFailure):
            return False
        finally:
            if fd is not None:
                self._unlock(fd)

    def _cleanup_owned(self, handle: str) -> bool:
        try:
            info = self._inspect(handle)
        except SandboxFailure as exc:
            return self._confirmed_absent(str(exc), handle) and self._purge_scratch(handle)
        except (OSError, ValueError, KeyError):
            return False
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("forgeflow.namespace") != NAMESPACE or labels.get("forgeflow.handle") != handle:
            return False
        try:
            if info["State"].get("Paused"):
                self._command(["unpause", handle])
            self._command(["rm", "--force", handle])
            try:
                self._inspect(handle)
                return False
            except SandboxFailure as exc:
                return self._confirmed_absent(str(exc), handle) and self._purge_scratch(handle)
        except (OSError, SandboxFailure):
            return False

    @staticmethod
    def _confirmed_absent(reason: str, handle: str) -> bool:
        return bool(
            re.search(
                r"\bno such (?:object|container):?\s*" + re.escape(handle.casefold()) + r"(?=\s|$)", reason.casefold()
            )
        )

    @staticmethod
    def _inspection(info: dict) -> dict:
        hc, config = info["HostConfig"], info["Config"]
        return {
            "image": info["Image"],
            "user": config["User"],
            "readonly_rootfs": hc["ReadonlyRootfs"],
            "network_mode": hc["NetworkMode"],
            "nano_cpus": hc["NanoCpus"],
            "memory": hc["Memory"],
            "memory_swap": hc["MemorySwap"],
            "pids_limit": hc["PidsLimit"],
            "cap_drop": hc["CapDrop"],
            "security_opt": hc["SecurityOpt"],
            "privileged": hc["Privileged"],
            "log_config": hc["LogConfig"],
            "tmpfs": hc["Tmpfs"],
            "mounts": [
                {"destination": m["Destination"], "rw": m["RW"], "type": m["Type"]} for m in info["Mounts"]
            ],
            "port_bindings": hc.get("PortBindings"),
            "state": info["State"],
        }

    @staticmethod
    def _verify_inspect(info: dict, image: str) -> None:
        hc, config = info["HostConfig"], info["Config"]
        checks = (
            info["Image"] == image,
            config["User"] == "10001:10001",
            hc["ReadonlyRootfs"],
            hc["NetworkMode"] == "none",
            hc["NanoCpus"] == 500_000_000,
            hc["Memory"] == 256 * 1024 * 1024,
            hc["MemorySwap"] == 256 * 1024 * 1024,
            hc["PidsLimit"] == 32,
            hc["CapDrop"] == ["ALL"],
            "no-new-privileges" in (hc["SecurityOpt"] or []),
            not hc["Privileged"],
            hc["LogConfig"]["Type"] == "none",
            not hc.get("PortBindings"),
            len(info["Mounts"]) == 2,
            {(m["Destination"], m["RW"]) for m in info["Mounts"]}
            == {("/inputs", False), ("/outputs/result.json", True)},
            set(hc["Tmpfs"]) == {"/workspace", "/tmp"},
        )
        if not all(checks):
            raise SandboxFailure("Docker configuration does not match checker-v1")

    def execute(
        self,
        spec: ExecutionSpec,
        cancel_requested: Callable[[], bool] | None = None,
        lease_deadline: float | None = None,
    ) -> ExecutionResult:
        handle, stdout, stderr = self.handle(spec), bytearray(), bytearray()
        started, created = time.monotonic(), False
        status, reason, exit_code, inspection, artifacts = "FAILED", "", None, {}, ()
        attach, output_fd, scratch, lifecycle_fd = None, None, None, None

        def cancelled() -> bool:
            return (
                self._revoked(handle)
                or (lease_deadline is not None and time.time() >= lease_deadline)
                or bool(cancel_requested and cancel_requested())
            )

        try:
            if spec.code_hash != template_hash(spec.template_id) or spec.input_hash != digest(
                spec.input_manifest
            ):
                raise SandboxFailure("Immutable code/input hash mismatch")
            if spec.image != self.resolve_image():
                raise SandboxFailure("Execution image does not match pinned local digest")
            lifecycle_fd = self._lock(handle)
            if cancelled():
                return ExecutionResult(
                    handle=handle,
                    status="CANCELLED",
                    cleanup="NOT_STARTED",
                    reason="Cancelled, revoked or lease expired before container creation",
                )
            scratch = self._scratch(handle)  # Persisted deterministic identity survives partial preparation.
            scratch, output_fd = self._prepare_scratch(handle, spec)
            if scratch:
                args = [
                    "create",
                    "--pull",
                    "never",
                    "--name",
                    handle,
                    "--label",
                    f"forgeflow.namespace={NAMESPACE}",
                    "--label",
                    f"forgeflow.handle={handle}",
                    "--label",
                    f"forgeflow.run={spec.run_id}",
                    "--label",
                    f"forgeflow.step={spec.step_id}",
                    "--label",
                    f"forgeflow.attempt={spec.attempt}",
                    "--read-only",
                    "--user",
                    "10001:10001",
                    "--network",
                    "none",
                    "--cpus",
                    "0.5",
                    "--memory",
                    "256m",
                    "--memory-swap",
                    "256m",
                    "--pids-limit",
                    "32",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--log-driver",
                    "none",
                    "--tmpfs",
                    "/workspace:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=8m,uid=10001,gid=10001,mode=0700",
                    "--mount",
                    f"type=bind,source={scratch / 'inputs'},target=/inputs,readonly",
                    "--mount",
                    f"type=bind,source={scratch / 'result.json'},target=/outputs/result.json",
                    "--workdir",
                    "/workspace",
                    "--env",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "--env",
                    "PYTHONUNBUFFERED=1",
                    "--env",
                    f"FORGEFLOW_CHECK_TIMEOUT={spec.timeout_seconds}",
                    spec.image,
                    "/usr/local/bin/python",
                    "-I",
                    "/inputs/runner.py",
                ]
                if cancelled():
                    raise SandboxFailure("Cancelled/revoked before container creation")
                self._command(args)
                created = True
                raw = self._inspect(handle)
                self._verify_inspect(raw, spec.image)
                inspection = self._inspection(raw)
                if cancelled():
                    raise SandboxFailure("Cancelled/revoked before container start")
                attach = subprocess.Popen(
                    [self.docker, "start", "--attach", handle], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                assert attach.stdout and attach.stderr
                selector = selectors.DefaultSelector()
                selector.register(attach.stdout, selectors.EVENT_READ, stdout)
                selector.register(attach.stderr, selectors.EVENT_READ, stderr)
                deadline, done = time.monotonic() + spec.timeout_seconds, False
                try:
                    while selector.get_map():
                        if cancelled():
                            status, reason = "CANCELLED", "Cancellation observed; artifacts UNAVAILABLE"
                            break
                        if time.monotonic() >= deadline:
                            status, reason = "TIMED_OUT", "Wall timeout; incomplete artifact UNAVAILABLE"
                            break
                        for key, _ in selector.select(0.05):
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            buf = key.data
                            available = OUTPUT_CAP - len(buf)
                            buf.extend(chunk[:available])
                            if len(chunk) > available:
                                status, reason = (
                                    "OUTPUT_LIMIT",
                                    "Bounded attach output exceeded; artifacts UNAVAILABLE",
                                )
                                break
                        if status == "OUTPUT_LIMIT":
                            break
                        if b"\nFORGEFLOW_TIMEOUT\n" in stdout:
                            status, reason = "TIMED_OUT", "Runner wall timeout; artifact UNAVAILABLE"
                            break
                        match = re.search(rb"\nFORGEFLOW_DONE:(-?\d+)\r?\n", stdout)
                        if match:
                            done, exit_code = True, int(match.group(1))
                            break
                    if done:
                        self._command(["pause", handle])
                        frozen = self._inspect(handle)
                        inspection = self._inspection(frozen)
                        inspection["artifact_profile"] = "checker-v1-fileartifact"
                        inspection["artifact_fsize_hard_limit"] = ARTIFACT_CAP
                        if not frozen["State"]["Paused"] or not frozen["State"]["Running"]:
                            raise SandboxFailure("Container not frozen before artifact export")
                        if exit_code == 0:
                            artifacts = (self._read_output(scratch, output_fd),)
                            status = "SUCCEEDED"
                        else:
                            status, reason = "FAILED", "Checker exited unsuccessfully; artifact not trusted"
                    elif not reason:
                        status, reason = (
                            "FAILED",
                            "Runner stopped before completion; incomplete artifact UNAVAILABLE",
                        )
                finally:
                    selector.close()
                inspection["elapsed_seconds"] = round(time.monotonic() - started, 3)
                inspection.update(
                    spec_hash=digest(spec),
                    input_hash=spec.input_hash,
                    code_hash=spec.code_hash,
                    runner_hash=hashlib.sha256(RUNNER.encode()).hexdigest(),
                )
                # Cleanup while input bind still exists. Exit of the attach process proves no retained reader.
                self._revoke(handle)
                cleaned = self._cleanup_owned(handle)
                created = False if cleaned else created
                if not cleaned:
                    status, reason = "FAILED", "Container cleanup could not be confirmed"
                inspection["cleanup_verified"] = cleaned
                return ExecutionResult(
                    handle=handle,
                    status=status,
                    exit_code=exit_code,
                    stdout=stdout.decode(errors="replace"),
                    stderr=stderr.decode(errors="replace"),
                    cleanup="REMOVED" if cleaned else "FAILED",
                    artifacts=artifacts,
                    inspect=inspection,
                    reason=reason,
                )
        except (SandboxFailure, OSError, ValueError, KeyError) as exc:
            reason = str(exc)[:2000]
            if created or scratch is not None:
                self._revoke(handle)
            cleaned = self._cleanup_owned(handle) if created or scratch is not None else False
            return ExecutionResult(
                handle=handle,
                status="FAILED" if created else "UNAVAILABLE",
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                cleanup="REMOVED" if cleaned else ("FAILED" if created or scratch else "NOT_STARTED"),
                inspect=inspection,
                reason=reason,
            )
        finally:
            if output_fd is not None:
                os.close(output_fd)
            if attach is not None:
                if attach.poll() is None:
                    attach.terminate()
                    try:
                        attach.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        attach.kill()
                        attach.wait(timeout=3)
                if attach.stdout:
                    attach.stdout.close()
                if attach.stderr:
                    attach.stderr.close()
            if lifecycle_fd is not None:
                self._unlock(lifecycle_fd)
