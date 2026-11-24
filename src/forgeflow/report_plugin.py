"""A fixed, read-only MCP plugin with task-owned sessions (adapted from Manus Q3).

Only authorized retrieved rows cross the boundary. No installer, arbitrary argv,
network tool or side effect is exposed. Transport failure is not an abstention.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from . import annual_cash, apple_sales
from .contracts import ContextHit, canonical
from .report_lookup import for_question
from .report_trace import CALL_VERSION, RESULT_VERSION, TOOL_NAME, event, finish, hashed, journal, new_trace

RUNTIME_VERSION = "report-mcp-runtime-v2"
HIT_KEYS = {"id", "version", "content", "source_hash", "classification", "status"}
INPUT_KEYS = {"schema_version", "call_id", "question", "index_version", "hits"}
RESULT_KEYS = {"schema_version", "call_id", "status", "found", "answer", "citations", "tool_version",
               "input_sha256", "output_sha256"}
INPUT_SCHEMA = {"type": "object", "additionalProperties": False, "required": sorted(INPUT_KEYS),
                "properties": {"schema_version": {"const": CALL_VERSION},
                               **{k: {"type": "string", "minLength": 1} for k in
                                  ("call_id", "question", "index_version")},
                               "hits": {"type": "array", "maxItems": 20, "items": {
                                   "type": "object", "additionalProperties": False, "required": sorted(HIT_KEYS),
                                   "properties": {**{k: {"type": "string"} for k in HIT_KEYS - {"classification"}},
                                                  "classification": {"type": "integer"}}}}}}


class ReportProcessError(RuntimeError):
    """No trustworthy acknowledgement: the completion state is unknown."""


class ReportProtocolError(RuntimeError):
    """Known violation of the input/output contract; retrying it is not a remedy."""


def validate_input(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != INPUT_KEYS or value.get("schema_version") != CALL_VERSION:
        raise ReportProtocolError("invalid call schema")
    if any(not isinstance(value[k], str) or not value[k].strip() or len(value[k]) > limit
           for k, limit in (("call_id", 128), ("question", 4000), ("index_version", 128))):
        raise ReportProtocolError("invalid call fields")
    if not isinstance(value["hits"], list) or len(value["hits"]) > 20 or len(canonical(value)) > 512000:
        raise ReportProtocolError("input budget exceeded")
    seen = set()
    for hit in value["hits"]:
        if (not isinstance(hit, dict) or set(hit) != HIT_KEYS or type(hit["classification"]) is not int
                or any(not isinstance(hit[k], str) or not hit[k] for k in HIT_KEYS - {"classification"})):
            raise ReportProtocolError("invalid hit schema")
        if hit["id"] in seen or hit["source_hash"] != hashlib.sha256(hit["content"].encode()).hexdigest():
            raise ReportProtocolError("duplicate or altered source row")
        seen.add(hit["id"])
        try:
            ContextHit(**hit)
        except ValueError as exc:
            raise ReportProtocolError("invalid hit status") from exc
    if for_question(value["question"]) is None:
        raise ReportProtocolError("unsupported typed question")
    return value


def expected_version(call: dict) -> str:
    lookup = for_question(call["question"])
    return apple_sales.VERSION if lookup and lookup.sales_intent else annual_cash.VERSION


def validate_result(value: Any, call: dict) -> dict:
    if not isinstance(value, dict) or not RESULT_KEYS <= value.keys() or set(value) - RESULT_KEYS - {"calculation"}:
        raise ReportProtocolError("invalid result fields")
    if (value["schema_version"] != RESULT_VERSION or value["call_id"] != call["call_id"]
            or value["input_sha256"] != hashed(call)
            or value["output_sha256"] != hashed({k: v for k, v in value.items() if k != "output_sha256"})
            or value["tool_version"] != expected_version(call)
            or value["status"] not in {"ANSWERED", "PERIOD_UNVERIFIED", "UNSUPPORTED"}
            or type(value["found"]) is not bool or not isinstance(value["answer"], str)
            or len(canonical(value)) > 512000):
        raise ReportProtocolError("invalid result binding")
    cites = value["citations"]
    if (not isinstance(cites, list) or any(not isinstance(c, str) for c in cites)
            or len(set(cites)) != len(cites) or not set(cites) <= {h["id"] for h in call["hits"]}):
        raise ReportProtocolError("foreign or duplicate citation")
    if value["status"] == "ANSWERED":
        if not value["found"] or not value["answer"].strip() or not cites:
            raise ReportProtocolError("contradictory answered result")
    elif value["found"] or cites:
        raise ReportProtocolError("contradictory abstention")
    if "calculation" in value and not isinstance(value["calculation"], dict):
        raise ReportProtocolError("invalid calculation")
    return value


def answer(arguments: dict) -> dict:
    call = validate_input(arguments)
    lookup = for_question(call["question"])
    raw = lookup.answer(call["question"], [ContextHit(**h) for h in call["hits"]])
    result = {"schema_version": RESULT_VERSION, "call_id": call["call_id"],
              "status": raw["status"], "found": bool(raw["found"]), "answer": raw["answer"],
              "citations": raw["citations"], "tool_version": raw["model"], "input_sha256": hashed(call)}
    if "calculation" in raw:
        result["calculation"] = raw["calculation"]
    result["output_sha256"] = hashed(result)
    return validate_result(result, call)


def _parameters() -> StdioServerParameters:
    # Owned code only; this private factory is replaceable by test fault servers.
    return StdioServerParameters(command=sys.executable, args=["-m", "forgeflow.report_plugin", "--serve"],
                                 env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])})


def _consume(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()  # a timed-out/cancelled caller must not leak an unobserved owner exception


class ReportRuntime:
    """One owner task owns all SDK contexts; one lock serializes calls and close."""
    def __init__(self):
        self._task = self._queue = self._ready = None
        self._lock = asyncio.Lock()

    async def _owner(self, queue, ready) -> None:
        active = None
        try:
            # Plugin stderr is not a product log or an arbitrary output sink.
            with open(os.devnull, "w") as errors:
                async with stdio_client(_parameters(), errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        info = await session.initialize()
                        listed = await session.list_tools()
                        if [t.name for t in listed.tools] != [TOOL_NAME]:
                            raise ReportProtocolError("unexpected plugin tools")
                        receipt = {"transport": "stdio", "sdk_version": importlib.metadata.version("mcp"),
                                   "protocol_version": info.protocolVersion,
                                   "server_instance": info.serverInfo.version, "tools": [TOOL_NAME]}
                        ready.set_result(receipt)
                        while (item := await queue.get()) is not None:
                            call, active = item
                            response = await session.call_tool(TOOL_NAME, call)
                            if response.isError:
                                raise ReportProtocolError("plugin rejected call")
                            value = response.structuredContent
                            if value is None:
                                try:
                                    value = json.loads(next(x.text for x in response.content
                                                            if isinstance(x, types.TextContent)))
                                except (StopIteration, ValueError) as exc:
                                    raise ReportProtocolError("invalid plugin JSON") from exc
                            if not active.done():
                                active.set_result(value)
                            active = None
        except BaseException as exc:
            # Exception groups from the SDK remain transport failures unless a
            # nested explicit protocol error was the cause.
            def protocol(error):
                return isinstance(error, ReportProtocolError) or any(protocol(e) for e in getattr(error, "exceptions", []))
            failure = ReportProtocolError("plugin protocol failure") if protocol(exc) else ReportProcessError("plugin disconnected")
            if not ready.done():
                ready.set_exception(failure)
            if active is not None and not active.done():
                active.set_exception(failure)
            while not queue.empty():
                item = queue.get_nowait()
                if item is not None and not item[1].done():
                    item[1].set_exception(failure)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def _discard(self) -> None:
        task, queue = self._task, self._queue
        self._task = self._queue = self._ready = None
        if task is None:
            return
        if queue is not None:
            queue.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(task), 1)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def call(self, arguments: dict, timeout: float = 10) -> tuple[dict, dict]:
        async with self._lock:
            future = None
            try:
                if self._task is None or self._task.done():
                    await self._discard()
                    self._queue = asyncio.Queue()
                    self._ready = asyncio.get_running_loop().create_future()
                    self._ready.add_done_callback(_consume)
                    self._task = asyncio.create_task(self._owner(self._queue, self._ready))
                receipt = await asyncio.wait_for(asyncio.shield(self._ready), timeout)
                future = asyncio.get_running_loop().create_future()
                future.add_done_callback(_consume)
                await self._queue.put((arguments, future))
                value = await asyncio.wait_for(asyncio.shield(future), timeout)
                return value, receipt
            except BaseException as exc:
                if future is not None and not future.done():
                    future.cancel()
                await self._discard()
                if isinstance(exc, (asyncio.CancelledError, ReportProtocolError)):
                    raise
                raise ReportProcessError("plugin timeout or disconnection") from exc

    async def close(self) -> None:
        async with self._lock:
            await self._discard()


async def run(question, hits, *, index_version, run_id, attempt=1, timeout=10.0, journal_dir=None, runtime=None):
    lookup = for_question(question)
    trace = new_trace(run_id=run_id, case_id="catl-operating-cash-period-dev" if lookup and not lookup.sales_intent
                      else "apple-services-growth-contribution-dev", attempt=attempt, question=question,
                      index_version=index_version, hits=hits)
    call = trace["_call"]
    owned, runtime = runtime is None, runtime or ReportRuntime()
    result, state = None, "unknown"
    try:
        try:
            validate_input(call)
        except ReportProtocolError:
            event(trace, "hook_decision", "denied")
            raise
        event(trace, "hook_decision", "ok")
        event(trace, "tool_call", "ok", arguments={"input_sha256": hashed(call)})
        result, receipt = await runtime.call(call, timeout)
        validate_result(result, call)
        event(trace, "tool_result", "ok", result={"output_sha256": result["output_sha256"], "receipt": receipt})
        state = "success"
        return result, trace
    except ReportProtocolError:
        result, state = None, "failure"
        event(trace, "tool_error", "error", error_code="REPORT_TOOL_PROTOCOL")
        await runtime.close()
        raise
    except (ReportProcessError, asyncio.CancelledError):
        result, state = None, "unknown"
        event(trace, "tool_error", "unknown", error_code="REPORT_TOOL_PROCESS")
        raise
    finally:
        finish(trace, result, state)
        try:
            journal(journal_dir, trace)
        finally:
            if owned:
                await runtime.close()


async def _serve():
    server = Server("forgeflow-annual-report-plugin", version=f"{RUNTIME_VERSION}:{os.getpid()}")

    @server.list_tools()
    async def list_tools():
        return [types.Tool(name=TOOL_NAME, description="Read-only annual report answer", inputSchema=INPUT_SCHEMA)]

    @server.call_tool(validate_input=False)
    async def tool(name, arguments):
        try:
            if name != TOOL_NAME:
                raise ReportProtocolError("unknown tool")
            value = answer(arguments)
        except ReportProtocolError:
            return types.CallToolResult(content=[types.TextContent(type="text", text="REPORT_TOOL_PROTOCOL")], isError=True)
        return types.CallToolResult(content=[types.TextContent(type="text", text=canonical(value))], structuredContent=value)

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    if not parser.parse_args().serve:
        parser.error("--serve is required")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
