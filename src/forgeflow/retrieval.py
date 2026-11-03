"""Lexical, vector and hybrid (RRF) ranking behind the same ACL and citation checks as KnowledgeService.

context.py belongs to the sealed S1 delivery and stays unchanged. RankedKnowledgeService reuses its
authorization helpers and repeats its citation step around a pluggable ranker; a parity test holds the
lexical ranker to the S1 results on the frozen S2 dataset, so only the ranking differs between modes.

Order of operations, same as S1: the store filters by role, classification and scope BEFORE rows
exist here; rows are checked again; only then are they ranked. Embeddings are therefore computed only
for content this principal may read, and an embedding provider never sees anything else.
"""

from __future__ import annotations

import ipaddress
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Protocol
from urllib.parse import urlparse

import httpx

from . import contracts
from .context import ContextStore, _active_rows, _expired, _score, _validate_query, _visible
from .contracts import ContextHit, DomainError, Principal, Scope, authorize

# A ranker orders authorized rows for a query: (score, row) pairs, best first, only rows it would return.
Ranker = Callable[[str, list[dict]], list[tuple[float, dict]]]


def _order(pairs: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    # Same tie-break as S1: score, then id, then version, so equal scores never reorder between runs.
    return sorted(pairs, key=lambda value: (-value[0], str(value[1]["id"]), str(value[1].get("version", "1"))))


def lexical_ranker(query: str, rows: list[dict]) -> list[tuple[float, dict]]:
    """The S1 scorer unchanged: token overlap with exact phrase and exact ID boosts."""
    return _order([(score, row) for row in rows if (score := _score(query, row)) > 0])


class Embedder(Protocol):
    model_id: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _loopback(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OllamaEmbedder:
    """Dense embeddings from a local Ollama server (POST /api/embed). Nothing leaves the machine."""

    def __init__(self, model: str = "bge-m3", base_url: str = "http://127.0.0.1:11434", timeout: float = 60.0):
        if not _loopback(base_url):
            raise ValueError("OllamaEmbedder only talks to a local server")
        self.model_id = f"ollama/{model}"
        self._model = model
        # Desktop proxies (Clash and the like) answer localhost with 502, so loopback skips proxy settings.
        self._client = httpx.Client(base_url=base_url, timeout=timeout, trust_env=False)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        response = self._client.post("/api/embed", json={"model": self._model, "input": list(texts)})
        response.raise_for_status()
        vectors = response.json()["embeddings"]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding count does not match input count")
        return vectors


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class VectorRanker:
    """Cosine similarity between the query and each authorized row, kept only above a fixed floor.

    Vectors are cached by (model, source_hash): content with the same hash has the same vector, and a
    cached vector is only ever compared for a row that is in the current caller's authorized set.
    """

    def __init__(self, embedder: Embedder, min_similarity: float):
        self.embedder = embedder
        self.min_similarity = min_similarity
        self._cache: dict[tuple[str, str], list[float]] = {}
        self.embedded_texts: list[str] = []  # what the provider was sent, for audits and tests

    def _vectors(self, rows: list[dict]) -> list[list[float]]:
        missing = [row for row in rows if (self.embedder.model_id, str(row["source_hash"])) not in self._cache]
        if missing:
            texts = [str(row["content"]) for row in missing]
            self.embedded_texts.extend(texts)
            for row, vector in zip(missing, self.embedder.embed(texts), strict=True):
                self._cache[(self.embedder.model_id, str(row["source_hash"]))] = vector
        return [self._cache[(self.embedder.model_id, str(row["source_hash"]))] for row in rows]

    def similarities(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        if not rows:
            return []
        self.embedded_texts.append(query)
        query_vector = self.embedder.embed([query])[0]
        return _order([(_cosine(query_vector, vector), row) for row, vector in zip(rows, self._vectors(rows), strict=True)])

    def __call__(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        return [(score, row) for score, row in self.similarities(query, rows) if score >= self.min_similarity]


class HybridRanker:
    """Reciprocal rank fusion of the lexical and vector rankings: score = sum of 1 / (k + rank).

    A row returned by either ranker can be returned; a row both agree on rises. k = 60 is the
    constant from Cormack, Clarke and Buettcher (SIGIR 2009), fixed before any run here.
    """

    def __init__(self, vector: VectorRanker, k: int = 60):
        self.vector = vector
        self.k = k

    def __call__(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        fused: dict[tuple[str, str, str], float] = defaultdict(float)
        by_key: dict[tuple[str, str, str], dict] = {}
        for ranking in (lexical_ranker(query, rows), self.vector(query, rows)):
            for rank, (_, row) in enumerate(ranking, start=1):
                key = (str(row["id"]), str(row.get("version", "1")), str(row["source_hash"]))
                fused[key] += 1 / (self.k + rank)
                by_key[key] = row
        return _order([(score, by_key[key]) for key, score in fused.items()])


def _cited_hits(
    store: ContextStore,
    principal: Principal,
    scope: Scope,
    top_k: int,
    kind: str,
    rows: list[dict],
    ranked: list[tuple[float, dict]],
) -> list[ContextHit]:
    """S1's citation and status step (context._hits after its ranking line), unchanged in substance."""
    now = contracts.utcnow()
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        subject = row.get("subject_key", row.get("subject", row["id"]))
        if not _expired(row.get("valid_until"), now):
            groups[str(subject)].append(row)
    conflicts = {
        key for key, values in groups.items() if len({str(row["content"]).strip() for row in values}) > 1
    }
    results: list[ContextHit] = []
    for _, row in ranked[:top_k]:
        citation = store.cite(principal, scope, kind, str(row.get("citation_id", row["id"])))
        authorize(principal, scope, classification=int(row["classification"]))
        if not _visible(principal, scope, citation):
            raise DomainError("NOT_FOUND", "Object unavailable", 404)
        if (
            str(citation.get("version", "1")) != str(row.get("version", "1"))
            or citation.get("source_hash") != row["source_hash"]
        ):
            raise DomainError("SOURCE_CHANGED", "Citation version changed during retrieval", 409)
        if citation.get("status") in {"DELETED", "RETRACTED", "SUPERSEDED"} or citation.get("deleted_at"):
            raise DomainError("SOURCE_CHANGED", "Citation validity changed during retrieval", 409)
        subject = str(row.get("subject_key", row.get("subject", row["id"])))
        if (
            row.get("status") == "EXPIRED"
            or citation.get("status") == "EXPIRED"
            or _expired(row.get("valid_until"), now)
            or _expired(citation.get("valid_until"), now)
        ):
            status = "EXPIRED"
        elif subject in conflicts:
            status = "CONFLICT"
        elif kind == "memory":
            status = "CONFIRMED" if row.get("status") == "CONFIRMED" else "CANDIDATE"
        else:
            status = "CURRENT"
        results.append(
            ContextHit(
                id=str(row["id"]),
                version=str(row.get("version", "1")),
                content=str(row["content"]),
                source_hash=str(row["source_hash"]),
                classification=int(row["classification"]),
                status=status,
            )
        )
    return results


class RankedKnowledgeService:
    """KnowledgeService with the ranking step swappable: lexical (S1), vector, or hybrid."""

    def __init__(self, store: ContextStore, ranker: Ranker = lexical_ranker):
        self.store = store
        self.ranker = ranker

    def search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> list[ContextHit]:
        authorize(principal, scope)
        _validate_query(query, top_k)
        rows = _active_rows(self.store.document_rows(principal, scope), principal, scope, "document")
        return _cited_hits(self.store, principal, scope, top_k, "document", rows, self.ranker(query, rows))
