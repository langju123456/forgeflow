"""Role and revision invariants against HTTP, SQLite, and the real typed lookup on a synthetic two-row store."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow import annual_cash, apple_sales, research_api, research_store
from forgeflow.contracts import digest
from forgeflow.evt_store import EVTBoundStore
from forgeflow.report_desk import LOADER, SCOPE, ReportDesk, mount_report_desk
from forgeflow.retrieval import RankedKnowledgeService

Q = "宁德时代2025年经营现金流净额是多少？"


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("FORGEFLOW_RESEARCH_DIR", str(root))
    users = {name: {"Authorization": "Bearer " + research_store.issue(root, name, role)}
             for name, role in [("a", "author"), ("b", "author"), ("r", "reviewer"), ("q", "reviewer")]}
    d = ReportDesk(tmp_path, tmp_path)
    d.store = EVTBoundStore(f"sqlite:///{tmp_path / 'index.db'}")
    d.store.create_schema()
    d.service = RankedKnowledgeService(d.store)
    d.reports = [{"key": annual_cash.DOCUMENT, "company": "合成年报", "state": "ok",
                  "sha256": "2" * 64, "url": "https://example.org/fixture", "document": "合成年度表"}]
    text = ("[source] fixture.pdf | p.11 [0,0,10,10] | sha256 " + "2"*16 + "\n"
            "[table] 摘要 | 单位:千元 (x1000 CNY)\n项目 | 2025年\n经营活动产生的现金流量净额 | 123")
    d.store.put_document(LOADER, SCOPE, annual_cash.DOCUMENT + "#p11-t1", "v1", text, ["finance"], 1)
    app = FastAPI()
    mount_report_desk(app, d)
    with TestClient(app, client=("127.0.0.1", 40000)) as client:
        result = client.post("/reports/ask", headers=users["a"], json={"question": Q}).json()
        yield client, users, d, result


def test_private_revision_flow_and_restart(scene):
    c, u, desk, answer = scene
    new = {"grant_id": answer["research_grant"], "note": "补上年度和合并口径"}
    assert "research_grant" not in c.post("/reports/ask", json={"question": Q}).json()
    assert c.post("/research/drafts", headers=u["b"], json=new).status_code == 404
    assert c.get("/research/drafts").status_code == 401
    assert c.post("/research/drafts", headers=u["a"], json=new | {"answer": "自由覆盖金额"}).status_code == 422
    assert c.post("/research/drafts", headers=u["r"], json=new).status_code == 403
    saved = c.post("/research/drafts", headers=u["a"], json=new).json()
    path = "/research/drafts/" + saved["id"]
    assert c.get(path, headers=u["b"]).status_code == c.get(path, headers=u["r"]).status_code == 404
    assert c.get("/research/drafts", headers=u["b"]).json() == []
    def act(user, action, expected, **more):
        return c.post(path + "/decisions", headers=u[user], json={"action": action, "expected": expected, **more})
    first = saved["head"]
    assert digest(saved["payload"]) == first
    assert act("a", "deliver", first).status_code == 409
    assert act("a", "submit", first, reviewer="r").json()["state"] == "submitted"
    assert act("a", "approve", first).status_code == 403
    assert act("q", "approve", first).status_code == 404
    assert c.post(path + "/revisions", headers=u["a"], json=new | {"expected": first}).status_code == 409
    assert act("r", "return", first).status_code == 422
    assert act("r", "return", first, memo="补充原表说明").json()["state"] == "returned"
    second = c.post(path + "/revisions", headers=u["a"], json=new | {"expected": first, "note": "<研究员> 补齐"}).json()
    assert second["payload"]["parent_digest"] == first
    assert c.get(path + "/versions/2/export/json", headers=u["r"]).status_code == 404
    assert c.get(path + "/versions/1/export/json", headers=u["r"]).json()["state"] == "returned"
    assert c.post(path + "/revisions", headers=u["a"], json=new | {"expected": first}).status_code == 409
    assert act("a", "submit", second["head"], reviewer="q").status_code == 409
    assert act("a", "submit", second["head"], reviewer="r").status_code == 200
    assert act("r", "approve", first).status_code == 409
    assert act("r", "approve", second["head"]).json()["state"] == "approved"
    with ThreadPoolExecutor(max_workers=4) as pool:
        deliveries = list(pool.map(lambda _: act("a", "deliver", second["head"]).json(), range(4)))
    assert all(len([e for e in d["events"] if e["kind"] == "delivered"]) == 1 for d in deliveries)
    archive = c.get(path + "/versions/2/export/json", headers=u["a"])
    assert archive.headers["cache-control"] == "no-store" and len(archive.json()["local_receipt"]) == 64
    assert digest(archive.json()["payload"]) == second["head"]
    assert "&lt;研究员&gt;" in c.get(path + "/versions/2/export/html", headers=u["r"]).text
    assert "local_receipt" in archive.json() and c.get(path, headers=u["q"]).status_code == 404
    # Snapshot the pre-grant schema in this disposable private fixture. Existing delivered revisions must survive.
    with sqlite3.connect(research_store.root_path() / "research.sqlite3") as old:
        old.execute("DROP TABLE source_request")
        old.execute("PRAGMA user_version=0")
    app = FastAPI()
    mount_report_desk(app, desk)
    with TestClient(app, client=("127.0.0.1", 50001)) as restarted:
        assert restarted.get(path + "/versions/2/export/json", headers=u["a"]).content == archive.content
        assert restarted.post("/research/drafts", headers=u["a"], json=new).status_code == 404
        fresh = restarted.post("/reports/ask", headers=u["a"], json={"question": Q}).json()
        assert restarted.post("/research/drafts", headers=u["a"], json={"grant_id": fresh["research_grant"]}).status_code == 200
    with TestClient(app, client=("192.0.2.1", 50001)) as remote:
        assert remote.get(path, headers=u["a"]).status_code == 403


def test_source_binding_and_cached_service_run(scene):
    c, u, desk, result = scene
    pytest.importorskip("grpc")
    from forgeflow.desk_service.gateway import mount_desk_gateway

    proxy = AsyncMock()
    proxy.job.return_value = {"status": "done", "result": result | {"cached": True}}
    proxy.ask.return_value = 200, {"job_id": "cache-hit", "state": "cached"}
    proxy.info.return_value = {"reports": desk.reports}
    app = FastAPI()
    mount_desk_gateway(app, lambda: proxy)
    with TestClient(app, client=("::1", 41000)) as gateway:
        grant = gateway.post("/v2/reports/ask", headers=u["a"], json={"question": Q}).json()["research_grant"]
        answer = gateway.post("/research/drafts", headers=u["a"], json={"grant_id": grant}).json()
        facts = answer["payload"]["evidence"]
        assert facts["request_id"] == "cache-hit" and facts["run_id"] == result["run_id"] and facts["cached"]
        assert facts["citations"][0]["source_hash"] == result["kept"][0]["source_hash"]
        proxy.job.return_value = {"status": "done", "result": result | {"status": "PERIOD_UNVERIFIED"}}
        assert gateway.post("/research/drafts", headers=u["a"], json={"grant_id": grant}).status_code == 422
    desk.reports[0]["sha256"] = "0" * 64
    assert c.post("/research/drafts", headers=u["a"], json={"grant_id": result["research_grant"]}).status_code == 409


def test_metric_patch_preserves_other_checked_apple_fact_and_marks_the_rechecked_conclusion():
    base = {"tool_version": apple_sales.VERSION, "run_id": "run-before", "source": {"sha256": "a" * 64},
            "citations": [{"id": "page-26"}], "calculation": {"periods": [2025, 2024], "unit": "百万美元",
            "services": [109158, 96169], "total_sales": [416161, 391035], "operations": [
                {"metric": "服务收入增速", "formula": "服务收入增量 / 上期服务收入", "numerator": 12989,
                 "denominator": 96169, "percent": "13.51"},
                {"metric": "服务对公司收入增量的贡献", "formula": "服务收入增量 / 公司收入增量", "numerator": 12989,
                 "denominator": 25126, "percent": "51.70"}]}}
    incoming = {**base, "run_id": "run-after", "citations": [{"id": "page-39"}], "calculation":
                {**base["calculation"], "operations": [base["calculation"]["operations"][1]]}}
    patched = research_api.merge_metric_update(base, incoming)
    assert [op["metric"] for op in patched["calculation"]["operations"]] == ["服务收入增速", "服务对公司收入增量的贡献"]
    assert patched["metric_updates"] == {"服务对公司收入增量的贡献": {"run_id": "run-after",
        "tool_version": apple_sales.VERSION, "citation_ids": ["page-39"]}}
    assert {citation["id"] for citation in patched["citations"]} == {"page-26", "page-39"}
    assert "服务收入增速为13.51%" in patched["answer"] and "贡献为51.70%" in patched["answer"]
    next_query = {**incoming, "run_id": "run-third", "calculation":
                  {**base["calculation"], "operations": [base["calculation"]["operations"][0]]}}
    third = research_api.merge_metric_update(patched, next_query)
    assert third["metric_updates"]["服务对公司收入增量的贡献"]["run_id"] == "run-after"
    assert third["metric_updates"]["服务收入增速"]["run_id"] == "run-third"
    assert third["metric_patch"]["prior_run_id"] == "run-after"
    assert "服务收入增速" not in patched["metric_updates"]
    assert "metric_updates" not in base

    for key, value in (("periods", [2024, 2023]), ("unit", "千美元"),
                       ("services", [1, 2]), ("total_sales", [3, 4]),
                       ("operations", []), ("operations", [{"metric": "未经批准的第三指标"}])):
        changed = deepcopy(incoming)
        changed["calculation"][key] = value
        with pytest.raises(research_api.DomainError) as exc:
            research_api.merge_metric_update(base, changed)
        assert exc.value.code == "METRIC_PATCH_UNAVAILABLE"
    for key, value in (("source", {"sha256": "b" * 64}), ("tool_version", "foreign-tool")):
        with pytest.raises(research_api.DomainError) as exc:
            research_api.merge_metric_update(base, {**incoming, key: value})
        assert exc.value.code == "METRIC_PATCH_UNAVAILABLE"


@pytest.mark.parametrize("failure,code", [("process", 503), ("protocol", 502)])
def test_mcp_failure_never_creates_source_grant(scene, monkeypatch, failure, code):
    from forgeflow.report_plugin import ReportProcessError, ReportProtocolError
    client, users, _, _ = scene
    with sqlite3.connect(research_store.root_path() / "research.sqlite3") as connection:
        before = connection.execute("SELECT count(*) FROM source_request").fetchone()[0]
    error = ReportProcessError if failure == "process" else ReportProtocolError
    monkeypatch.setattr(client.app.state.report_runtime, "call", AsyncMock(side_effect=error("injected")))
    response = client.post("/reports/ask", headers=users["a"], json={"question": Q})
    assert response.status_code == code and "research_grant" not in response.json()
    with sqlite3.connect(research_store.root_path() / "research.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM source_request").fetchone()[0] == before


@pytest.mark.parametrize("field", ["answer", "source_hash", "row_version", "page", "denied_hook", "missing_trace"])
def test_tampered_trace_or_outward_result_cannot_become_evidence(scene, field):
    _, _, desk, original = scene
    result = deepcopy(original)
    if field == "answer":
        result["answer"] = "forged"
    elif field in {"source_hash", "row_version"}:
        result["citations"][0][field] = "forged"
    elif field == "page":
        result["citations"][0]["page"] = 999
    elif field == "denied_hook":
        result["tool_trace"]["events"][0]["status"] = "denied"
    else:
        result.pop("tool_trace")
    with pytest.raises(research_api.DomainError) as error:
        research_api.fact_record(result, desk.reports, "sync")
    assert error.value.code == "SOURCE_CHANGED"


def test_service_delays_grant_until_verified_completion_and_requester(scene):
    from types import SimpleNamespace

    from forgeflow.desk_service.gateway import mount_desk_gateway
    c, users, desk, answer = scene
    proxy = AsyncMock()
    proxy.config = SimpleNamespace(job_ttl_s=3600)
    proxy.ask.return_value = 202, {"job_id": "queued-job", "state": "queued"}
    proxy.job.return_value = {"status": "queued"}
    proxy.info.return_value = {"reports": desk.reports}
    members = set()
    async def add(key, subject):
        members.add((key, subject))
    async def has(key, subject):
        return (key, subject) in members
    proxy.redis.sadd.side_effect = add
    proxy.redis.sismember.side_effect = has
    app = FastAPI()
    mount_desk_gateway(app, lambda: proxy)
    def count():
        with sqlite3.connect(research_store.root_path() / "research.sqlite3") as connection:
            return connection.execute("SELECT count(*) FROM source_request").fetchone()[0]
    before = count()
    path = "/v2/jobs/queued-job/research-grant"
    with TestClient(app, client=("127.0.0.1", 41001)) as gateway:
        queued = gateway.post("/v2/reports/ask", headers=users["a"], json={"question": Q})
        assert queued.status_code == 202 and queued.json()["research_grant_pending"]
        assert "research_grant" not in queued.json() and count() == before
        assert gateway.post(path, headers=users["a"]).status_code == 409
        assert gateway.post(path, headers=users["b"]).status_code == 404
        assert gateway.post(path, headers=users["r"]).status_code == 403
        assert gateway.post(path).status_code == 401
        assert count() == before
        proxy.job.return_value = {"status": "failed"}
        assert gateway.post(path, headers=users["a"]).status_code == 409
        bad = deepcopy(answer)
        bad["tool_trace"]["events"][0]["status"] = "denied"
        proxy.job.return_value = {"status": "done", "result": bad}
        assert gateway.post(path, headers=users["a"]).status_code == 409
        cached_bad = gateway.post("/v2/reports/ask", headers=users["a"], json={"question": Q}).json()
        assert "research_grant" not in cached_bad and cached_bad["research_grant_error"]
        assert count() == before
        proxy.job.return_value = {"status": "done", "result": answer}
        granted = gateway.post(path, headers=users["a"])
        assert granted.status_code == 200 and granted.headers["cache-control"] == "no-store"
        assert count() == before + 1
        assert gateway.post("/research/drafts", headers=users["a"],
                            json={"grant_id": granted.json()["research_grant"]}).status_code == 200
        assert gateway.get(path, headers=users["a"]).status_code == 405
        assert count() == before + 1
