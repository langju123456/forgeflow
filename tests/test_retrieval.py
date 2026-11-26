"""Ranking modes behind the S1 ACL and citation checks: parity, ACL-before-embedding, fusion, floor, reranking."""

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
    RerankRanker,
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


class RecordingReranker:
    """Scores passages from a table (0.0 when absent) and keeps every (query, passages) it was given."""

    model_id = "test/recording"

    def __init__(self, scores=None):
        self.scores = scores or {}
        self.calls = []

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        return [self.scores.get(passage, 0.0) for passage in passages]


def test_the_reranker_only_ever_sees_content_the_caller_may_read(frozen):
    data, store, identities = frozen
    sent_in_total = 0
    for q in data["questions"]:
        identity, scope = identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]])
        reranker = RecordingReranker()
        vector = VectorRanker(FakeEmbedder(), min_similarity=-1.0)  # every authorized row becomes a candidate
        ranker = RerankRanker(HybridRanker(vector), reranker, candidates=30)
        outcome(RankedKnowledgeService(store, ranker), data, identities, q)
        # Readable is worked out from the frozen documents, not from what the Store returned.
        readable = {
            doc["content"]
            for doc in data["documents"]
            if doc["scope"] == q["scope"] and identity.permits(scope, set(doc["roles"]), doc["classification"])
        }
        sent = [passage for _, passages in reranker.calls for passage in passages]
        assert set(sent) <= readable, q["id"]
        assert sent == ranker.scored_texts, q["id"]
        assert all(query == q["query"] for query, _ in reranker.calls), q["id"]
        sent_in_total += len(sent)
    assert sent_in_total > len(data["questions"])  # the check above was not vacuous


def test_the_reranker_scores_only_the_first_n_candidates():
    def base(query, rows):
        return [(3.0, ROWS[0]), (2.0, ROWS[1]), (1.0, ROWS[2])]

    reranker = RecordingReranker({"alpha": 0.1, "beta words": 0.9, "gamma": 5.0})
    ranked = RerankRanker(base, reranker, candidates=2)("beta", ROWS)
    assert reranker.calls == [("beta", ["alpha", "beta words"])]
    # c was never scored: it stays behind the two reranked rows although its score would have been the highest.
    assert [row["id"] for _, row in ranked] == ["b", "a", "c"]
    assert [score for score, _ in ranked] == [0.9, 0.1, 1.0]


def test_equal_rerank_scores_keep_the_base_order_whatever_order_the_rows_arrive_in():
    base = HybridRanker(VectorRanker(TableEmbedder(VECTORS), min_similarity=0.0), k=60)
    assert [row["id"] for _, row in base("beta", ROWS)] == ["b", "a", "c"]
    for rows in (ROWS, ROWS[::-1]):
        all_equal = RerankRanker(base, RecordingReranker(), candidates=30)("beta", rows)
        assert [row["id"] for _, row in all_equal] == ["b", "a", "c"]
        two_equal = RerankRanker(base, RecordingReranker({"gamma": 1.0}), candidates=30)("beta", rows)
        assert [row["id"] for _, row in two_equal] == ["c", "b", "a"]


def test_no_candidates_means_the_reranker_is_never_called():
    class Refusing:
        model_id = "test/refusing"

        def score(self, query, passages):
            raise AssertionError("the reranker must not be called without candidates")

    ranker = RerankRanker(lexical_ranker, Refusing(), candidates=30)
    assert ranker("zzz", ROWS) == []  # authorized rows, none of them a candidate
    assert ranker("beta", []) == []  # nothing authorized to rank
    assert ranker.scored_texts == []


SEALED_HYBRID = EVAL / "hybrid/runs/20261103T153335-real-hybrid-930f8d99/report.json"
SEALED_COSINES = EVAL / "hybrid/runs/20261103T153249-real-vector-f75c0fc6/similarities.jsonl"


class SealedVector:
    """The vector half of the sealed runs: the bge-m3 cosines recorded per question, floor as in VectorRanker."""

    def __init__(self, cosines, min_similarity):
        self.cosines = cosines
        self.min_similarity = min_similarity

    def __call__(self, query, rows):
        pairs = [(self.cosines[(str(row["id"]), str(row.get("version", "1")))], row) for row in rows]
        pairs.sort(key=lambda value: (-value[0], str(value[1]["id"]), str(value[1].get("version", "1"))))
        return [(score, row) for score, row in pairs if score >= self.min_similarity]


def test_without_a_reranker_results_match_the_sealed_hybrid_run_question_by_question(frozen):
    data, store, identities = frozen
    sealed = {row["id"]: row for row in json.loads(SEALED_HYBRID.read_text())["questions"]}
    cosines = {
        row["id"]: {(score["id"], score["version"]): score["cosine"] for score in row["scores"]}
        for row in map(json.loads, SEALED_COSINES.read_text().splitlines())
    }
    assert len(sealed) == len(data["questions"]) == 47
    for q in data["questions"]:
        hybrid = HybridRanker(SealedVector(cosines.get(q["id"], {}), min_similarity=0.50), k=60)
        reader = RankedKnowledgeService(store, RerankRanker(hybrid, None, candidates=30))
        expected = (sealed[q["id"]]["error"] or {}).get("code") or [
            (hit["id"], hit["version"], hit["source_hash"], hit["status"]) for hit in sealed[q["id"]]["hits"]
        ]
        assert outcome(reader, data, identities, q) == expected, q["id"]
