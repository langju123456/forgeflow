"""The 年报问答 desk: only verified files are ingested, answers cite only what retrieval returned, pages render."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

from forgeflow.contracts import DomainError  # noqa: E402
from forgeflow.report_desk import ReportDesk, mount_report_desk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EN = "cc_form10k_2025_en"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("make_financial_pdfs", ROOT / "scripts/make_financial_pdfs.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    folder = tmp_path_factory.mktemp("reports")
    generator.generate(folder)
    data = (folder / f"{EN}.pdf").read_bytes()
    sources = folder / "sources.json"
    sources.write_text(json.dumps({"reports": [
        {"key": EN, "company": "Example Co.", "document": "Form 10-K", "language": "en", "pages": 2, "url": "-",
         "sha256": hashlib.sha256(data).hexdigest()},
        {"key": "absent_report", "company": "Nobody", "document": "-", "language": "en", "pages": 1, "url": "-",
         "sha256": "0" * 64},
    ]}))
    return folder, sources


class FakeAnswerer:
    def __init__(self):
        self.seen = []

    def __call__(self, question, hits):
        self.seen.append([h.id for h in hits])
        return {"found": True, "answer": "from the first hit", "citations": [hits[0].id, "made-up-id"]}


@pytest.fixture(scope="module")
def desk(fixtures, tmp_path_factory):
    folder, sources = fixtures
    desk = ReportDesk(folder, tmp_path_factory.mktemp("work"), ollama_url="http://127.0.0.1:9",
                      answerer=FakeAnswerer(), sources=sources)
    desk.load()
    return desk


def test_only_files_matching_their_recorded_hash_are_loaded(desk):
    states = {r["key"]: r["state"] for r in desk.reports}
    assert states == {EN: "ok", "absent_report": "missing"}
    assert desk.status["state"] == "ready" and desk.mode == "lexical"  # no Ollama on port 9
    assert desk.status["rows"] > 0


def test_answers_cite_only_hits_that_retrieval_returned(desk):
    result = desk.ask("total net sales", gate=False)
    assert result["found"] is True
    returned = {c["id"] for c in result["kept"]}
    assert [c["id"] for c in result["citations"]] == [desk.answerer.seen[-1][0]]  # the made-up id is dropped
    assert {c["id"] for c in result["citations"]} <= returned
    first = result["citations"][0]
    assert first["doc"] == EN and isinstance(first["page"], int) and len(first["bbox"]) == 4


def test_a_cited_page_renders_with_its_region_highlighted(desk):
    cite = desk.ask("total net sales", gate=False)["citations"][0]
    png = desk.page_png(EN, cite["page"], cite["bbox"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(DomainError):
        desk.page_png("absent_report", 1, None)


def test_questions_wait_until_the_reports_are_loaded(fixtures, tmp_path):
    folder, sources = fixtures
    fresh = ReportDesk(folder, tmp_path, ollama_url="http://127.0.0.1:9", sources=sources)
    with pytest.raises(DomainError) as error:
        fresh.ask("anything")
    assert error.value.code == "DESK_NOT_READY"


def test_http_routes(desk):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    mount_report_desk(app, desk)
    client = TestClient(app)
    info = client.get("/reports").json()
    assert info["status"]["state"] == "ready" and all("path" not in r for r in info["reports"])
    asked = client.post("/reports/ask", json={"question": "total net sales", "gate": False}).json()
    cite = asked["citations"][0]
    image = client.get(f"/reports/{cite['doc']}/pages/{cite['page']}.png", params={"box": ",".join(map(str, cite["bbox"]))})
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert client.get("/reports/absent_report/pages/1.png").status_code == 404
    assert client.post("/reports/ask", json={"question": "  "}).status_code == 422


class CountingGate:
    def __init__(self):
        self.calls = 0

    def check(self, question, hits):
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(status="SUPPORTED", kept=list(hits[:1]))


def test_the_gate_runs_only_when_asked_for(desk):
    # Off by default: on the real reports it missed its pre-registered bar (preparation/s4/real-reports/RESULTS.md).
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    saved, desk.gate = desk.gate, CountingGate()
    try:
        assert desk.ask("total net sales")["gate"] == "OFF" and desk.gate.calls == 0
        app = FastAPI()
        mount_report_desk(app, desk)
        client = TestClient(app)
        assert client.post("/reports/ask", json={"question": "total net sales"}).json()["gate"] == "OFF"
        assert desk.gate.calls == 0
        gated = client.post("/reports/ask", json={"question": "total net sales", "gate": True}).json()
        assert gated["gate"] == "SUPPORTED" and desk.gate.calls == 1 and len(gated["kept"]) == 1
    finally:
        desk.gate = saved


class ScriptedChatModel:
    """Stands in for the local model only; the gate itself is the real AnswerabilityGate."""

    model_id = "test/scripted"

    def complete(self, messages, schema):
        import re

        text = messages[-1]["content"]
        last = max(int(n) for n in re.findall(r"^\[(\d+)\] ", text, flags=re.M))
        quote = re.search(rf"^\[{last}\] (.+)$", text, flags=re.M).group(1)
        return json.dumps({"verdicts": [{"passage": n, "answers": n == last, "quote": quote if n == last else ""}
                                        for n in range(1, last + 1)]})


def test_the_real_gate_keeps_exactly_the_hits_it_supported(desk):
    # The real gate reports kept hits as positions (AnswerabilityResult.kept) and the hits themselves in .hits.
    # A hand-written double with a different shape let the desk read .kept as hits and fail only in real use.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forgeflow.answerability import AnswerabilityGate
    from forgeflow.report_desk import ANALYST, SCOPE

    saved, desk.gate = desk.gate, AnswerabilityGate(ScriptedChatModel())
    try:
        hits = desk.service.search(ANALYST, SCOPE, "total net sales", 5)
        assert hits, "the fixture report must return something for this question"
        result = desk.ask("total net sales", gate=True)
        assert result["gate"] == "SUPPORTED"
        assert [c["id"] for c in result["kept"]] == [hits[-1].id]
        assert [c["id"] for c in result["dropped"]] == [h.id for h in hits[:-1]]
        assert desk.answerer.seen[-1] == [hits[-1].id]  # the answer step only sees what the gate kept
        app = FastAPI()
        mount_report_desk(app, desk)
        response = TestClient(app).post("/reports/ask", json={"question": "total net sales", "gate": True})
        assert response.status_code == 200 and [c["id"] for c in response.json()["kept"]] == [hits[-1].id]
    finally:
        desk.gate = saved


def test_the_gate_uses_the_long_context_model_when_it_is_installed():
    from forgeflow.report_desk import pick_chat_model

    assert pick_chat_model({"qwen3:8b", "forgeflow-qwen3-8b-ctx16k:latest", "bge-m3:latest"}) == "forgeflow-qwen3-8b-ctx16k"
    assert pick_chat_model({"qwen3:8b", "bge-m3:latest"}) == "qwen3:8b"
    assert pick_chat_model({"bge-m3:latest"}) is None
