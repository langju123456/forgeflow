import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgeflow import annual_cash as cash
from forgeflow.contracts import ContextHit, Principal
from forgeflow.evt_store import EVTBoundStore
from forgeflow.report_desk import LOADER, SCOPE, ReportDesk
from forgeflow.retrieval import RankedKnowledgeService

Q = "宁德时代 2025 年经营活动产生的现金流量净额是多少？"


def table(value="123", header="2025年", title="摘要 | 单位:千元 (x1000 CNY)"):
    return f"source\n[table] {title}\n项目 | {header}\n{cash.METRIC} | {value}"


@pytest.fixture
def desk(tmp_path):
    d = ReportDesk(tmp_path, tmp_path)
    d.store = EVTBoundStore(f"sqlite:///{tmp_path / 'reports.db'}")
    d.store.create_schema()
    d.service = RankedKnowledgeService(d.store)
    d.reports = [{"key": cash.DOCUMENT, "company": "宁德时代"}]
    return d


def add(desk, index, body, roles=("finance",)):
    desk.store.put_document(LOADER, SCOPE, f"{cash.DOCUMENT}#p{index}-t1", "1", body, list(roles), 1)


def test_annual_route_uses_only_authorized_matching_columns(desk):
    add(desk, 12, table("55", "第四季度"))
    add(desk, 11, table())
    add(desk, 30, table())
    add(desk, 15, table("456"), ("admin",))
    add(desk, 10, table("987", title="母公司现金流量表 | 单位:千元 (x1000 CNY)"))
    result = desk.ask(Q, gate=True)
    assert result["found"] and "123 千元人民币" in result["answer"]
    assert {c["id"] for c in result["citations"]} == {f"{cash.DOCUMENT}#p{i}-t1" for i in (11, 30)}
    denied = Principal(subject_id="reader", kind="user", roles=frozenset({"other"}), scopes=(SCOPE,), clearance=1)
    assert not desk.evidence(Q, principal=denied)["kept"]


def test_disagreement_before_top_k_quarter_only_and_missing_year(desk):
    add(desk, 12, table("55", ""))
    assert desk.ask(Q)["status"] == "PERIOD_UNVERIFIED"
    add(desk, 11, table())
    add(desk, 30, table("124"))
    result = desk.ask(Q, top_k=1)
    assert not result["found"] and result["status"] == "PERIOD_UNVERIFIED"
    assert result["gate"] == "ANNUAL_CONFLICT"
    assert {c["id"] for c in result["dropped"]} == {f"{cash.DOCUMENT}#p{i}-t1" for i in (11, 30)}
    assert {c["snippet"].splitlines()[1] for c in result["dropped"]} == {"原格 123", "原格 124"}
    assert not desk.ask(Q.replace("2025", "2024"))["found"]


@pytest.mark.parametrize("question", ["宁德时代2025年第四季度经营现金流净额是多少？", "宁德时代与茅台比较",
                                      Q.replace("年", "年母公司"), Q + "同比多少？"])
def test_other_questions_keep_existing_qa(question):
    assert cash.requested_year(question) is None


async def test_worker_checks_period_without_invoking_streaming_model():
    pytest.importorskip("grpc")
    from forgeflow.desk_service.worker import Worker

    body = table()
    hit = ContextHit(id=f"{cash.DOCUMENT}#p1-t1", version="1", source_hash=sha256(body.encode()).hexdigest(),
                     content=body, classification=1, status="CURRENT")
    worker = Worker(None, None, None, None)
    assert (await worker._answer("job", Q, [hit], 1, {}))["citations"] == [hit.id]
    assert (await worker._answer("job", Q, [], 1, {}))["status"] == "PERIOD_UNVERIFIED"


@pytest.mark.real_clock  # the native gRPC server uses the system clock for deadlines
@pytest.mark.parametrize("conflict", [False, True])
async def test_worker_delivers_checked_grpc_citations_in_final_event(desk, conflict):
    grpc = pytest.importorskip("grpc")
    from forgeflow.cited_answer import prompt_sha256
    from forgeflow.desk_service.config import DeskConfig
    from forgeflow.desk_service.proto import desk_pb2_grpc
    from forgeflow.desk_service.retrieval_server import RetrievalServicer, build_server
    from forgeflow.desk_service.worker import Worker
    from forgeflow.report_desk import ANALYST

    add(desk, 11, table())
    add(desk, 30, table("124" if conflict else "123"))
    server, port = build_server(RetrievalServicer(desk), "127.0.0.1:0")
    server.start()
    redis = AsyncMock()
    redis.get.return_value = redis.hget.return_value = None
    redis.hincrby.return_value = 1
    job = {"job_id": "annual-test", "cache_key": "annual-cache", "question": Q,
           "model": "no-stream-method", "prompt_sha256": prompt_sha256(), "enqueued_at": 0,
           "gate": False, "top_k": 1, "subject": ANALYST.subject_id, "index_version": desk.index_version()}
    message = AsyncMock()
    message.body = json.dumps(job).encode()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            worker = Worker(DeskConfig(work_dir=str(desk.work_dir)), redis, desk_pb2_grpc.ReportRetrievalStub(channel),
                            SimpleNamespace(model=job["model"]))
            await worker.handle(message)
        message.ack.assert_awaited_once()
        events = [call.args[1] for call in redis.xadd.await_args_list]
        assert events[-1]["type"] == "final"
        result = json.loads(events[-1]["data"])
        assert result["tool_trace"]["terminal_state"] == "success"
        assert result["tool_trace"]["events"][-1]["result"]["receipt"]["transport"] == "stdio"
        assert result["gate"] == ("ANNUAL_CONFLICT" if conflict else "ANNUAL_COLUMN")
        assert result["found"] is not conflict
        if conflict:
            assert {c["snippet"].splitlines()[1] for c in result["dropped"]} == {"原格 123", "原格 124"}
        else:
            assert result["citations"] == result["kept"]
            assert result["citations"][0]["id"] == f"{cash.DOCUMENT}#p11-t1"
    finally:
        if "worker" in locals():
            await worker.close()
        server.stop(0).wait()
