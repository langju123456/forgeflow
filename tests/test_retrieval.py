"""Ranking modes behind the S1 ACL and citation checks: parity, ACL-before-embedding, fusion, floor."""

import json
import sys
from pathlib import Path

import pytest

from forgeflow.context import KnowledgeService
from forgeflow.contracts import DomainError, Scope
from forgeflow.retrieval import (
    HybridRanker,
    OllamaEmbedder,
    RankedKnowledgeService,
    VectorRanker,
    lexical_ranker,
)

EVAL = Path(__file__).resolve().parents[1] / "preparation/s2/evaluation"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "hybrid"))
import evaluator as ev  # noqa: E402
from run_modes import FakeEmbedder  # noqa: E402


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    data = ev.validate_dataset(json.loads((EVAL / "dataset.json").read_text()))
    store, _ = ev.seed_store(data, "sqlite:///" + str(tmp_path_factory.mktemp("s2") / "eval.db"))
    yield data, store, ev.principals(data)
    store.engine.dispose()


def outcome(reader, data, identities, q):
    try:
        hits = reader.search(identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]]), q["query"], 5)
        return [(h.id, h.version, h.source_hash, h.status) for h in hits]
    except DomainError as error:
        return error.code


def test_lexical_mode_returns_exactly_what_the_s1_service_returns(frozen):
    data, store, identities = frozen
    s1, ranked = KnowledgeService(store), RankedKnowledgeService(store, lexical_ranker)
    for q in data["questions"]:
        assert outcome(ranked, data, identities, q) == outcome(s1, data, identities, q), q["id"]


def test_the_embedder_only_ever_sees_content_the_caller_may_read(frozen):
    data, store, identities = frozen
    for q in data["questions"]:
        identity, scope = identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]])
        vector = VectorRanker(FakeEmbedder(), min_similarity=-1.0)  # fresh cache: every text sent is visible
        outcome(RankedKnowledgeService(store, HybridRanker(vector)), data, identities, q)
        try:
            readable = {str(row["content"]) for row in store.document_rows(identity, scope)}
        except DomainError:
            readable = set()
        sent = [text for text in vector.embedded_texts if text != q["query"]]
        assert set(sent) <= readable, q["id"]


class TableEmbedder:
    model_id = "test/table"

    def __init__(self, table):
        self.table = table

    def embed(self, texts):
        return [self.table[text] for text in texts]


ROWS = [
    {"id": "a", "version": "1", "content": "alpha", "source_hash": "ha"},
    {"id": "b", "version": "1", "content": "beta words", "source_hash": "hb"},
    {"id": "c", "version": "1", "content": "gamma", "source_hash": "hc"},
]
VECTORS = {"alpha": [1.0, 0.0], "beta words": [0.6, 0.8], "gamma": [0.0, 1.0], "beta": [1.0, 0.1], "zzz": [-1.0, -0.2]}


def test_vector_floor_drops_weak_matches_and_unanswerable_queries_come_back_empty():
    vector = VectorRanker(TableEmbedder(VECTORS), min_similarity=0.5)
    assert [row["id"] for _, row in vector("beta", ROWS)] == ["a", "b"]
    assert vector("zzz", ROWS) == []


def test_rrf_puts_a_row_both_rankers_agree_on_first():
    vector = VectorRanker(TableEmbedder(VECTORS), min_similarity=0.5)
    fused = HybridRanker(vector, k=60)("beta", ROWS)
    # lexical finds only b (rank 1); vector ranks a then b; b gets 1/61 + 1/62, a gets 1/61.
    assert [row["id"] for _, row in fused] == ["b", "a"]
    assert fused[0][0] == pytest.approx(1 / 61 + 1 / 62)


def test_vectors_are_cached_by_content_hash():
    calls = []
    embedder = TableEmbedder(VECTORS)
    original = embedder.embed
    embedder.embed = lambda texts: calls.append(list(texts)) or original(texts)
    vector = VectorRanker(embedder, min_similarity=0.0)
    vector("beta", ROWS)
    vector("beta", ROWS)
    assert calls == [["beta"], ["alpha", "beta words", "gamma"], ["beta"]]


def test_ollama_embedder_refuses_anything_but_a_local_server():
    with pytest.raises(ValueError):
        OllamaEmbedder(base_url="https://embeddings.example.com")
    OllamaEmbedder(base_url="http://127.0.0.1:11434")
    OllamaEmbedder(base_url="http://localhost:11434")
