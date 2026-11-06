"""Cited answers over four real annual reports: hybrid -> answer, and hybrid -> answerability gate -> answer.

Settings, extra permission questions, scoring and the decision rule come from preregistered.json /
PREREGISTRATION.md, committed before the first real run. The runner refuses to start if the question set,
the sources list, a PDF, a prompt or the model settings differ from what was registered.

    python preparation/s4/real-reports/run_qa.py --reports ~/code/finreports            # real: local Ollama
    python preparation/s4/real-reports/run_qa.py --reports DIR --llm fake --embedder fake --out /tmp/x  # plumbing

What it does: ingests the four PDFs into one Store scope (role finance, classification 1) through
forgeflow.pdf_ingest, plus two restricted copies (one under a role the analyst lacks, one in another
project); asks the 40 frozen questions as a finance analyst and two extra questions as principals without
access; retrieval runs once per question and both modes answer from the same hits, so only the gate differs.
Every row the Store returns, every citation, every prompt sent to the model, every text sent to the
embedder and every answer is audited for content the asking principal may not read.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from array import array
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]

from forgeflow import answerability as ans  # noqa: E402
from forgeflow import cited_answer as ca  # noqa: E402
from forgeflow.contracts import ContextHit, DomainError, Principal, Scope  # noqa: E402
from forgeflow.evt_store import EVTBoundStore  # noqa: E402
from forgeflow.pdf_ingest import ingest_into_store, ingest_pdf, parse_source_line  # noqa: E402
from forgeflow.retrieval import HybridRanker, OllamaEmbedder, RankedKnowledgeService, VectorRanker  # noqa: E402

PREREG = json.loads((HERE / "preregistered.json").read_text())
MODES = ("hybrid", "hybrid+answerability")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- scoring (pure, tested)

_FULLWIDTH = str.maketrans("０１２３４５６７８９．", "0123456789.")
_TOKEN = re.compile(r"\d+(?:,\d{3}(?!\d))*(?:\.\d+)?")
_PURE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def numeric_tokens(text: str) -> list[str]:
    """Maximal numbers in the text, thousands separators removed: '1,303,141.15万元' -> ['1303141.15']."""
    return [m.group(0).replace(",", "") for m in _TOKEN.finditer((text or "").translate(_FULLWIDTH))]


def has_number(text: str) -> bool:
    """A figure in the answer: any numeric token except a bare four-digit year (2025, 2024)."""
    return any(not _YEAR.fullmatch(token) for token in numeric_tokens(text))


def _unit_norm(text: str) -> str:
    text = (text or "").translate(_FULLWIDTH)
    text = re.sub(r"\s+", "", text)
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text).casefold()


def accept_match(answer: str, accept: list[str]) -> str | None:
    """The first accept form found in the answer, or None.

    Pure-number forms ('121', '168,838,102,514.79'): equal, as decimal numbers, to a whole numeric token of
    the answer, so '121' does not match '1210' or '121.5'. Forms with a unit ('1688.38亿', '72.2 billion'):
    spaces and thousands separators removed and case folded on both sides, then a substring that does not
    start right after a digit or a decimal point, so '722亿' does not match '1722亿'.
    """
    tokens = {Decimal(t) for t in numeric_tokens(answer)}
    normalised = _unit_norm(answer)
    for form in accept:
        stripped = form.strip()
        if _PURE.fullmatch(stripped):
            if Decimal(stripped.replace(",", "")) in tokens:
                return form
            continue
        needle = _unit_norm(stripped)
        start = normalised.find(needle)
        while start != -1:
            if start == 0 or normalised[start - 1] not in "0123456789.":
                return form
            start = normalised.find(needle, start + 1)
    return None


def hit_place(hit: ContextHit) -> dict:
    cited = parse_source_line(hit.content) or {"file": None, "pages": []}
    return {"file": cited["file"], "pages": sorted({p["page"] for p in cited["pages"]})}


def on_gold_page(hit: ContextHit, doc: str, pages: list[int]) -> bool:
    place = hit_place(hit)
    return place["file"] == f"{doc}.pdf" and bool(set(place["pages"]) & set(pages))


def score_answer(q: dict, result: dict, hits: tuple[ContextHit, ...]) -> dict:
    """Correctness, figure in the answer and wrong-but-confident, by the registered rules."""
    error = result["status"] in {"ANSWER_ERROR", "GATE_ERROR"}
    found = bool(result["found"]) and not error
    text = result["answer"] if found else ""
    figure = found and has_number(text)
    by_id = {h.id: h for h in hits}
    if q["kind"] == "answerable":
        matched = accept_match(text, q["accept"]) if found else None
        gold_cited = found and any(c in by_id and on_gold_page(by_id[c], q["doc"], q["pages"])
                                   for c in result["citations"])
        return {"correct": bool(matched) and gold_cited, "accept_matched": matched, "gold_page_cited": gold_cited,
                "figure_in_answer": figure, "wrong_but_confident": figure and not matched,
                "right_figure_wrong_citation": bool(matched) and not gold_cited, "error": error}
    return {"correct": not found and not error, "figure_in_answer": figure, "wrong_but_confident": figure,
            "error": error}


# --------------------------------------------------------------------------- models and embeddings


class CachedBatchEmbedder:
    """Batches embedding calls and caches vectors on disk by sha256(model, model digest, text).

    Vectors are always returned from the cache (float64), so a cached and an uncached run rank the same.
    """

    def __init__(self, inner, cache_file: Path | None, batch_size: int, model_key: str):
        self.inner = inner
        self.model_id = inner.model_id
        self.batch_size = batch_size
        self.model_key = model_key
        self.db = sqlite3.connect(str(cache_file) if cache_file else ":memory:")
        self.db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        self.stats = {"texts_requested": 0, "texts_embedded": 0, "cache_hits": 0, "calls": 0, "call_ms": []}

    def _key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model_key}\0{text}".encode()).hexdigest()

    def _get(self, key: str) -> list[float] | None:
        row = self.db.execute("SELECT vector FROM vectors WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        values = array("d")
        values.frombytes(row[0])
        return values.tolist()

    def embed(self, texts):
        texts = list(texts)
        self.stats["texts_requested"] += len(texts)
        keys = [self._key(t) for t in texts]
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key not in missing and self._get(key) is None:
                missing[key] = text
        self.stats["cache_hits"] += len(texts) - len(missing)
        pending = list(missing.items())
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start:start + self.batch_size]
            began = time.perf_counter()
            vectors = self.inner.embed([text for _, text in chunk])
            self.stats["call_ms"].append(round((time.perf_counter() - began) * 1000, 1))
            self.stats["calls"] += 1
            self.stats["texts_embedded"] += len(chunk)
            self.db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                                [(key, array("d", vector).tobytes()) for (key, _), vector in zip(chunk, vectors,
                                                                                                strict=True)])
            self.db.commit()
        return [self._get(key) for key in keys]

    def summary(self) -> dict:
        ms = self.stats["call_ms"]
        return {k: v for k, v in self.stats.items() if k != "call_ms"} | {
            "batch_size": self.batch_size, "total_call_ms": round(sum(ms), 1),
            "median_call_ms": statistics.median(ms) if ms else None}


class FakeEmbedder:
    """Plumbing only: character-trigram hashing into 1024 dimensions. No semantics; numbers meaningless."""

    model_id = "fake/trigram-hash-1024"

    def embed(self, texts):
        out = []
        for text in texts:
            vector = [0.0] * 1024
            padded = f"  {text.casefold()}  "
            for i in range(len(padded) - 2):
                vector[int(hashlib.md5(padded[i:i + 3].encode()).hexdigest()[:8], 16) % 1024] += 1.0
            out.append(vector)
        return out


class FakeChatModel:
    """Plumbing only: the gate keeps passages sharing a 4+ character query word; the answer quotes the first
    number of the first passage and cites it. Numbers produced with it are meaningless."""

    model_id = "fake/plumbing"

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, messages, schema):
        user = messages[-1]["content"]
        self.calls.append({"ms": 0.0, "finish_reason": "stop", "usage": None, "reasoning_returned": False})
        if "verdicts" in schema["properties"]:
            query = user.split("\n", 1)[0].casefold()
            words = [w for w in re.findall(r"\w{4,}", query)]
            passages = re.split(r"\n(?=\[\d+\] )", user.split("Passages:\n", 1)[1])
            verdicts = []
            for passage in passages:
                number, text = passage[1:].split("] ", 1)
                keep = any(w in text.casefold() for w in words)
                verdicts.append({"passage": int(number), "answers": keep, "quote": text[:40] if keep else ""})
            return json.dumps({"verdicts": verdicts})
        first = user.split("Passages:\n", 1)[1]
        ident = first[1:first.index("]\n")]
        body = first.split("\n", 2)[2] if first.count("\n") >= 2 else ""
        number = next(iter(_TOKEN.findall(body)), "")
        return json.dumps({"answer": number, "citations": [ident], "found": bool(number)})


class RecordingChatModel:
    """Passes calls through and keeps every prompt, so the audit can read exactly what the model saw."""

    def __init__(self, inner):
        self.inner = inner
        self.model_id = inner.model_id
        self.prompts: list[list[dict]] = []

    @property
    def calls(self) -> list[dict]:
        return getattr(self.inner, "calls", [])

    def complete(self, messages, schema):
        self.prompts.append(messages)
        return self.inner.complete(messages, schema)


class AuditStore:
    """Transparent: records every row and citation the Store returns to a search, never filters or adds."""

    def __init__(self, store: EVTBoundStore):
        self.store = store
        self.rows: list[dict] = []
        self.citations: list[dict] = []

    def reset(self) -> None:
        self.rows, self.citations = [], []

    def document_rows(self, principal, scope):
        rows = self.store.document_rows(principal, scope)
        self.rows.extend(rows)
        return rows

    def memory_rows(self, principal, scope):  # pragma: no cover - not used by document search
        return self.store.memory_rows(principal, scope)

    def cite(self, principal, scope, kind, identifier):
        row = self.store.cite(principal, scope, kind, identifier)
        self.citations.append(row)
        return row


def row_authorized(row: dict, principal: Principal, scope: Scope) -> bool:
    try:
        actual = Scope.model_validate({f: row[f] for f in ("tenant_id", "workspace_id", "project_id")})
        return actual == scope and principal.permits(actual, set(row["roles"]), int(row["classification"]))
    except (KeyError, TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- setup


def scopes() -> dict[str, Scope]:
    return {name: Scope.model_validate(value) for name, value in PREREG["scopes"].items()}


def principals() -> dict[str, Principal]:
    named = scopes()
    return {p["subject_id"]: Principal(subject_id=p["subject_id"], kind=p["kind"], roles=frozenset(p["roles"]),
                                       scopes=tuple(named[s] for s in p["scopes"]), clearance=p["clearance"])
            for p in PREREG["principals"]}


def verify_inputs(reports: Path) -> dict:
    q, s = PREREG["questions"], PREREG["sources"]
    if sha256(PROJECT / q["path"]) != q["sha256"]:
        raise SystemExit("questions.json differs from the registered one")
    if sha256(PROJECT / s["path"]) != s["sha256"]:
        raise SystemExit("sources.json differs from the registered one")
    if ans.prompt_sha256() != PREREG["prompts"]["answerability"]["sha256"]:
        raise SystemExit("answerability prompt differs from the registered one")
    if ca.prompt_sha256() != PREREG["prompts"]["cited_answer"]["sha256"]:
        raise SystemExit("cited-answer prompt differs from the registered one")
    pdfs = {}
    for report in json.loads((PROJECT / s["path"]).read_text())["reports"]:
        path = reports / f"{report['key']}.pdf"
        if not path.is_file() or sha256(path) != report["sha256"]:
            raise SystemExit(f"{path}: missing or hash differs from sources.json")
        pdfs[report["key"]] = report["sha256"]
    return {"questions_sha256": q["sha256"], "sources_sha256": s["sha256"], "pdfs": pdfs}


def load_questions() -> list[dict]:
    data = json.loads((PROJECT / PREREG["questions"]["path"]).read_text())
    questions = data["questions"]
    kinds = [q["kind"] for q in questions]
    if len(questions) != 40 or kinds.count("answerable") != 28 or kinds.count("absent") != 12:
        raise SystemExit("question counts differ from the registered ones")
    return questions


def ingest(store: EVTBoundStore, reports: Path, scratch: Path) -> tuple[dict, dict]:
    named, admin = scopes(), principals()[PREREG["admin"]]
    corpus = PREREG["corpus"]
    log: dict[str, Any] = {"documents": [], "restricted_copies": []}
    languages = {}
    for report in json.loads((PROJECT / PREREG["sources"]["path"]).read_text())["reports"]:
        languages[report["key"]] = report["language"]
        began = time.perf_counter()
        result = ingest_pdf(reports / f"{report['key']}.pdf", scope=named[corpus["scope"]], roles=corpus["roles"],
                            classification=corpus["classification"], max_chunk_chars=corpus["max_chunk_chars"])
        parsed = time.perf_counter()
        ingest_into_store(store, admin, named[corpus["scope"]], result)
        log["documents"].append({
            "key": report["key"], "rows": len(result.rows), "tables": len(result.tables),
            "text_rows": sum(r["kind"] == "text" for r in result.rows), "needs_ocr_pages": result.needs_ocr,
            "warnings": len(result.warnings), "parse_ms": round((parsed - began) * 1000, 1),
            "store_ms": round((time.perf_counter() - parsed) * 1000, 1)})
    for copy in PREREG["permission_checks"]["restricted_copies"]:
        target = scratch / f"{copy['key']}.pdf"
        shutil.copyfile(reports / f"{copy['source']}.pdf", target)
        result = ingest_pdf(target, scope=named[copy["scope"]], roles=copy["roles"],
                            classification=copy["classification"], max_chunk_chars=corpus["max_chunk_chars"])
        ingest_into_store(store, admin, named[copy["scope"]], result)
        log["restricted_copies"].append({**copy, "rows": len(result.rows)})
    return log, languages


# --------------------------------------------------------------------------- one question


def _hit_view(hit: ContextHit) -> dict:
    body = hit.content.split("\n", 1)[1] if "\n" in hit.content else ""
    return {"id": hit.id, "version": hit.version, "status": hit.status, **hit_place(hit), "text": body[:600]}


def _markers(text: str) -> list[str]:
    return [m for m in PREREG["permission_checks"]["markers"] if m in text]


def ask(q: dict, principal: Principal, scope: Scope, service: RankedKnowledgeService, audit: AuditStore,
        gate: ans.AnswerabilityGate, answerer: ca.CitedAnswerer, recorder: RecordingChatModel,
        readable: set[tuple[str, str, str]]) -> dict:
    top_k = PREREG["retrieval"]["top_k"]
    audit.reset()
    began = time.perf_counter()
    error = None
    try:
        hits = tuple(service.search(principal, scope, q["query"], top_k))
    except DomainError as exc:
        hits, error = (), exc.code
    retrieval_ms = (time.perf_counter() - began) * 1000
    rows_seen, citations_seen = list(audit.rows), list(audit.citations)

    p0 = len(recorder.prompts)
    began = time.perf_counter()
    plain = answerer.answer(q["query"], hits)
    answer_a_ms = (time.perf_counter() - began) * 1000
    p1 = len(recorder.prompts)
    began = time.perf_counter()
    gated = gate.check(q["query"], hits)
    gate_ms = (time.perf_counter() - began) * 1000
    given_b: list[tuple[ContextHit, ...]] = [hits]  # what the model may have read in this mode
    if gated.status == "GATE_ERROR":
        after_gate = {"answer": "", "citations": [], "found": False, "status": "GATE_ERROR",
                      "rejected_citations": [], "model_called": False, "error": gated.error}
        answer_b_ms = 0.0
    else:
        began = time.perf_counter()
        after_gate = answerer.answer(q["query"], gated.hits)
        answer_b_ms = (time.perf_counter() - began) * 1000
        given_b.append(gated.hits)
    p2 = len(recorder.prompts)

    def leaks(prompts: list[list[dict]], given: list[tuple[ContextHit, ...]], result: dict) -> dict:
        texts = [m["content"] for messages in prompts for m in messages]
        passages = [h for group in given for h in group]
        return {
            "prompts": len(prompts),
            "prompts_with_restricted_marker": sum(bool(_markers(t)) for t in texts),
            "passages_given": len(passages),
            "passages_not_readable_by_caller": sum((h.id, h.source_hash, h.content) not in readable
                                                   for h in passages),
            "answer_with_restricted_marker": int(bool(_markers(result["answer"]))
                                                 or any(_markers(c) for c in result["citations"])),
            "citations_outside_hits": sum(c not in {h.id for h in passages} for c in result["citations"]),
        }

    out = {
        "id": q["id"], "kind": q["kind"], "lang": q["lang"], "doc": q.get("doc"), "query": q["query"],
        "principal": principal.subject_id, "scope": scope.project_id,
        "retrieval": {
            "ms": round(retrieval_ms, 1), "error": error, "hits": [_hit_view(h) for h in hits],
            "store_rows_seen": len(rows_seen),
            "store_rows_forbidden": sum(not row_authorized(r, principal, scope) for r in rows_seen),
            "store_rows_restricted": sum(bool(_markers(str(r.get("id")) + str(r.get("content", ""))))
                                         or r.get("project_id") != scope.project_id for r in rows_seen),
            "citations_seen": len(citations_seen),
            "citations_forbidden": sum(not row_authorized(r, principal, scope) for r in citations_seen),
            "hits_restricted": sum(bool(_markers(h.id + h.content)) for h in hits),
        },
        "modes": {
            "hybrid": {**plain, "ms": {"answer": round(answer_a_ms, 1)},
                       "audit": leaks(recorder.prompts[p0:p1], [hits], plain)},
            "hybrid+answerability": {
                **after_gate,
                "gate": {"status": gated.status, "kept": list(gated.kept), "error": gated.error,
                         "model_called": gated.model_called, "raw_output": gated.raw_output,
                         "verdicts": [vars(v) for v in gated.verdicts]},
                "ms": {"gate": round(gate_ms, 1), "answer": round(answer_b_ms, 1)},
                "audit": leaks(recorder.prompts[p1:p2], given_b, after_gate),
            },
        },
    }
    if q["kind"] == "answerable":
        out["gold"] = {"doc": q["doc"], "pages": q["pages"], "accept": q["accept"]}
        out["retrieval"]["gold_page_in_top5"] = any(on_gold_page(h, q["doc"], q["pages"]) for h in hits)
        out["modes"]["hybrid+answerability"]["gate"]["gold_page_kept"] = any(
            on_gold_page(h, q["doc"], q["pages"]) for h in gated.hits)
    for mode, given in (("hybrid", hits), ("hybrid+answerability", gated.hits)):
        scored = q["kind"] in {"answerable", "absent"}
        out["modes"][mode]["score"] = score_answer(q, out["modes"][mode], tuple(given)) if scored else None
    return out


# --------------------------------------------------------------------------- summaries


def rate(items: list[bool]) -> dict:
    return {"n": sum(items), "of": len(items), "rate": round(sum(items) / len(items), 4) if items else None}


def pct(values: list[float]) -> dict:
    ms = sorted(values)
    if not ms:
        return {"n": 0}
    return {"n": len(ms), "median_ms": round(statistics.median(ms), 1),
            "p95_ms": ms[max(0, (95 * len(ms) + 99) // 100 - 1)], "max_ms": ms[-1]}


def summarize(outcomes: list[dict], languages: dict[str, str]) -> dict:
    def cross(o: dict) -> bool:
        return languages[o["doc"]] != o["lang"]

    answerable = [o for o in outcomes if o["kind"] == "answerable"]
    absent = [o for o in outcomes if o["kind"] == "absent"]
    split = {"all": lambda o: True, "same_language": lambda o: not cross(o), "cross_language": cross}
    summary: dict[str, Any] = {
        "page_recall_at_5": {k: rate([o["retrieval"]["gold_page_in_top5"] for o in answerable if f(o)])
                             for k, f in split.items()},
        "retrieval_errors": sum(bool(o["retrieval"]["error"]) for o in outcomes),
        "modes": {},
    }
    for mode in MODES:
        res = [o["modes"][mode] for o in outcomes]
        wbc_ans = [o["id"] for o in answerable if o["modes"][mode]["score"]["wrong_but_confident"]]
        wbc_abs = [o["id"] for o in absent if o["modes"][mode]["score"]["wrong_but_confident"]]
        statuses: dict[str, int] = {}
        for r in res:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        body = {
            "answerable_correct": {k: rate([o["modes"][mode]["score"]["correct"] for o in answerable if f(o)])
                                   for k, f in split.items()},
            "absent_correct": {k: rate([o["modes"][mode]["score"]["correct"] for o in absent if f(o)])
                               for k, f in split.items()},
            "wrong_but_confident": {"answerable": len(wbc_ans), "absent": len(wbc_abs),
                                    "total": len(wbc_ans) + len(wbc_abs), "ids": wbc_ans + wbc_abs},
            "right_figure_wrong_citation": [o["id"] for o in answerable
                                            if o["modes"][mode]["score"]["right_figure_wrong_citation"]],
            "answered_found_true": sum(r["found"] for r in res),
            "status_counts": dict(sorted(statuses.items())),
            "rejected_citations": sum(len(r.get("rejected_citations") or []) for r in res),
            "missed_answerable": [o["id"] for o in answerable if not o["modes"][mode]["score"]["correct"]],
            "wrong_absent": [o["id"] for o in absent if not o["modes"][mode]["score"]["correct"]],
        }
        if mode == "hybrid+answerability":
            body["gold_page_kept_by_gate"] = rate([o["modes"][mode]["gate"]["gold_page_kept"] for o in answerable])
            body["gate_status_counts"] = {
                s: sum(o["modes"][mode]["gate"]["status"] == s for o in outcomes)
                for s in ("SUPPORTED", "NO_ANSWER_IN_KNOWLEDGE_BASE", "GATE_ERROR")}
            reasons: dict[str, int] = {}
            for o in outcomes:
                for v in o["modes"][mode]["gate"]["verdicts"]:
                    reasons[v["reason"]] = reasons.get(v["reason"], 0) + 1
            body["verdict_reasons"] = dict(sorted(reasons.items()))
        retrieval = [o["retrieval"]["ms"] for o in outcomes]
        if mode == "hybrid":
            parts = {"retrieval": retrieval, "answer": [o["modes"][mode]["ms"]["answer"] for o in outcomes]}
            total = [o["retrieval"]["ms"] + o["modes"][mode]["ms"]["answer"] for o in outcomes]
        else:
            parts = {"retrieval": retrieval, "gate": [o["modes"][mode]["ms"]["gate"] for o in outcomes],
                     "answer": [o["modes"][mode]["ms"]["answer"] for o in outcomes]}
            total = [o["retrieval"]["ms"] + o["modes"][mode]["ms"]["gate"] + o["modes"][mode]["ms"]["answer"]
                     for o in outcomes]
        body["latency"] = {k: pct(v) for k, v in parts.items()} | {"end_to_end": pct(total)}
        summary["modes"][mode] = body
    return summary


def permission_summary(main: list[dict], extra: list[dict], embedded: list[str]) -> dict:
    def count(key: str, where: str = "retrieval") -> int:
        return sum(o[where][key] for o in main + extra)

    per_mode = {}
    for mode in MODES:
        a = [o["modes"][mode]["audit"] for o in main + extra]
        per_mode[mode] = {k: sum(x[k] for x in a) for k in a[0]}
    extras = []
    for o in extra:
        modes = {m: {"found": o["modes"][m]["found"], "answer": o["modes"][m]["answer"],
                     "model_prompts": o["modes"][m]["audit"]["prompts"], "status": o["modes"][m]["status"]}
                 for m in MODES}
        passed = (o["retrieval"]["store_rows_seen"] == 0 and not o["retrieval"]["hits"]
                  and all(not v["found"] and not v["answer"] and v["model_prompts"] == 0 for v in modes.values()))
        extras.append({"id": o["id"], "check": o["check"], "principal": o["principal"], "scope": o["scope"],
                       "query": o["query"], "retrieval_error": o["retrieval"]["error"],
                       "store_rows_seen": o["retrieval"]["store_rows_seen"], "hits": len(o["retrieval"]["hits"]),
                       "modes": modes, "passed": passed})
    leak_total = (count("store_rows_forbidden") + count("store_rows_restricted") + count("citations_forbidden")
                  + count("hits_restricted") + sum(sum(v[k] for k in v if k not in {"prompts", "passages_given"})
                                                  for v in per_mode.values())
                  + sum(bool(_markers(t)) for t in embedded))
    return {
        "store_rows_seen": count("store_rows_seen"), "store_rows_forbidden": count("store_rows_forbidden"),
        "store_rows_from_restricted_copies": count("store_rows_restricted"),
        "citations_seen": count("citations_seen"), "citations_forbidden": count("citations_forbidden"),
        "hits_from_restricted_copies": count("hits_restricted"),
        "model_inputs": per_mode,
        "embedder_texts": len(embedded),
        "embedder_texts_with_restricted_marker": sum(bool(_markers(t)) for t in embedded),
        "extra_questions": extras,
        "leak_total": leak_total,
        "passed": leak_total == 0 and all(e["passed"] for e in extras),
    }


def decide(summary: dict, permissions: dict) -> dict:
    rule = PREREG["decision_rule"]
    a, b = summary["modes"]["hybrid"], summary["modes"]["hybrid+answerability"]
    checks = {
        "permissions_zero_leaks": permissions["passed"],
        "absent_gain": b["absent_correct"]["all"]["n"] - a["absent_correct"]["all"]["n"],
        "answerable_drop": a["answerable_correct"]["all"]["n"] - b["answerable_correct"]["all"]["n"],
    }
    passed = {
        "permissions_zero_leaks": checks["permissions_zero_leaks"],
        "absent_gain_at_least": checks["absent_gain"] >= rule["min_absent_gain_questions"],
        "answerable_drop_at_most": checks["answerable_drop"] <= rule["max_answerable_drop_questions"],
    }
    return {"rule": rule, "measured": checks, "passed": passed, "gate_stays_on": all(passed.values())}


# --------------------------------------------------------------------------- environment


def ollama_detail(base_url: str, names: list[str]) -> dict:
    import httpx

    with httpx.Client(base_url=base_url, trust_env=False, timeout=30) as client:
        version = client.get("/api/version").json().get("version")
        tags = client.get("/api/tags").json().get("models", [])
        shown = {}
        for name in names:
            body = client.post("/api/show", json={"model": name}).json()
            weights = re.findall(r"^FROM (.+)$", body.get("modelfile", ""), flags=re.M)
            shown[name] = {"parameters": body.get("parameters"), "details": body.get("details"),
                           "weights_blob": [Path(w).name for w in weights]}
        running = client.get("/api/ps").json().get("models", [])
    return {"ollama_version": version,
            "models": [{k: m.get(k) for k in ("name", "digest", "size", "details")} for m in tags
                       if m.get("name") in names or m.get("name") in {n + ":latest" for n in names}],
            "show": shown,
            "running": [{k: m.get(k) for k in ("name", "digest", "context_length", "size_vram")} for m in running]}


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def model_call_summary(calls: list[dict], num_ctx: int | None, max_tokens: int) -> dict:
    prompt_tokens = [c["usage"]["prompt_tokens"] for c in calls if c.get("usage")]
    return {
        **pct([c["ms"] for c in calls]),
        "not_stopped": sum(c["finish_reason"] != "stop" for c in calls),
        "reasoning_returned": sum(c["reasoning_returned"] for c in calls),
        "max_prompt_tokens": max(prompt_tokens) if prompt_tokens else None,
        "median_prompt_tokens": statistics.median(prompt_tokens) if prompt_tokens else None,
        "calls_over_context": sum(p + max_tokens > num_ctx for p in prompt_tokens) if num_ctx else None,
    }


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, required=True, help="folder with <key>.pdf (fetch_real_reports.sh)")
    parser.add_argument("--llm", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--embedder", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--cache", type=Path, help="embedding cache file (default: <reports>/.cache/embeddings.sqlite)")
    parser.add_argument("--limit", type=int, help="plumbing only: first N questions")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    args = parser.parse_args()
    reports = args.reports.expanduser().resolve()

    inputs = verify_inputs(reports)
    questions = load_questions()
    if args.limit:
        if args.llm == "ollama" and args.embedder == "ollama":
            raise SystemExit("--limit is for plumbing runs only")
        questions = questions[:args.limit]
    real = args.llm == "ollama" and args.embedder == "ollama"
    model_cfg, retrieval_cfg = PREREG["model"], PREREG["retrieval"]
    ollama = None
    if args.llm == "ollama" or args.embedder == "ollama":
        ollama = ollama_detail(args.ollama_url, [model_cfg["served_as"], model_cfg["weights"],
                                                 retrieval_cfg["embedder"]])
    if args.embedder == "ollama":
        digest = next((m["digest"] for m in ollama["models"]
                       if m["name"] in {retrieval_cfg["embedder"], retrieval_cfg["embedder"] + ":latest"}), None)
        if not digest:
            raise SystemExit("embedder model not found in ollama")
        inner = OllamaEmbedder(retrieval_cfg["embedder"], args.ollama_url, timeout=300.0)
        model_key = f"{inner.model_id}@{digest}"
    else:
        inner, model_key = FakeEmbedder(), FakeEmbedder.model_id
    cache_file = (args.cache or reports / ".cache" / "embeddings.sqlite") if args.embedder == "ollama" else None
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
    embedder = CachedBatchEmbedder(inner, cache_file, retrieval_cfg["embed_batch"], model_key)

    if args.llm == "ollama":
        shown = ollama["show"]
        served, base = shown[model_cfg["served_as"]], shown[model_cfg["weights"]]

        def weights_details(entry: dict) -> dict:
            details = entry["details"] or {}
            return {k: details.get(k) for k in ("format", "family", "parameter_size", "quantization_level")}

        if weights_details(served) != weights_details(base) or (
                served["weights_blob"] and base["weights_blob"] and served["weights_blob"] != base["weights_blob"]):
            raise SystemExit("served model does not use the registered qwen3:8b weights")
        if f"num_ctx {model_cfg['num_ctx']}" not in re.sub(r"\s+", " ", shown[model_cfg["served_as"]]["parameters"]
                                                            or ""):
            raise SystemExit("served model does not declare the registered num_ctx")
        chat = ans.OllamaChatModel(model_cfg["served_as"], args.ollama_url, timeout=600.0,
                                   max_tokens=model_cfg["max_tokens"])
        if chat.settings() != model_cfg["settings"]:
            raise SystemExit("model settings differ from the registered ones")
    else:
        chat = FakeChatModel()
    recorder = RecordingChatModel(chat)
    gate = ans.AnswerabilityGate(recorder)
    answerer = ca.CitedAnswerer(recorder)
    if args.llm == "ollama":
        # Load the model once on text unrelated to the corpus, then confirm the context it was loaded with.
        warm = (ContextHit(id="warm-1", version="1", content="The test bench is painted blue.",
                           source_hash="0" * 64, classification=0, status="CURRENT"),)
        gate.check("What colour is the test bench?", warm)
        answerer.answer("What colour is the test bench?", warm)
        ollama["after_warmup"] = ollama_detail(args.ollama_url, [model_cfg["served_as"]])["running"]
        loaded = [m for m in ollama["after_warmup"] if m["name"].split(":")[0] == model_cfg["served_as"]]
        if loaded and loaded[0].get("context_length") not in (None, model_cfg["num_ctx"]):
            raise SystemExit(f"model loaded with context {loaded[0]['context_length']}, not {model_cfg['num_ctx']}")
        chat.calls.clear()
        recorder.prompts.clear()
        gate.seen.clear()
        answerer.seen.clear()

    label = "real" if real else "fake"
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    output = args.out / (f"qa-{stamp}" if real else f"qa-{stamp}-{label}")
    output.mkdir(parents=True)
    named, people = scopes(), principals()
    analyst = people[PREREG["asker"]]
    with tempfile.TemporaryDirectory(prefix="s4-qa-") as scratch:
        store = EVTBoundStore(f"sqlite:///{Path(scratch) / 'store.sqlite'}")
        store.create_schema()
        try:
            began = time.perf_counter()
            ingest_log, languages = ingest(store, reports, Path(scratch))
            ingest_log["total_ms"] = round((time.perf_counter() - began) * 1000, 1)
            readable = {(r["id"], r["source_hash"], r["content"])
                        for r in store.document_rows(analyst, named[PREREG["corpus"]["scope"]])}
            audit = AuditStore(store)
            vector = VectorRanker(embedder, retrieval_cfg["min_similarity"])
            service = RankedKnowledgeService(audit, HybridRanker(vector, retrieval_cfg["rrf_k"]))
            began = time.perf_counter()  # embeds every row the analyst may read, in batches, once
            service.search(analyst, named[PREREG["corpus"]["scope"]], PREREG["retrieval"]["warmup_query"], 5)
            index_ms = round((time.perf_counter() - began) * 1000, 1)
            main_outcomes = []
            for q in questions:
                main_outcomes.append(ask(q, analyst, named[PREREG["corpus"]["scope"]], service, audit, gate,
                                         answerer, recorder, readable))
                print(f"[qa] {q['id']} retrieval {main_outcomes[-1]['retrieval']['ms']:.0f} ms "
                      f"gold-page={main_outcomes[-1]['retrieval'].get('gold_page_in_top5')} "
                      f"a={main_outcomes[-1]['modes']['hybrid']['status']} "
                      f"b={main_outcomes[-1]['modes']['hybrid+answerability']['status']}", flush=True)
            extra_outcomes = []
            for x in PREREG["permission_checks"]["extra_questions"]:
                o = ask({"id": x["id"], "kind": "permission", "lang": x["lang"], "query": x["query"]},
                        people[x["principal"]], named[x["scope"]], service, audit, gate, answerer, recorder, set())
                o["check"] = x["check"]
                extra_outcomes.append(o)
        finally:
            store.engine.dispose()
    summary = summarize(main_outcomes, languages)
    permissions = permission_summary(main_outcomes, extra_outcomes, vector.embedded_texts)
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "REAL" if real else "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS",
        "git_head": git_head(),
        "preregistered": PREREG,
        "prompt_sha256": {"answerability": ans.prompt_sha256(), "cited_answer": ca.prompt_sha256()},
        "code_sha256": {name: sha256(PROJECT / name) for name in [
            "src/forgeflow/cited_answer.py", "src/forgeflow/answerability.py", "src/forgeflow/retrieval.py",
            "src/forgeflow/pdf_ingest.py", "preparation/s4/real-reports/run_qa.py"]},
        "llm": chat.model_id, "llm_settings": chat.settings() if hasattr(chat, "settings") else None,
        "embedder": embedder.model_id, "embedder_cache_key": model_key,
        "ollama": ollama,
        "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                        "packages": {n: importlib.metadata.version(n)
                                     for n in ["pydantic", "sqlalchemy", "httpx", "pdfplumber"]}},
        "inputs_verified": inputs,
        "inputs_unchanged_after": verify_inputs(reports) == inputs,
        "questions_run": len(questions),
        "ingest": ingest_log,
        "index": {"ms": index_ms, "embedder": embedder.summary(), "texts_sent_to_embedder": len(vector.embedded_texts)},
        "model_calls": model_call_summary(chat.calls, model_cfg["num_ctx"] if args.llm == "ollama" else None,
                                          model_cfg["max_tokens"]),
        "summary": summary,
        "permissions": permissions,
        "decision": decide(summary, permissions) if len(questions) == 40 else None,
        "questions": main_outcomes,
        "extra_questions": extra_outcomes,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    brief = {"output": str(output), "status": report["status"], "decision": report["decision"],
             "page_recall_at_5": {k: f"{v['n']}/{v['of']}" for k, v in summary["page_recall_at_5"].items()},
             "permissions_passed": permissions["passed"], "leak_total": permissions["leak_total"],
             "model_calls": report["model_calls"], "index_ms": index_ms}
    for mode in MODES:
        s = summary["modes"][mode]
        brief[mode] = {
            "answerable_correct": {k: f"{v['n']}/{v['of']}" for k, v in s["answerable_correct"].items()},
            "absent_correct": f"{s['absent_correct']['all']['n']}/{s['absent_correct']['all']['of']}",
            "wrong_but_confident": s["wrong_but_confident"],
            "end_to_end": s["latency"]["end_to_end"]}
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
