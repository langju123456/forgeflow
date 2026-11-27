"""Real MCP sessions plus fault injection; fixtures contain synthetic report rows."""
import asyncio
import hashlib
import json
import os
import sys
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from mcp import StdioServerParameters

from forgeflow import annual_cash
from forgeflow import report_plugin as plugin
from forgeflow.report_trace import call_input, hashed

Q = "宁德时代2025年经营现金流净额是多少？"


def hits():
    content = "source\n[table] 摘要 | 单位:千元 (x1000 CNY)\n项目 | 2025年\n经营活动产生的现金流量净额 | 123"
    return [{"id": annual_cash.DOCUMENT + "#p11-t1", "version": "v1", "content": content,
             "source_hash": hashlib.sha256(content.encode()).hexdigest(), "classification": 1, "status": "CURRENT"}]


def arguments():
    return call_input("test-call", Q, "index-test", hits())


async def test_real_mcp_reuses_session_and_closes_process(tmp_path):
    runtime = plugin.ReportRuntime()
    one, first = await plugin.run(Q, hits(), index_version="test", run_id="a", runtime=runtime, journal_dir=tmp_path)
    two, second = await plugin.run(Q, hits(), index_version="test", run_id="b", runtime=runtime, journal_dir=tmp_path)
    receipt = first["events"][-1]["result"]["receipt"]
    assert receipt["transport"] == "stdio" and receipt["tools"] == ["annual_report.answer.v1"]
    assert receipt == second["events"][-1]["result"]["receipt"]
    pid = int(receipt["server_instance"].split(":")[-1])
    os.kill(pid, 0)
    assert one["found"] and two["found"] and "123 千元" in one["answer"]
    await runtime.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    rows = [json.loads(line) for line in (tmp_path / "report-tool-traces.jsonl").read_text().splitlines()]
    assert [row["run_id"] for row in rows] == ["a", "b"]
    assert all(row["terminal_state"] == "success" and "_call" not in row for row in rows)


@pytest.mark.parametrize("mode", ["startup_hang", "call_hang", "crash", "cancel"])
async def test_fault_is_unknown_and_next_call_recovers(tmp_path, monkeypatch, mode):
    script = tmp_path / "fault.py"
    pidfile = tmp_path / "pid"
    script.write_text('''import asyncio, os, sys
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
Path(sys.argv[2]).write_text(str(os.getpid()))
mode = sys.argv[1]
async def main():
    if mode == "startup_hang":
        await asyncio.sleep(100)
    server = Server("fault", version="fault")
    @server.list_tools()
    async def listed():
        return [types.Tool(name="annual_report.answer.v1", inputSchema={"type":"object"})]
    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        if mode == "crash":
            os._exit(12)
        await asyncio.sleep(100)
    async with stdio_server() as (r,w):
        await server.run(r,w,server.create_initialization_options())
asyncio.run(main())
''')
    original = plugin._parameters
    monkeypatch.setattr(plugin, "_parameters", lambda: StdioServerParameters(
        command=sys.executable, args=[str(script), mode, str(pidfile)]))
    runtime = plugin.ReportRuntime()
    task = asyncio.create_task(plugin.run(Q, hits(), index_version="test", run_id="fault",
                                         runtime=runtime, journal_dir=tmp_path, timeout=1))
    if mode == "cancel":
        for _ in range(100):
            if runtime._ready is not None and runtime._ready.done():
                break
            await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(plugin.ReportProcessError):
            await task
    assert runtime._task is None
    if pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    trace = json.loads((tmp_path / "report-tool-traces.jsonl").read_text().splitlines()[0])
    assert trace["terminal_state"] == "unknown" and trace["result"] is None
    assert trace["events"][-1]["error_code"] == "REPORT_TOOL_PROCESS"
    monkeypatch.setattr(plugin, "_parameters", original)
    result, _ = await plugin.run(Q, hits(), index_version="test", run_id="recovery", runtime=runtime)
    assert result["found"]
    await runtime.close()


async def test_invalid_source_denied_before_process_and_recorded(tmp_path):
    runtime = plugin.ReportRuntime()
    runtime.call = AsyncMock(side_effect=AssertionError("must not execute"))
    altered = hits()
    altered[0]["content"] += "forged"
    with pytest.raises(plugin.ReportProtocolError):
        await plugin.run(Q, altered, index_version="test", run_id="denied", runtime=runtime, journal_dir=tmp_path)
    runtime.call.assert_not_awaited()
    trace = json.loads((tmp_path / "report-tool-traces.jsonl").read_text())
    assert trace["terminal_state"] == "failure" and trace["events"][0]["status"] == "denied"


@pytest.mark.parametrize("change", ["foreign_citation", "wrong_version", "wrong_input", "wrong_hash", "unknown_status"])
async def test_malformed_output_is_failure_not_business_abstention(tmp_path, change):
    runtime = plugin.ReportRuntime()
    async def wrong(call, timeout):
        result = plugin.answer(call)
        if change == "foreign_citation":
            result["citations"] = ["outside-row"]
        elif change == "wrong_version":
            result["tool_version"] = "untrusted-v2"
        elif change == "wrong_input":
            result["input_sha256"] = "0" * 64
        elif change == "unknown_status":
            result["status"] = "CUSTOM_PASS"
        result["output_sha256"] = hashed({k: v for k, v in result.items() if k != "output_sha256"})
        if change == "wrong_hash":
            result["output_sha256"] = "0" * 64
        return result, {}
    runtime.call = wrong
    with pytest.raises(plugin.ReportProtocolError):
        await plugin.run(Q, hits(), index_version="test", run_id="bad", runtime=runtime, journal_dir=tmp_path)
    trace = json.loads((tmp_path / "report-tool-traces.jsonl").read_text())
    assert trace["terminal_state"] == "failure" and trace["result"] is None
    assert not any(e["kind"] == "tool_result" for e in trace["events"])


def test_source_validation_does_not_compute_answer(monkeypatch):
    monkeypatch.setattr(annual_cash, "answer_from_period", lambda *a: pytest.fail("host preflight executed calculator"))
    assert plugin.validate_input(deepcopy(arguments()))["question"] == Q


@pytest.mark.parametrize("failure", ["process", "protocol"])
async def test_worker_failures_never_finish_or_cache(tmp_path, monkeypatch, failure):
    from forgeflow.desk_service.config import DeskConfig
    from forgeflow.desk_service.proto import desk_pb2
    from forgeflow.desk_service.worker import Final, Transient, Worker
    redis, stub = AsyncMock(), AsyncMock()
    redis.get.return_value = None
    stub.Search.return_value = desk_pb2.SearchReply(
        kept=[desk_pb2.Hit(**hits()[0])], mode="lexical", index_version="test", gate_status="ANNUAL_COLUMN")
    worker = Worker(DeskConfig(work_dir=str(tmp_path), orchestration="single"), redis, stub, None)
    error = plugin.ReportProcessError if failure == "process" else plugin.ReportProtocolError
    monkeypatch.setattr(plugin.ReportRuntime, "call", AsyncMock(side_effect=error("injected")))
    with pytest.raises(Transient if failure == "process" else Final):
        await worker.process({"job_id": "j", "cache_key": "k", "question": Q,
                              "gate": False, "top_k": 1, "subject": "desk-analyst"}, 1)
    redis.set.assert_not_awaited()
    assert all(call.args[1]["type"] not in {"final", "done"} for call in redis.xadd.await_args_list)
    await worker.close()
    assert worker._report_runtime is None
