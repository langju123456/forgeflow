"""Role and revision invariants against HTTP, SQLite, and the real typed lookup on a synthetic two-row store."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow import annual_cash, research_store
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
    client = TestClient(app, client=("127.0.0.1", 40000))
    result = client.post("/reports/ask", headers=users["a"], json={"question": Q}).json()
    yield client, users, d, result
    client.close()


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
