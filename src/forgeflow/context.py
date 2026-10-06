"""Scope-safe lexical context retrieval. Knowledge and Memory remain separate.

The repository is the first ACL boundary: its row methods MUST filter before
returning candidates. This service checks returned metadata again before ranking.
No embedding, vector retrieval or semantic contradiction detector is claimed.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Protocol

from .contracts import ContextHit, DomainError, Principal, Scope, authorize


class ContextStore(Protocol):
    def document_rows(self, p: Principal, scope: Scope) -> list[dict]: ...
    def memory_rows(self, p: Principal, scope: Scope) -> list[dict]: ...
    def cite(self, p: Principal, scope: Scope, kind: str, id: str) -> dict: ...
    def write_memory(
        self,
        p: Principal,
        scope: Scope,
        runid: str,
        subject: str,
        content: str,
        source_event_ids: list[str],
        status: str = "CANDIDATE",
        classification: int = 0,
    ) -> dict: ...


def _expired(value: str | datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    try:
        instant = (
            value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        # Naive/invalid dates are not evidence of current validity.
        return instant.tzinfo is None or instant <= now
    except (TypeError, ValueError):
        return True


def _validate_query(query: str, top_k: int) -> None:
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise DomainError("INVALID_QUERY", "Query must contain 1–2000 characters", 422)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise DomainError("INVALID_TOP_K", "top_k must be between 1 and 20", 422)


def _visible(principal: Principal, scope: Scope, row: dict) -> bool:
    # A scope field, when returned, must never contradict the requested scope.
    for field, value in zip(("tenant_id", "workspace_id", "project_id"), scope.key(), strict=True):
        if field in row and row[field] != value:
            return False
    try:
        classification = int(row["classification"])
    except (KeyError, TypeError, ValueError):
        return False
    if not 0 <= classification <= 3:
        return False
    roles = set(row["roles"]) if "roles" in row else None
    return principal.permits(scope, roles, classification)


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())


def _score(query: str, row: dict) -> float:
    """Deterministic lexical overlap with exact phrase/ID boosts, not semantics."""
    tokens = set(_tokens(query))
    counts = Counter(_tokens(str(row["content"])))
    overlap = sum(min(counts[token], 3) for token in tokens)
    exact_phrase = 2 if query.casefold() in str(row["content"]).casefold() else 0
    exact_id = 4 if query.casefold() == str(row["id"]).casefold() else 0
    return overlap + exact_phrase + exact_id


def _active_rows(rows: list[dict], principal: Principal, scope: Scope, kind: str) -> list[dict]:
    authorized = [dict(row) for row in rows if _visible(principal, scope, row)]
    # Tombstones cannot provide new retrieval evidence. Superseding tombstones
    # still remove the superseded version rather than resurrecting old content.
    replaced: set[tuple[str, str]] = set()
    replaced_ids: set[str] = set()
    for row in authorized:
        supersedes = row.get("supersedes")
        if isinstance(supersedes, dict) and "id" in supersedes and "version" in supersedes:
            replaced.add((str(supersedes["id"]), str(supersedes["version"])))
        elif isinstance(supersedes, str) and supersedes:
            # Document stores may use a previous version string for the same ID;
            # memory stores usually use an immutable prior memory ID.
            if kind == "document":
                replaced.add((str(row["id"]), supersedes))
            else:
                replaced_ids.add(supersedes)
    return [
        row
        for row in authorized
        if str(row.get("status", "CURRENT")) not in {"DELETED", "RETRACTED", "SUPERSEDED"}
        and not row.get("deleted_at")
        and (str(row["id"]), str(row.get("version", "1"))) not in replaced
        and str(row["id"]) not in replaced_ids
    ]


def _hits(
    store: ContextStore,
    principal: Principal,
    scope: Scope,
    query: str,
    top_k: int,
    kind: str,
    rows: list[dict],
) -> list[ContextHit]:
    now = datetime.now(UTC)
    rows = _active_rows(rows, principal, scope, kind)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        # Contradiction grouping is explicit metadata, not a model guess.
        subject = row.get("subject_key", row.get("subject", row["id"]))
        if not _expired(row.get("valid_until"), now):
            groups[str(subject)].append(row)
    conflicts = {
        key for key, values in groups.items() if len({str(row["content"]).strip() for row in values}) > 1
    }
    ranked = sorted(
        ((score, row) for row in rows if (score := _score(query, row)) > 0),
        key=lambda value: (
            -value[0],
            str(value[1]["id"]),
            str(value[1].get("version", "1")),
        ),
    )
    results: list[ContextHit] = []
    for _, row in ranked[:top_k]:
        # Exact citation is reauthorized; changed evidence fails closed rather
        # than quietly substituting a mutable "latest" source.
        citation = store.cite(principal, scope, kind, str(row.get("citation_id", row["id"])))
        authorize(principal, scope, classification=int(row["classification"]))
        if not _visible(principal, scope, citation):
            raise DomainError("NOT_FOUND", "Object unavailable", 404)
        if (
            str(citation.get("version", "1")) != str(row.get("version", "1"))
            or citation.get("source_hash") != row["source_hash"]
        ):
            raise DomainError("SOURCE_CHANGED", "Citation version changed during retrieval", 409)
        if citation.get("status") in {
            "DELETED",
            "RETRACTED",
            "SUPERSEDED",
        } or citation.get("deleted_at"):
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


class KnowledgeService:
    def __init__(self, store: ContextStore):
        self.store = store

    def search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> list[ContextHit]:
        authorize(principal, scope)
        _validate_query(query, top_k)
        # The repository query is responsible for ACL BEFORE these rows exist.
        return _hits(
            self.store,
            principal,
            scope,
            query,
            top_k,
            "document",
            self.store.document_rows(principal, scope),
        )


class MemoryService:
    WRITE_ROLES = frozenset({"memory_writer"})

    def __init__(self, store: ContextStore):
        self.store = store

    def search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> list[ContextHit]:
        authorize(principal, scope)
        _validate_query(query, top_k)
        return _hits(
            self.store,
            principal,
            scope,
            query,
            top_k,
            "memory",
            self.store.memory_rows(principal, scope),
        )

    def propose(
        self,
        principal: Principal,
        scope: Scope,
        runid: str,
        subject: str,
        content: str,
        source_event_ids: list[str],
        classification: int = 0,
    ) -> dict:
        authorize(principal, scope, self.WRITE_ROLES, classification)
        if principal.kind != "service":
            raise DomainError(
                "MEMORY_SERVICE_REQUIRED",
                "Memory writes require a trusted control-plane service",
                403,
            )
        if (
            not runid
            or not subject.strip()
            or len(subject) > 200
            or not content.strip()
            or len(content) > 8000
        ):
            raise DomainError("INVALID_MEMORY", "Memory requires a bounded subject and content", 422)
        if (
            not source_event_ids
            or len(source_event_ids) > 20
            or any(not isinstance(value, str) or not value for value in source_event_ids)
        ):
            raise DomainError("MISSING_PROVENANCE", "Memory requires 1–20 source events", 422)
        # Repository validates every event belongs to this run/full scope,
        # inherits source classification, and atomically deduplicates the write.
        # Candidate creation cannot promote facts or perform confirmed supersession.
        return self.store.write_memory(
            principal,
            scope,
            runid,
            subject.strip(),
            content.strip(),
            sorted(set(source_event_ids)),
            status="CANDIDATE",
            classification=classification,
        )
