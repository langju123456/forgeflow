"""The 年报问答 desk with reranking: off by default, the registered settings when on, hybrid alone when it cannot load."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

import forgeflow.report_desk as report_desk  # noqa: E402
import forgeflow.retrieval as retrieval  # noqa: E402
from forgeflow.desk_service.config import DeskConfig  # noqa: E402
from forgeflow.desk_service.retrieval_server import desk_from_config  # noqa: E402
from forgeflow.retrieval import CrossEncoderReranker, HybridRanker, RerankRanker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EN = "cc_form10k_2025_en"
REGISTRATION = ROOT / "preparation/s4/real-reports/rerank/preregistered.json"
SEALED_RUN = ROOT / "preparation/s4/real-reports/rerank/runs/rerank-20261126T091738Z/report.json"


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
    ]}))
    return folder, sources


class FakeEmbedder:
    """Stands in for bge-m3: every vector points the same way, so every row clears the 0.50 floor."""

    def __init__(self, model="bge-m3", base_url="", timeout=0.0):
        self.model_id = f"fake/{model}"

    def embed(self, texts):
        return [[1.0, 1.0 + (len(text) % 3) / 10] for text in texts]


class LengthReranker:
    """Scores a passage by its length, so the reranked order is easy to predict; keeps what it was sent."""

    model_id = "test/length"

    def __init__(self):
        self.calls = []
        self.loaded = False

    def load(self):
        self.loaded = True
        return self

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        return [float(len(passage)) for passage in passages]


@pytest.fixture
def local_models(monkeypatch):
    """A local Ollama that lists bge-m3 (and no chat model), with the embedder faked."""
    monkeypatch.setattr(report_desk, "ollama_tags", lambda base_url=report_desk.OLLAMA: {"bge-m3:latest"})
    monkeypatch.setattr(retrieval, "OllamaEmbedder", FakeEmbedder)


def build(fixtures, work, **kwargs):
    folder, sources = fixtures
    desk = report_desk.ReportDesk(folder, work, ollama_url="http://127.0.0.1:9", sources=sources, **kwargs)
    desk.load()
    return desk


def test_rerank_is_off_by_default(fixtures, tmp_path, local_models, monkeypatch):
    def refuse():
        raise AssertionError("the reranker must not be built unless the desk is asked to rerank")

    monkeypatch.setattr(report_desk, "registered_reranker", refuse)
    desk = build(fixtures, tmp_path)
    assert desk.mode == "hybrid" and desk.reranker is None and desk.status["rerank"] == "off"
    assert isinstance(desk.service.ranker, HybridRanker)


def test_switched_on_it_reorders_the_first_30_hybrid_candidates(fixtures, tmp_path, local_models, monkeypatch):
    reranker = LengthReranker()
    monkeypatch.setattr(report_desk, "registered_reranker", lambda: reranker)
    desk = build(fixtures, tmp_path, rerank=True)
    assert desk.mode == "hybrid+rerank" and desk.status["state"] == "ready"
    assert desk.status["rerank"].startswith("on: BAAI/bge-reranker-v2-m3@953dc6f6")
    assert reranker.loaded  # loaded while the desk loads, not inside the first question
    ranker = desk.service.ranker
    assert isinstance(ranker, RerankRanker) and isinstance(ranker.base, HybridRanker)
    assert ranker.reranker is reranker and ranker.candidates == 30

    hits = desk.evidence("total net sales")["hits"]
    query, sent = reranker.calls[-1]
    assert query == "total net sales"
    assert 0 < len(sent) <= 30
    assert set(sent) <= {row["content"] for row in desk._row_index.values()}  # only rows the desk loaded
    # The first five are the five longest passages the reranker scored, longest first.
    assert [len(h.content) for h in hits] == sorted((len(p) for p in sent), reverse=True)[: len(hits)]


def test_rerank_needs_hybrid_retrieval(fixtures, tmp_path, monkeypatch):
    monkeypatch.setattr(report_desk, "ollama_tags", lambda base_url=report_desk.OLLAMA: set())

    def refuse():
        raise AssertionError("without bge-m3 there is no hybrid result to rerank")

    monkeypatch.setattr(report_desk, "registered_reranker", refuse)
    desk = build(fixtures, tmp_path, rerank=True)
    assert desk.mode == "lexical" and desk.reranker is None
    assert desk.status["rerank"].startswith("not used") and "bge-m3" in desk.status["rerank"]


def test_a_reranker_that_cannot_load_leaves_hybrid_and_says_why(fixtures, tmp_path, local_models, monkeypatch):
    class Missing(LengthReranker):
        def load(self):
            raise OSError("weights are not in the local cache")

    monkeypatch.setattr(report_desk, "registered_reranker", Missing)
    desk = build(fixtures, tmp_path, rerank=True)
    assert desk.mode == "hybrid" and desk.reranker is None and desk.status["state"] == "ready"
    assert isinstance(desk.service.ranker, HybridRanker)
    assert "OSError" in desk.status["rerank"] and "local cache" in desk.status["rerank"]


def test_answers_cached_without_rerank_are_not_served_with_it(fixtures, tmp_path, local_models, monkeypatch):
    plain = build(fixtures, tmp_path / "plain")
    monkeypatch.setattr(report_desk, "registered_reranker", LengthReranker)
    reranked = build(fixtures, tmp_path / "reranked", rerank=True)
    assert plain._row_index.keys() == reranked._row_index.keys()
    assert plain.index_version() != reranked.index_version()


def test_the_desk_uses_the_registered_settings_and_the_evaluated_snapshot():
    registered = json.loads(REGISTRATION.read_text())["reranker"]
    assert (report_desk.RERANK_MODEL, report_desk.RERANK_DEVICE, report_desk.RERANK_MAX_LENGTH,
            report_desk.RERANK_BATCH_SIZE, report_desk.RERANK_CANDIDATES) == (
        registered["model"], registered["device"], registered["max_length"], registered["batch_size"],
        registered["candidates"])
    sealed = json.loads(SEALED_RUN.read_text())["reranker"]["snapshot"]["commit"]
    assert report_desk.RERANK_REVISION == sealed
    built = report_desk.registered_reranker()  # built, not loaded: nothing is read or downloaded here
    assert isinstance(built, CrossEncoderReranker) and built._encoder is None
    assert (built.model_id, built.device, built.max_length, built.batch_size) == (
        f"sentence-transformers/{registered['model']}", "mps", 1024, 8)
    assert built._revision == sealed and built._local_files_only is True


@pytest.mark.parametrize(("raw", "expected"), [(None, False), ("1", True), ("true", True), ("True", True),
                                                ("on", True), ("0", False), ("false", False), ("False", False),
                                                ("off", False), ("", False)])
def test_the_service_switch_reads_true_and_false_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.delenv("FORGEFLOW_DESK_RERANK", raising=False)
    if raw is not None:
        monkeypatch.setenv("FORGEFLOW_DESK_RERANK", raw)
    config = DeskConfig.from_env()
    assert config.rerank is expected
    assert DeskConfig.from_env(**{}).to_env()["FORGEFLOW_DESK_RERANK"] == str(expected)


def test_the_service_switch_refuses_anything_else(monkeypatch):
    monkeypatch.setenv("FORGEFLOW_DESK_RERANK", "maybe")
    with pytest.raises(ValueError):
        DeskConfig.from_env()


def test_the_retrieval_server_builds_its_desk_with_the_switch(tmp_path):
    assert desk_from_config(DeskConfig(work_dir=str(tmp_path))).rerank is False
    assert desk_from_config(DeskConfig(work_dir=str(tmp_path), rerank=True)).rerank is True
