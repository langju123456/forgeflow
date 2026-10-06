"""Actual official SDK stdio transport, one trusted scope/role/run per server process.

The local bootstrap file is a short-lived DEV_ONLY internal capability, not SSO.
It never enters a model context or a Docker container. The client records serialized
JSON-RPC messages at the transport boundary, rather than inventing protocol events.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .contracts import DomainError, ExecutionResult, ExecutionSpec, Principal, canonical
from .policy import POLICY, execution_policy
from .sandbox import DockerSandbox

EXECUTOR_ROLES = frozenset(
    role for role, grants in POLICY["role_tools"].items() if "sandbox.execute" in grants
)
TOOL_NAME = "sandbox.execute"
TOOL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "template_id": {"type": "string", "enum": ["evt-check", "timeout", "network-probe", "output-flood"]},
        "input_manifest_id": {"type": "string"},
        "resource_profile": {"type": "string", "enum": ["checker-v1"]},
    },
    "required": ["template_id", "input_manifest_id", "resource_profile"],
}


class _RecordedStream:
    def __init__(self, stream, transcript: list[dict], direction: str):
        self.stream, self.transcript, self.direction = stream, transcript, direction

    def _record(self, message):
        if isinstance(message, Exception):
            self.transcript.append({"direction": self.direction, "transport_error": type(message).__name__})
            return
        value = message.message.model_dump(mode="json", by_alias=True, exclude_none=True)
        raw = canonical(value)
        entry = {
            "direction": self.direction,
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "bytes": len(raw.encode()),
        }
        if len(raw.encode()) <= 64 * 1024:
            entry["message"] = value
        else:
            entry["message_summary"] = {
                "jsonrpc": value.get("jsonrpc"),
                "id": value.get("id"),
                "method": value.get("method"),
                "large_payload_redacted": True,
            }
        if len(self.transcript) < 100:
            self.transcript.append(entry)

    async def send(self, message):
        self._record(message)
        await self.stream.send(message)

    async def receive(self):
        message = await self.stream.receive()
        self._record(message)
        return message

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except Exception as exc:
            import anyio

            if isinstance(exc, anyio.EndOfStream):
                raise StopAsyncIteration from exc
            raise

    async def __aenter__(self):
        await self.stream.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.stream.__aexit__(*args)

    async def aclose(self):
        return await self.stream.aclose()


class MCPBridge:
    def __init__(self, provider: DockerSandbox | None = None):
        self.provider = provider or DockerSandbox()

    async def call(
        self,
        principal: Principal,
        spec: ExecutionSpec,
        name: str,
        arguments: dict,
        *,
        scan_receipt: dict | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        lease_deadline: float | datetime | None = None,
    ) -> tuple[dict, dict]:
        transcript: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="forgeflow-mcp-") as folder:
            directory, binding = Path(folder), Path(folder) / "binding.json"
            cancel_file = directory / "cancel"
            deadline = lease_deadline.timestamp() if isinstance(lease_deadline, datetime) else lease_deadline
            binding.write_text(
                canonical(
                    {
                        "principal": principal.model_dump(mode="json"),
                        "spec": spec.model_dump(mode="json"),
                        "expires_at": time.time() + spec.timeout_seconds + 90,
                        "cancel_file": str(cancel_file),
                        "scan_receipt": scan_receipt,
                        "lease_deadline": deadline,
                        "image": self.provider.image,
                        "docker": self.provider.docker,
                        "scratch_root": str(self.provider.scratch_root),
                    }
                )
            )
            binding.chmod(0o600)
            # SDK defaults whitelist a small parent environment; no credential inheritance.
            source_root = str(Path(__file__).resolve().parents[1])
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "forgeflow.mcp_bridge", "--serve", str(binding)],
                env={"PYTHONPATH": source_root},
            )
            # A task-owned bounded stderr file keeps server logs out of JSON-RPC stdout.
            with (directory / "server-stderr.log").open("w+") as errlog:
                async with stdio_client(params, errlog=errlog) as (reader, writer):  # noqa: SIM117
                    async with ClientSession(
                        _RecordedStream(reader, transcript, "server_to_client"),
                        _RecordedStream(writer, transcript, "client_to_server"),
                        read_timeout_seconds=timedelta(seconds=spec.timeout_seconds + 45),
                    ) as session:
                        negotiated = await session.initialize()
                        listed = await session.list_tools()
                        task = asyncio.create_task(session.call_tool(name, arguments))
                        try:
                            while not task.done():
                                if cancel_requested and cancel_requested() and not cancel_file.exists():
                                    cancel_file.write_text("cancel")
                                    cancel_file.chmod(0o600)
                                await asyncio.sleep(0.05)
                            result = await task
                        finally:
                            if not task.done():
                                cancel_file.write_text("cancel")
                                cancel_file.chmod(0o600)
                                task.cancel()
                errlog.flush()
                errlog.seek(0)
                server_error = errlog.read(16 * 1024)
            receipt = {
                "transport": "stdio",
                "independent_server_process": True,
                "sdk": "mcp",
                "sdk_version": importlib.metadata.version("mcp"),
                "protocol_version": negotiated.protocolVersion,
                "server_info": negotiated.serverInfo.model_dump(mode="json"),
                "tools": [tool.name for tool in listed.tools],
                "binding": {
                    "scope": spec.scope.model_dump(),
                    "run_id": spec.run_id,
                    "subject_id": principal.subject_id,
                    "roles": sorted(principal.roles),
                },
                "messages": transcript,
                "server_stderr": server_error,
            }
            data = result.structuredContent
            if data is None:
                content = next(
                    (item.text for item in result.content if isinstance(item, types.TextContent)), "{}"
                )
                data = json.loads(content)
            return data, receipt

    async def execute(
        self,
        principal: Principal,
        spec: ExecutionSpec,
        *,
        scan_receipt: dict | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        lease_deadline: float | datetime | None = None,
    ) -> tuple[ExecutionResult, dict]:
        data, transcript = await self.call(
            principal,
            spec,
            TOOL_NAME,
            {
                "template_id": spec.template_id,
                "input_manifest_id": spec.input_hash,
                "resource_profile": spec.profile,
            },
            scan_receipt=scan_receipt,
            cancel_requested=cancel_requested,
            lease_deadline=lease_deadline,
        )
        if "error" in data:
            raise DomainError(data["error"], data.get("reason", "MCP call rejected"), 403)
        return ExecutionResult.model_validate(data), transcript


def _load_binding(path: str) -> dict:
    target = Path(path)
    if target.is_symlink():
        raise ValueError("Bootstrap symlink rejected")
    info = target.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 256 * 1024:
        raise ValueError("Bootstrap permissions/size rejected")
    data = json.loads(target.read_text())
    principal = Principal.model_validate(data["principal"])
    spec = ExecutionSpec.model_validate(data["spec"])
    cancel_file = Path(data["cancel_file"])
    if cancel_file.parent != target.parent or cancel_file.name != "cancel":
        raise ValueError("Cancellation binding rejected")
    data.update(principal=principal, spec=spec)
    return data


async def _serve(path: str) -> None:
    binding = _load_binding(path)
    principal, spec = binding["principal"], binding["spec"]
    provider = DockerSandbox(
        image=binding["image"], docker=binding["docker"], scratch_root=binding["scratch_root"]
    )
    server = Server("forgeflow-local-sandbox", version="0.1.0")

    def permitted() -> bool:
        return (
            principal.permits(spec.scope, set(EXECUTOR_ROLES))
            and principal.kind in {"agent", "service"}
            and time.time() < binding["expires_at"]
        )

    @server.list_tools()
    async def list_tools():
        return (
            [
                types.Tool(
                    name=TOOL_NAME,
                    description="Execute the session-bound immutable synthetic checker",
                    inputSchema=TOOL_SCHEMA,
                )
            ]
            if permitted()
            else []
        )

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict):
        def denied(reason: str):
            value = {"error": "DENIED", "reason": reason}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=canonical(value))],
                structuredContent=value,
                isError=True,
            )

        # list visibility is not authority: direct calls repeat every guard.
        if not permitted() or name != TOOL_NAME:
            return denied("Role, scope, expiry or tool capability rejected")
        expected = {
            "template_id": spec.template_id,
            "input_manifest_id": spec.input_hash,
            "resource_profile": spec.profile,
        }
        if arguments != expected:
            return denied("Structured arguments differ from immutable session binding")
        scan = binding.get("scan_receipt") or {}
        if scan.get("status") != "PASS" or scan.get("source_hash") != spec.code_hash:
            return denied("Successful same-source scan receipt required; scanning is auxiliary")
        try:
            execution_policy(principal, spec, scan["source_hash"], scan["status"])
        except DomainError:
            return denied("Trusted structured execution policy rejected the bound request")
        if (
            not isinstance(binding.get("lease_deadline"), (int, float))
            or time.time() >= binding["lease_deadline"]
        ):
            return denied("Fresh trusted lease deadline required")
        result = await asyncio.to_thread(
            provider.execute, spec, lambda: Path(binding["cancel_file"]).exists(), binding["lease_deadline"]
        )
        value = result.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=canonical(value))],
            structuredContent=value,
            isError=result.status != "SUCCEEDED",
        )

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", required=True)
    args = parser.parse_args()
    asyncio.run(_serve(args.serve))


if __name__ == "__main__":
    main()
