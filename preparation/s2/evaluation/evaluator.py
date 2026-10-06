"""Evaluation only: imports unchanged S1 services, writes only this preparation root."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[2]
sys.path.insert(0, str(PROJECT / "src"))

from forgeflow.context import KnowledgeService  # noqa: E402 -- explicit read-only S1 source bootstrap
from forgeflow.contracts import DomainError, Principal, Scope  # noqa: E402
from forgeflow.store import Store  # noqa: E402

PROTOCOL_VERSION = "lexical-baseline-eval-v1"
S1_MANIFEST = PROJECT / "deliveries/2026-10-05/manifest.json"
SCORER_FILES = ["src/forgeflow/context.py", "src/forgeflow/store.py", "src/forgeflow/contracts.py"]
FREEZE_FILES = ["dataset.json", "protocol.md", "evaluator.py", "run_baseline.py", "write_dataset.py"]
REQUIRED_TAGS = {
    "english",
    "chinese",
    "synonym",
    "cross-language",
    "exact-id",
    "version",
    "conflict",
    "expiry",
    "unanswerable",
    "acl-negative",
    "acl-control",
    "acl-decoy",
    "tenant",
    "workspace",
    "project",
    "role",
    "classification",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_key(scope: str, id: str, version: str, source_hash: str) -> tuple[str, str, str, str]:
    return scope, id, version, source_hash


def ref_key(ref: dict) -> tuple[str, str, str, str]:
    return source_key(ref["scope"], ref["id"], ref["version"], ref["source_hash"])


def principals(data: dict) -> dict[str, Principal]:
    return {
        name: Principal(
            subject_id="eval-" + name,
            kind="agent",
            roles=frozenset(row["roles"]),
            scopes=(Scope.model_validate(data["scopes"][row["scope"]]),),
            clearance=row["clearance"],
        )
        for name, row in data["principals"].items()
    }


def validate_dataset(data: dict) -> dict:
    """Validate gold without querying the retriever or deriving relevance from scores."""
    if data.get("schema_version") != 1 or data.get("top_k") != 5 or data.get("repetitions") != 5:
        raise ValueError("Unsupported frozen protocol dimensions")
    if len(data["questions"]) < 30:
        raise ValueError("At least 30 questions required")
    scopes = {name: Scope.model_validate(row) for name, row in data["scopes"].items()}
    if len({scope.key() for scope in scopes.values()}) != len(scopes):
        raise ValueError("Scope aliases must uniquely identify full scopes")
    identities = principals(data)
    documents, current, acl_by_id = {}, {}, {}
    for row in data["documents"]:
        if (
            row["scope"] not in scopes
            or not re.fullmatch(r"[a-z0-9-]{1,64}", row["id"])
            or not row["version"]
            or not isinstance(row["content"], str)
            or not row["content"]
            or not row["roles"]
            or len(row["roles"]) != len(set(row["roles"]))
            or type(row["classification"]) is not int
            or not 0 <= row["classification"] <= 3
        ):
            raise ValueError("Invalid document metadata")
        content_hash = hashlib.sha256(row["content"].encode()).hexdigest()
        if row["source_hash"] != content_hash:
            raise ValueError("Document hash mismatch")
        identity = (row["scope"], row["id"], row["version"])
        if identity in documents:
            raise ValueError("Duplicate immutable version")
        logical = identity[:2]
        acl = (tuple(sorted(row["roles"])), row["classification"])
        if logical in acl_by_id and acl_by_id[logical] != acl:
            raise ValueError("Versioned ACL changes are outside this fixture")
        acl_by_id[logical] = acl
        if row["valid_until"] is not None:
            expiry = datetime.fromisoformat(row["valid_until"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.year > 2000:
                raise ValueError("Only long-expired or null validity allowed; no clock patch")
        documents[identity] = row
        current[logical] = row["version"]
    ids = [q["id"] for q in data["questions"]]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"Q\d{2}", qid) for qid in ids):
        raise ValueError("Question IDs must be unique")
    tags = set()
    questions = {q["id"]: q for q in data["questions"]}
    for q in data["questions"]:
        if q["principal"] not in identities or q["scope"] not in scopes or q["split"] != "heldout":
            raise ValueError("Question scope/principal/split invalid")
        if not isinstance(q["query"], str) or not 1 <= len(q["query"].strip()) <= 2000:
            raise ValueError("Question query invalid")
        tags.update(q["tags"])
        negative = "acl-negative" in q["tags"] or "unanswerable" in q["tags"]
        if negative == bool(q["relevant"]):
            raise ValueError("Positive/negative relevance partition invalid")
        if len(q["relevant"]) != len({ref_key(ref) for ref in q["relevant"]}):
            raise ValueError("Duplicate gold relevance")
        for ref in q["relevant"]:
            identity = (ref["scope"], ref["id"], ref["version"])
            if identity not in documents:
                raise ValueError("Gold references unknown source/version")
            row = documents[identity]
            if ref["scope"] != q["scope"] or current[identity[:2]] != ref["version"]:
                raise ValueError("Gold must reference the requested scope/current version")
            if row["source_hash"] != ref["source_hash"]:
                raise ValueError("Gold hash mismatch")
            if not identities[q["principal"]].permits(
                scopes[q["scope"]], set(row["roles"]), row["classification"]
            ):
                raise ValueError("Gold positive source is unauthorized")
            expected_status = "EXPIRED" if row["valid_until"] else "CURRENT"
            if ref["status"] != expected_status:
                raise ValueError("Gold source status incorrect")
        if q["conflict_required"]:
            if len(q["relevant"]) < 2 or any(ref["status"] != "CURRENT" for ref in q["relevant"]):
                raise ValueError("Conflict task needs two current direct sources")
        if q["expected_denial"] and identities[q["principal"]].permits(scopes[q["scope"]]):
            raise ValueError("Expected denial has an authorized scope")
        if "acl-negative" in q["tags"]:
            twin = questions.get(q["counterpart"])
            if (
                not twin
                or twin["query"] != q["query"]
                or twin["scope"] != q["scope"]
                or "acl-control" not in twin["tags"]
                or not twin["relevant"]
            ):
                raise ValueError("ACL negative requires an exact-query positive counterpart")
    if not REQUIRED_TAGS <= tags:
        raise ValueError("Required heldout slices missing")
    return data


def verify_s1() -> dict:
    manifest = json.loads(S1_MANIFEST.read_text())
    if any(row["path"].startswith("preparation/s2/evaluation/") for row in manifest["files"]):
        raise ValueError("Evaluation files must be outside the sealed S1 manifest")
    changed = [row["path"] for row in manifest["files"] if sha256(PROJECT / row["path"]) != row["sha256"]]
    if changed:
        raise ValueError("Sealed S1 content changed: " + ", ".join(changed))
    return {
        "manifest_sha256": sha256(S1_MANIFEST),
        "files_checked": len(manifest["files"]),
        "scorer_sha256": {name: sha256(PROJECT / name) for name in SCORER_FILES},
    }


def freeze_inputs() -> dict:
    data = validate_dataset(json.loads((ROOT / "dataset.json").read_text()))
    protected = verify_s1()
    freeze = {
        "protocol_version": PROTOCOL_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "dataset_id": data["id"],
        "questions": len(data["questions"]),
        "files": {name: sha256(ROOT / name) for name in FREEZE_FILES},
        "protected_s1": protected,
        "instruction": "Corpus/gold and evaluator are frozen before first heldout retrieval. Do not overwrite; revisions require a new explicitly versioned benchmark.",
    }
    path = ROOT / "freeze.json"
    if path.exists():
        return verify_freeze()
    with path.open("x") as target:
        target.write(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n")
    return freeze


def verify_freeze() -> dict:
    freeze = json.loads((ROOT / "freeze.json").read_text())
    if freeze["protocol_version"] != PROTOCOL_VERSION:
        raise ValueError("Frozen protocol version mismatch")
    for name, expected in freeze["files"].items():
        if sha256(ROOT / name) != expected:
            raise ValueError("Frozen benchmark changed: " + name)
    if verify_s1() != freeze["protected_s1"]:
        raise ValueError("Sealed S1 identity changed")
    return freeze


class RecordingStore:
    """Transparent trace of real authorized repository outputs, never inject metadata."""

    def __init__(self, store):
        self.store = store
        self.rows = []
        self.citations = []

    def reset(self):
        self.rows, self.citations = [], []

    def document_rows(self, principal, scope):
        rows = self.store.document_rows(principal, scope)
        self.rows.extend(rows)
        return rows

    def cite(self, principal, scope, kind, id):
        row = self.store.cite(principal, scope, kind, id)
        self.citations.append(row)
        return row


def row_authorized(row: dict, principal: Principal, scope: Scope) -> bool:
    try:
        actual = Scope.model_validate(
            {field: row[field] for field in ["tenant_id", "workspace_id", "project_id"]}
        )
        return actual == scope and principal.permits(actual, set(row["roles"]), int(row["classification"]))
    except (KeyError, TypeError, ValueError):
        return False


def retrieval_scores(retrieved: list[tuple], gold: set[tuple]) -> dict:
    if not gold:
        return {"recall_at_5": None, "reciprocal_rank_at_5": None, "relevant_found": 0, "relevant_total": 0}
    first = next((rank for rank, key in enumerate(retrieved[:5], 1) if key in gold), None)
    found = len(set(retrieved[:5]) & gold)
    return {
        "recall_at_5": found / len(gold),
        "reciprocal_rank_at_5": 1 / first if first else 0,
        "relevant_found": found,
        "relevant_total": len(gold),
    }


def origin_scope_name(data: dict, origin: dict) -> str:
    """Scope comes from actual repository citation metadata, never from the query."""
    identity = tuple(origin.get(field) for field in ["tenant_id", "workspace_id", "project_id"])
    return next(
        (
            name
            for name, scope in data["scopes"].items()
            if tuple(scope[field] for field in ["tenant_id", "workspace_id", "project_id"]) == identity
        ),
        "UNRESOLVED_SCOPE",
    )


def source_metadata_matches(hit, row: dict) -> bool:
    return (
        row.get("id") == hit.id
        and row.get("version") == hit.version
        and row.get("source_hash") == hit.source_hash
        and row.get("content") == hit.content
        and row.get("classification") == hit.classification
    )


def rate(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "median_ms": None, "p95_ms": None}
    ordered = sorted(values)
    # Nearest-rank p95, with a disclosed finite denominator.
    return {
        "n": len(values),
        "median_ms": statistics.median(values),
        "p95_ms": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)],
        "max_ms": max(values),
    }


def seed_store(data: dict, url: str) -> tuple[Store, dict]:
    store = Store(url)
    store.create_schema()
    scopes = tuple(Scope.model_validate(value) for value in data["scopes"].values())
    admin = Principal(
        subject_id="synthetic-fixture-admin",
        kind="user",
        roles=frozenset({"admin"}),
        scopes=scopes,
        clearance=3,
    )
    seeded = {}
    for row in data["documents"]:
        scope = Scope.model_validate(data["scopes"][row["scope"]])
        result = store.put_document(
            admin,
            scope,
            row["id"],
            row["version"],
            row["content"],
            set(row["roles"]),
            row["classification"],
            row["valid_until"],
        )
        seeded[source_key(row["scope"], row["id"], row["version"], row["source_hash"])] = result[
            "citation_id"
        ]
    return store, seeded


def summarize(results: list[dict]) -> dict:
    positives = [r for r in results if r["metrics"]["relevant_total"]]
    core = [r for r in positives if "acl-control" not in r["tags"]]
    negatives = [r for r in results if "acl-negative" in r["tags"]]
    adversarial = [r for r in results if "acl-negative" in r["tags"] or "acl-decoy" in r["tags"]]
    unanswerable = [r for r in results if "unanswerable" in r["tags"]]
    all_checks = [check for r in results for check in r["citation_checks"]]
    relevant_total = sum(r["metrics"]["relevant_total"] for r in positives)
    relevant_found = sum(r["metrics"]["relevant_found"] for r in positives)
    conflict = [r for r in results if r["conflict_required"]]
    controls = [r for r in positives if "acl-control" in r["tags"]]
    stats = {
        "question_count": len(results),
        "positive_questions": len(positives),
        "core_content_questions": len(core),
        "acl_control_questions": len(positives) - len(core),
        "paired_acl_controls_recovered": rate(
            sum(r["metrics"]["relevant_found"] == r["metrics"]["relevant_total"] for r in controls),
            len(controls),
        ),
        "macro_recall_at_5": statistics.mean(r["metrics"]["recall_at_5"] for r in positives),
        "macro_mrr_at_5": statistics.mean(r["metrics"]["reciprocal_rank_at_5"] for r in positives),
        "core_content_recall_at_5": statistics.mean(r["metrics"]["recall_at_5"] for r in core),
        "core_content_mrr_at_5": statistics.mean(r["metrics"]["reciprocal_rank_at_5"] for r in core),
        "micro_relevant_retrieval": rate(relevant_found, relevant_total),
        "citation_resolves": rate(sum(c["resolves"] for c in all_checks), len(all_checks)),
        "citation_exact_authorized": rate(sum(c["exact_authorized"] for c in all_checks), len(all_checks)),
        "returned_citation_current_usable": rate(
            sum(c["current_usable"] for c in all_checks), len(all_checks)
        ),
        "returned_expected_source_status_correct": rate(
            sum(c["status_correct"] for c in all_checks), len(all_checks)
        ),
        "expected_status_gold_coverage": rate(
            sum(r["gold_status_correct"] for r in positives), relevant_total
        ),
        "acl_negative_query_leak": rate(sum(r["leak"] for r in negatives), len(negatives)),
        "adversarial_exposure_query_leak": rate(sum(r["leak"] for r in adversarial), len(adversarial)),
        "all_query_leak": rate(sum(r["leak"] for r in results), len(results)),
        "forbidden_candidate_rows": sum(r["forbidden_candidate_rows"] for r in results),
        "forbidden_citation_rows": sum(r["forbidden_citation_rows"] for r in results),
        "forbidden_candidate_observation_rate": rate(
            sum(r["forbidden_candidate_rows"] for r in results),
            sum(r.get("candidate_row_observations", 0) for r in results),
        ),
        "forbidden_citation_observation_rate": rate(
            sum(r["forbidden_citation_rows"] for r in results),
            sum(r.get("citation_row_observations", 0) for r in results),
        ),
        "unknown_returned_sources": sum(r["unknown_returned_sources"] for r in results),
        "unanswerable_nonempty_context": rate(sum(bool(r["hits"]) for r in unanswerable), len(unanswerable)),
        "unanswerable_empty_context": rate(sum(not r["hits"] for r in unanswerable), len(unanswerable)),
        "required_conflict_flag_coverage": rate(
            sum(r["conflict_flag_correct"] for r in conflict), len(conflict)
        ),
        "latency_search_including_service_citation": latency_summary(
            [v for r in results for v in r["latency_ms"]]
        ),
        "stable_query_results": rate(sum(r["stable"] for r in results), len(results)),
        "denial_expected_and_observed": rate(
            sum(r["denial_correct"] for r in results if r["expected_denial"]),
            sum(r["expected_denial"] for r in results),
        ),
    }
    slices = {}
    for tag in sorted({tag for r in results for tag in r["tags"]}):
        group = [r for r in positives if tag in r["tags"]]
        if group:
            slices[tag] = {
                "questions": len(group),
                "macro_recall_at_5": statistics.mean(r["metrics"]["recall_at_5"] for r in group),
                "macro_mrr_at_5": statistics.mean(r["metrics"]["reciprocal_rank_at_5"] for r in group),
            }
    stats["retrieval_slices"] = slices
    stats["latency_slices"] = {
        "positive": latency_summary([v for r in positives for v in r["latency_ms"]]),
        "acl_negative": latency_summary([v for r in negatives for v in r["latency_ms"]]),
        "unanswerable": latency_summary([v for r in unanswerable for v in r["latency_ms"]]),
    }
    return stats


def evaluate_question(
    data: dict,
    q: dict,
    store: Store,
    seeded: dict,
    reader: KnowledgeService,
    trace: RecordingStore,
    identity: Principal,
) -> dict:
    scope = Scope.model_validate(data["scopes"][q["scope"]])
    trace.reset()
    measurements, signature, hits, error = [], None, [], None
    stable = True
    retained_trace = None
    forbidden_rows = forbidden_citations = candidate_observations = citation_observations = 0
    for iteration in range(data["repetitions"]):
        trace.reset()
        started = time.perf_counter_ns()
        try:
            current_hits = reader.search(identity, scope, q["query"], top_k=5)
            current_error = None
        except DomainError as exc:
            current_hits, current_error = [], {"code": exc.code, "status": exc.status}
        measurements.append((time.perf_counter_ns() - started) / 1e6)
        # Security traces cover every measured repetition, not just a convenient
        # first sample; retrieval/provenance scores retain the first result.
        forbidden_rows += sum(not row_authorized(row, identity, scope) for row in trace.rows)
        forbidden_citations += sum(not row_authorized(row, identity, scope) for row in trace.citations)
        candidate_observations += len(trace.rows)
        citation_observations += len(trace.citations)
        current_signature = ([h.model_dump(mode="json") for h in current_hits], current_error)
        if iteration == 0:
            signature, hits, error = current_signature, current_hits, current_error
            retained_trace = (list(trace.rows), list(trace.citations))
        else:
            stable = stable and current_signature == signature
    rows, citations = retained_trace
    all_source = {
        source_key(row["scope"], row["id"], row["version"], row["source_hash"]): row
        for row in data["documents"]
    }
    origins = [citations[index] if index < len(citations) else {} for index in range(len(hits))]
    retrieved = [
        source_key(origin_scope_name(data, origin), h.id, h.version, h.source_hash)
        for h, origin in zip(hits, origins, strict=True)
    ]
    gold = {ref_key(ref) for ref in q["relevant"]}
    checks, unknown = [], 0
    for hit, key, origin in zip(hits, retrieved, origins, strict=True):
        resolves, exact, usable, status_correct = False, False, False, False
        row = all_source.get(key)
        try:
            # Resolve the UUID actually used by the retriever, then additionally
            # match the frozen seeded identity. Expected-source resolution alone
            # would falsely bless a foreign same-id/version/hash/content twin.
            resolved = store.cite(identity, scope, "document", origin["citation_id"])
            resolves = True
            origin_candidates = [
                candidate
                for candidate in rows
                if candidate.get("citation_id") == origin["citation_id"]
                and origin_scope_name(data, candidate) == origin_scope_name(data, origin)
            ]
            exact = (
                row is not None
                and origin["citation_id"] == seeded[key]
                and row_authorized(origin, identity, scope)
                and source_metadata_matches(hit, origin)
                and any(
                    row_authorized(candidate, identity, scope) and source_metadata_matches(hit, candidate)
                    for candidate in origin_candidates
                )
                and row_authorized(resolved, identity, scope)
                and source_metadata_matches(hit, resolved)
                and hit.classification == row["classification"]
                and hashlib.sha256(hit.content.encode()).hexdigest() == hit.source_hash
            )
            expected = "EXPIRED" if row and row["valid_until"] else "CURRENT"
            status_correct = exact and hit.status == expected and resolved["status"] == expected
            usable = exact and hit.status == resolved["status"] == "CURRENT"
        except (DomainError, KeyError):
            pass
        unknown += row is None
        checks.append(
            {
                "id": hit.id,
                "version": hit.version,
                "resolves": resolves,
                "exact_authorized": exact,
                "current_usable": usable,
                "status_correct": status_correct,
            }
        )
    required_conflict = q["conflict_required"]
    conflict_flag_correct = (
        (
            gold <= set(retrieved)
            and all(hit.status == "CONFLICT" for hit, key in zip(hits, retrieved, strict=True) if key in gold)
        )
        if required_conflict
        else None
    )
    return {
        "id": q["id"],
        "query": q["query"],
        "principal": q["principal"],
        "scope": q["scope"],
        "tags": q["tags"],
        "expected_denial": q["expected_denial"],
        "error": error,
        "denial_correct": bool(error and error == {"code": "NOT_FOUND", "status": 404})
        if q["expected_denial"]
        else error is None,
        "gold": q["relevant"],
        "hits": [h.model_dump(mode="json") for h in hits],
        "metrics": retrieval_scores(retrieved, gold),
        "citation_checks": checks,
        "gold_status_correct": len(
            {
                key
                for hit, key in zip(hits, retrieved, strict=True)
                if key in gold
                and hit.status == next(ref["status"] for ref in q["relevant"] if ref_key(ref) == key)
            }
        ),
        "forbidden_candidate_rows": forbidden_rows,
        "forbidden_citation_rows": forbidden_citations,
        "candidate_row_observations": candidate_observations,
        "citation_row_observations": citation_observations,
        "unknown_returned_sources": unknown,
        "leak": bool(forbidden_rows or forbidden_citations),
        "candidate_count_observed_first_repetition": len(rows),
        "hit_origin_scopes": [origin_scope_name(data, origin) for origin in origins],
        "conflict_required": required_conflict,
        "conflict_flag_correct": conflict_flag_correct,
        "stable": stable,
        "latency_ms": measurements,
    }


def run_baseline(label: str = "lexical") -> tuple[Path, dict]:
    if not re.fullmatch(r"[a-z0-9-]{1,40}", label):
        raise ValueError("Run label invalid")
    freeze = verify_freeze()
    data = validate_dataset(json.loads((ROOT / "dataset.json").read_text()))
    output = (
        ROOT / "runs" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + label + "-" + uuid4().hex[:8])
    )
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="sqlite-", dir=output) as scratch:
        store, seeded = seed_store(data, "sqlite:///" + str(Path(scratch) / "evaluation.db"))
        trace = RecordingStore(store)
        reader = KnowledgeService(trace)
        identities = principals(data)
        # Warmup occurs only after the recorded hash freeze, with no tuning.
        for q in data["questions"]:
            try:
                reader.search(
                    identities[q["principal"]],
                    Scope.model_validate(data["scopes"][q["scope"]]),
                    q["query"],
                    5,
                )
            except DomainError:
                pass
        results = [
            evaluate_question(data, q, store, seeded, reader, trace, identities[q["principal"]])
            for q in data["questions"]
        ]
        store.engine.dispose()
    # A concurrent change must prevent publishing results against stale freeze.
    end_freeze = verify_freeze()
    summary = summarize(results)
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "REAL_LEXICAL_BASELINE_SYNTHETIC",
        "protocol_version": PROTOCOL_VERSION,
        "dataset_id": data["id"],
        "freeze_sha256": sha256(ROOT / "freeze.json"),
        "freeze": freeze,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "packages": {name: importlib.metadata.version(name) for name in ["pydantic", "sqlalchemy"]},
            "store": "ephemeral fresh SQLite; synthetic fixture principals; no secrets/network/Docker",
            "retriever": "unchanged S1 KnowledgeService; literal lexical ranking; no vectors/LLM",
        },
        "metrics": summary,
        "questions": results,
        "sealed_s1_unchanged_after": verify_s1() == freeze["protected_s1"],
        "benchmark_unchanged_after": end_freeze == freeze,
        "not_claimed": [
            "human-independent gold",
            "hardware benchmark",
            "semantic embeddings",
            "hybrid retrieval",
            "LLM answer quality/hallucination rate",
            "production scalability",
            "complete security proof",
        ],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (output / "questions.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results)
    )
    return output, report
