"""Four ways to answer the same annual-report questions: single answer, self-check, A (executor + verifier),
B (planner + A).

Arms, metrics and the adoption rule come from preregistered.json / PREREGISTRATION.md, committed before this
file. Corpus, ingestion, retrieval, model settings and scoring are the S4 registration's, used through
preparation/s4/real-reports/run_qa.py as they are. The runner refuses to start when a question set, the sources
list, a PDF, a prompt, the model or the held-out freeze differs from what is recorded.

    python preparation/s6/multi-agent/run_arms.py --reports ~/code/finreports                # dev, then held-out
    python preparation/s6/multi-agent/run_arms.py --reports ~/code/finreports --sets dev     # development set only
    python preparation/s6/multi-agent/run_arms.py --reports ~/code/finreports --resume DIR   # continue a run
    python preparation/s6/multi-agent/run_arms.py --reports DIR --llm fake --embedder fake --limit 4 --out /tmp/x
    python preparation/s6/multi-agent/run_arms.py --pin                                      # rewrite pins.json

Per question: retrieve once, then run the four arms on those hits through forgeflow.report_orchestration (B adds
its planned retrieval). A call with the same messages and schema goes to the model once per run and its output is
shared across arms (``CallMemo``); calls and time are still counted for each arm as if it ran alone. The call
records are on disk in the run folder, so an interrupted run continues with ``--resume`` without repeating calls.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
sys.path.insert(0, str(PROJECT / "preparation/s4/real-reports"))

import run_qa as s4  # noqa: E402 - the registered S4 runner: ingestion, retrieval audit helpers, scoring

from forgeflow import answerability as ans  # noqa: E402
from forgeflow import cited_answer as ca  # noqa: E402
from forgeflow import report_lookup  # noqa: E402
from forgeflow import report_orchestration as ro  # noqa: E402
from forgeflow.analysis_provider import ProviderInvalid  # noqa: E402
from forgeflow.contracts import ContextHit, DomainError  # noqa: E402
from forgeflow.evt_store import EVTBoundStore  # noqa: E402
from forgeflow.retrieval import (  # noqa: E402
    HybridRanker,
    OllamaEmbedder,
    RankedKnowledgeService,
    VectorRanker,
)

PREREG = json.loads((HERE / "preregistered.json").read_text())
PINS = HERE / "pins.json"
ARMS = tuple(arm["id"] for arm in PREREG["arms"])
ERROR_STATUSES = frozenset(PREREG["statuses"]["errors_count_as_wrong"])
STEPS = ("plan", "answer", "self_check", "verify", "retry", "verify_retry")
_YEAR = re.compile(r"(?:19|20)\d{2}")


# --------------------------------------------------------------------------- what must match before a run


def pins_now() -> dict:
    return {"orchestration_version": ro.ORCHESTRATION_VERSION, "orchestration_prompt_sha256": ro.prompt_sha256(),
            "cited_answer": {"version": ca.PROMPT_VERSION, "sha256": ca.prompt_sha256()}}


def verify_registration() -> dict:
    """The S4 registration this one builds on, and the prompts of every role, are the recorded ones."""
    base = PREREG["builds_on"]
    if s4.sha256(PROJECT / base["path"]) != base["sha256"]:
        raise SystemExit("the S4 preregistered.json differs from the one this registration builds on")
    if not PINS.is_file():
        raise SystemExit("pins.json is missing: run with --pin and commit it before a run")
    pinned = json.loads(PINS.read_text())
    current = pins_now()
    if {k: pinned.get(k) for k in current} != current:
        raise SystemExit("role prompts differ from pins.json: run with --pin and commit it before a run")
    if set(ARMS) != set(ro.MODES) or PREREG["precedence"] != list(ro.PRECEDENCE):
        raise SystemExit("arms or precedence in the code differ from the registered ones")
    if PREREG["model"]["served_as"] != ro.REQUIRED_MODEL or s4.PREREG["model"]["served_as"] != ro.REQUIRED_MODEL:
        raise SystemExit("the registered model tag is not the one the orchestration requires")
    return {"s4_preregistered_sha256": base["sha256"], "pins": current, "pins_sha256": s4.sha256(PINS)}


def _check_counts(name: str, questions: list[dict], languages: dict[str, str] | None = None) -> None:
    spec = PREREG["sets"][name]
    kinds = [q["kind"] for q in questions]
    if (len(questions), kinds.count("answerable"), kinds.count("absent")) != (spec["count"], spec["answerable"],
                                                                             spec["absent"]):
        raise SystemExit(f"{name}: question counts differ from the registered ones")
    if len({q["id"] for q in questions}) != len(questions):
        raise SystemExit(f"{name}: question ids are not unique")
    if languages and "answerable_cross_language" in spec:
        cross = sum(q["kind"] == "answerable" and languages[q["doc"]] != q["lang"] for q in questions)
        if cross != spec["answerable_cross_language"]:
            raise SystemExit(f"{name}: {cross} cross-language answerable questions, registered "
                             f"{spec['answerable_cross_language']}")


def load_dev() -> tuple[list[dict], dict]:
    spec = PREREG["sets"]["dev"]
    digest = s4.sha256(PROJECT / spec["path"])
    if digest != spec["sha256"]:
        raise SystemExit("the development questions differ from the frozen S4 set")
    questions = json.loads((PROJECT / spec["path"]).read_text())["questions"]
    _check_counts("dev", questions)
    return questions, {"path": spec["path"], "sha256": digest}


def load_heldout() -> tuple[list[dict], dict]:
    """The held-out set, only if it was frozen after every question was checked by a person."""
    spec = PREREG["sets"]["heldout"]
    path, freeze_path = PROJECT / spec["path"], PROJECT / spec["freeze"]
    if not path.is_file() or not freeze_path.is_file():
        raise SystemExit("the held-out set is not frozen yet: draft it with write_heldout.py, have every question "
                         "checked against the printed page, then freeze and commit it (see README.md)")
    freeze = json.loads(freeze_path.read_text())
    digest = s4.sha256(path)
    if freeze.get("files", {}).get(path.name) != digest:
        raise SystemExit("heldout.json differs from the hash in heldout-freeze.json")
    data = json.loads(path.read_text())
    questions = data["questions"]
    reports = json.loads((PROJECT / s4.PREREG["sources"]["path"]).read_text())["reports"]
    languages = {r["key"]: r["language"] for r in reports}
    for q in questions:
        if (q.get("review") or {}).get("status") != "verified":
            raise SystemExit(f"held-out {q['id']} was not checked by a person")
        if q["doc"] not in languages:
            raise SystemExit(f"held-out {q['id']} names a report that is not in sources.json")
        if report_lookup.for_question(q["query"]):
            raise SystemExit(f"held-out {q['id']} is answered by a report tool, not by the model")
    _check_counts("heldout", questions, languages)
    return questions, {"path": spec["path"], "sha256": digest, "set_id": data.get("id"),
                       "verified_by": freeze.get("verified_by"), "frozen_at": freeze.get("recorded_at")}


def earlier_real_heldout_runs(out: Path) -> list[str]:
    found = []
    for report in sorted(out.glob("*/report.json")):
        try:
            data = json.loads(report.read_text())
        except (OSError, ValueError):
            continue
        if data.get("status") == "REAL" and "heldout" in data.get("sets", {}):
            found.append(str(report))
    return found


# --------------------------------------------------------------------------- one call per distinct prompt


class CallMemo:
    """Sends each distinct (messages, schema) to the model once and keeps the reply on disk.

    Arms that send the same prompt get the same reply, so they differ only where their prompts differ. A reply the
    model gave but that is unusable (cut off, not text) is kept too: the same input would give it again. A call
    that never produced a reply (connection refused, timeout) is retried a few times and never kept.
    """

    RETRIES = 3

    def __init__(self, inner: Any, path: Path | None, model_key: str):
        self.inner, self.model_key = inner, model_key
        self.model_id = inner.model_id
        self.db = sqlite3.connect(str(path) if path else ":memory:")
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, raw TEXT, error TEXT, ms REAL "
                        "NOT NULL, finish_reason TEXT, usage TEXT, reasoning_returned INTEGER, recorded_at TEXT)")
        self.sent: list[dict] = []  # calls that reached the model in this process, in order
        self.this_run: set[str] = set()
        self.from_disk = 0
        self.transport_retries = 0

    def key(self, messages: list[dict], schema: dict) -> str:
        material = [self.model_key, messages, schema]
        return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def call(self, messages: list[dict], schema: dict) -> dict:
        """``{"raw" | "error", "ms", "usage", "shared", "from_disk"}``; raises only when no reply could be had."""
        key = self.key(messages, schema)
        row = self.db.execute("SELECT raw, error, ms, finish_reason, usage, reasoning_returned FROM calls "
                              "WHERE key = ?", (key,)).fetchone()
        if row is not None:
            from_disk = key not in self.this_run
            if from_disk:
                self.this_run.add(key)
                self.from_disk += 1
            return {"key": key, "raw": row[0], "error": row[1], "ms": row[2], "finish_reason": row[3],
                    "usage": json.loads(row[4]) if row[4] else None, "shared": not from_disk, "from_disk": from_disk}
        before = len(getattr(self.inner, "calls", []))
        raw = error = None
        for attempt in range(1, self.RETRIES + 1):
            started = time.perf_counter()
            try:
                raw = self.inner.complete(messages, schema)
                break
            except ProviderInvalid as exc:
                error = str(exc)
                break
            except httpx.TransportError:
                if attempt == self.RETRIES:
                    raise
                self.transport_retries += 1
                time.sleep(5)
        ms = round((time.perf_counter() - started) * 1000, 1)
        logged = getattr(self.inner, "calls", [])
        detail = logged[-1] if len(logged) > before else {}
        record = {"ms": detail.get("ms", ms), "finish_reason": detail.get("finish_reason"),
                  "usage": detail.get("usage"), "reasoning_returned": bool(detail.get("reasoning_returned"))}
        self.db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, raw, error, record["ms"], record["finish_reason"],
                         json.dumps(record["usage"]) if record["usage"] else None, int(record["reasoning_returned"]),
                         datetime.now(UTC).isoformat()))
        self.db.commit()
        self.this_run.add(key)
        self.sent.append(record)
        return {"key": key, "raw": raw, "error": error, **record, "shared": False, "from_disk": False}


class ArmModel:
    """What one arm sees as its model: the memo, plus this arm's own count of calls, time and prompts."""

    def __init__(self, memo: CallMemo):
        self.memo = memo
        self.model_id = memo.model_id
        self.calls: list[dict] = []
        self.prompts: list[list[dict]] = []

    def complete(self, messages: list[dict], schema: dict) -> str:
        self.prompts.append(messages)
        started = time.perf_counter()
        try:
            record = self.memo.call(messages, schema)
        except ro.MODEL_ERRORS:  # no reply at all: counted as a call of this arm, never kept or shared
            self.calls.append({"key": None, "ms": round((time.perf_counter() - started) * 1000, 1), "usage": None,
                               "shared": False, "from_disk": False})
            raise
        self.calls.append({k: record[k] for k in ("key", "ms", "usage", "shared", "from_disk")})
        if record["error"] is not None:
            raise ProviderInvalid(record["error"])
        return record["raw"]


class FakeRoles:
    """Plumbing only: deterministic stand-ins for every role. Numbers produced with it are meaningless.

    Executor: quotes the first number of the first passage (of the second passage once it is told that its answer
    was rejected). Verifier: passes when a word of the question is in the cited passage. Planner: names the first
    report in the question's language and searches for the question's words.
    """

    model_id = "fake/plumbing"

    def __init__(self):
        self.calls: list[dict] = []

    @staticmethod
    def _words(question: str) -> list[str]:
        latin = re.findall(r"[a-z][a-z0-9]{3,}", question.casefold())
        cjk = re.findall(r"[\u4e00-\u9fff]{2,}", question)
        return latin + [run[i:i + 2] for run in cjk for i in range(len(run) - 1)]

    def complete(self, messages, schema):
        user = messages[-1]["content"]
        self.calls.append({"ms": 0.0, "finish_reason": "stop", "usage": None, "reasoning_returned": False})
        question = user.split("\n", 1)[0].removeprefix("Question: ")
        words = self._words(question)
        if "search_query" in schema["properties"]:
            language = "zh" if re.search(r"[\u4e00-\u9fff]", question) else "en"
            listed = re.findall(r"^- key: (\S+) \|.*\| language: (\S+)$", user, flags=re.M)
            company = next((key for key, lang in listed if lang == language), "")
            return json.dumps({"company": company, "metric": question[:40] or "-", "period": "",
                               "search_query": " ".join(words)[:120] or question[:120]}, ensure_ascii=False)
        if "verdict" in schema["properties"]:
            passage = user.split("Cited passages:\n", 1)[1]
            body = (passage.split("\n", 2)[2] if passage.count("\n") >= 2 else "").strip()
            if len(body) >= 6 and any(word in body.casefold() for word in words):
                return json.dumps({"verdict": "pass", "failed": "none", "quote": body[:40].strip()}, ensure_ascii=False)
            return json.dumps({"verdict": "reject", "failed": "metric", "quote": ""})
        passages = user.split("Passages:\n", 1)[1]
        for marker in ("\n\nYour previous answer:", "\n\nA checker rejected"):
            passages = passages.split(marker, 1)[0]
        blocks = re.split(r"\n\n(?=\[[^\]\n]+\]\n\[source\])", passages)
        pick = 1 if "A checker rejected" in user and len(blocks) > 1 else 0
        ident = blocks[pick][1:blocks[pick].index("]\n")]
        body = blocks[pick].split("\n", 2)[2] if blocks[pick].count("\n") >= 2 else ""
        number = next(iter(s4._TOKEN.findall(body)), "")
        return json.dumps({"answer": number, "citations": [ident], "found": bool(number)})


# --------------------------------------------------------------------------- scoring and failure types (pure)


def score(q: dict, result: dict, hits: tuple[ContextHit, ...]) -> dict:
    """The S4 rules. ANSWER_ERROR and VERIFY_ERROR are wrong for both kinds; NEEDS_REVIEW is "no answer shown"."""
    as_scored = result | {"status": "ANSWER_ERROR"} if result["status"] in ERROR_STATUSES else result
    return s4.score_answer(q, as_scored, hits)


def flags(q: dict, result: dict, hits: tuple[ContextHit, ...]) -> dict:
    """Two diagnostics that never block an answer: a citation into another report, a figure no cited passage prints."""
    cited = [h for h in hits if h.id in result["citations"]]
    # The first line of a hit is its [source] line (file, page, box): those numbers are not report content.
    printed = {Decimal(t) for h in cited for t in s4.numeric_tokens(h.content.split("\n", 1)[-1])}
    figures = [t for t in s4.numeric_tokens(result["answer"]) if not _YEAR.fullmatch(t)]
    return {"cites_other_report": [h.id for h in cited if s4.hit_place(h)["file"] != f"{q['doc']}.pdf"],
            "figures_not_in_cited": [t for t in figures if Decimal(t) not in printed]}


def failure_type(q: dict, result: dict, scored: dict, gold_in_evidence: bool | None) -> str | None:
    """One label per question that was not answered correctly; None when it was."""
    if scored["correct"]:
        return None
    if result["status"] in ERROR_STATUSES:
        return f"error:{result['status']}"
    if q["kind"] == "absent":
        return "fabricated_figure" if scored["figure_in_answer"] else "answer_without_figure"
    where = "gold_retrieved" if gold_in_evidence else "gold_not_retrieved"
    if result["status"] == "NEEDS_REVIEW":
        return "needs_review"
    if not result["found"]:
        return f"said_not_found_{where}"
    if scored["right_figure_wrong_citation"]:
        return "right_figure_wrong_page"
    if not scored["figure_in_answer"]:
        return "answer_without_figure"
    return f"wrong_figure_{where}"


def decide(summary: dict, permissions_passed: bool) -> dict:
    """The registered adoption rule, on the held-out summary."""
    rule = PREREG["decision_rule"]
    arms = summary["arms"]

    def right(arm: str) -> int:
        return arms[arm]["answerable_correct"]["all"]["n"]

    def confident(arm: str) -> int:
        return arms[arm]["wrong_but_confident"]["total"]

    judged = {}
    for arm in rule["applies_to"]:
        measured = {"answerable_gain_over_single": right(arm) - right("single"),
                    "answerable_gain_over_self_check": right(arm) - right("self_check"),
                    "wrong_but_confident_increase_over_single": confident(arm) - confident("single"),
                    "permissions_zero_leaks": permissions_passed}
        passed = {
            "gain_over_single": measured["answerable_gain_over_single"] >= rule["min_answerable_gain_over_single"],
            "gain_over_self_check": (measured["answerable_gain_over_self_check"]
                                     >= rule["min_answerable_gain_over_self_check"]),
            "wrong_but_confident_not_up": (measured["wrong_but_confident_increase_over_single"]
                                           <= rule["max_wrong_but_confident_increase_over_single"]),
            "permissions_zero_leaks": permissions_passed,
        }
        judged[arm] = {"measured": measured, "passed": passed, "meets_rule": all(passed.values())}
    meeting = [arm for arm in rule["applies_to"] if judged[arm]["meets_rule"]]
    # Both meet the rule: more answerable questions correct wins; on a tie the first listed (verify, fewer calls).
    adopt = max(meeting, key=lambda arm: (right(arm), -rule["applies_to"].index(arm))) if meeting else None
    return {"rule": rule, "arms": judged, "adopt": adopt, "conclusion": f"采用 {adopt}" if adopt else "不采用"}


# --------------------------------------------------------------------------- one question


def _retrieval_audit(audit: s4.AuditStore, principal, scope, hits) -> dict:
    rows, citations = list(audit.rows), list(audit.citations)
    return {
        "store_rows_seen": len(rows),
        "store_rows_forbidden": sum(not s4.row_authorized(r, principal, scope) for r in rows),
        "store_rows_restricted": sum(bool(s4._markers(str(r.get("id")) + str(r.get("content", ""))))
                                     or r.get("project_id") != scope.project_id for r in rows),
        "citations_seen": len(citations),
        "citations_forbidden": sum(not s4.row_authorized(r, principal, scope) for r in citations),
        "hits_restricted": sum(bool(s4._markers(h.id + h.content)) for h in hits),
    }


LEAK_COUNTERS = ("store_rows_forbidden", "store_rows_restricted", "citations_forbidden", "hits_restricted",
                 "prompts_with_restricted_marker", "passages_not_readable_by_caller",
                 "answer_with_restricted_marker", "citations_outside_hits")


def ask(q: dict, principal, scope, service: RankedKnowledgeService, audit: s4.AuditStore, memo: CallMemo,
        catalogue: list[dict], readable: set[tuple[str, str, str]]) -> dict:
    top_k = s4.PREREG["retrieval"]["top_k"]
    audit.reset()
    began = time.perf_counter()
    error = None
    try:
        hits = tuple(service.search(principal, scope, q["query"], top_k))
    except DomainError as exc:
        hits, error = (), exc.code
    retrieval_ms = round((time.perf_counter() - began) * 1000, 1)
    answerable = q["kind"] == "answerable"
    out: dict[str, Any] = {
        "id": q["id"], "kind": q["kind"], "lang": q["lang"], "doc": q.get("doc"), "query": q["query"],
        "retrieval": {"ms": retrieval_ms, "error": error, "hits": [s4._hit_view(h) for h in hits],
                      **_retrieval_audit(audit, principal, scope, hits)},
        "arms": {},
    }
    if answerable:
        out["gold"] = {"doc": q["doc"], "pages": q["pages"], "accept": q["accept"]}
        out["retrieval"]["gold_page_in_top5"] = any(s4.on_gold_page(h, q["doc"], q["pages"]) for h in hits)

    for arm in ARMS:
        model = ArmModel(memo)
        planned: dict[str, Any] = {}

        def search(query: str, doc: str | None, planned=planned) -> list[ContextHit]:
            audit.reset()
            scoped = service if doc is None else RankedKnowledgeService(audit, ro.within(doc, service.ranker))
            found = scoped.search(principal, scope, query, top_k)
            planned["audit"] = _retrieval_audit(audit, principal, scope, found)
            return found

        outcome = ro.Orchestrator(model, catalogue=catalogue).run(
            arm, q["query"], hits, search=search if arm == "plan_verify" else None)
        result, used = outcome.result, outcome.hits
        trace = result["orchestration"]
        if len(model.calls) != trace["calls"]:
            raise RuntimeError(f"{q['id']} {arm}: {len(model.calls)} calls made, {trace['calls']} steps recorded")
        for step, call in zip(trace["steps"], model.calls, strict=True):
            step["ms"] = call["ms"]  # the duration of the call itself, also when its output was shared
            step["shared"], step["from_disk"] = call["shared"], call["from_disk"]
            step["prompt_tokens"] = (call["usage"] or {}).get("prompt_tokens")
        model_ms = trace["model_ms"] = round(sum(c["ms"] for c in model.calls), 1)
        planned_ms = (trace["plan"] or {}).get("search_ms", 0.0) if trace["plan"] else 0.0
        gold_in = any(s4.on_gold_page(h, q["doc"], q["pages"]) for h in used) if answerable else None
        scored = score(q, result, used)
        texts = [m["content"] for messages in model.prompts for m in messages]
        counters = {
            "prompts": len(model.prompts),
            "prompts_with_restricted_marker": sum(bool(s4._markers(t)) for t in texts),
            "passages_given": len(used),
            "passages_not_readable_by_caller": sum((h.id, h.source_hash, h.content) not in readable for h in used),
            "answer_with_restricted_marker": int(bool(s4._markers(result["answer"]))
                                                 or any(s4._markers(c) for c in result["citations"])),
            "citations_outside_hits": sum(c not in {h.id for h in used} for c in result["citations"]),
            **{k: v for k, v in (planned.get("audit") or {}).items() if k in LEAK_COUNTERS},
        }
        out["arms"][arm] = {
            **result,
            "calls": len(model.calls),
            "calls_by_step": {s: sum(x["step"] == s for x in trace["steps"]) for s in STEPS
                              if any(x["step"] == s for x in trace["steps"])},
            "ms": {"retrieval": retrieval_ms, "planned_retrieval": planned_ms, "model": model_ms,
                   "end_to_end": round(retrieval_ms + planned_ms + model_ms, 1)},
            "evidence": {"source": trace["evidence"], "hits": [h.id for h in used], "gold_page_in_top5": gold_in},
            "score": scored, "failure": failure_type(q, result, scored, gold_in), "flags": flags(q, result, used),
            "audit": counters,
        }
    return out


# --------------------------------------------------------------------------- summaries


def _numbers(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "total": round(sum(values), 1), "mean": round(statistics.fmean(values), 2),
            "median": statistics.median(values), "max": max(values)}


def summarize(outcomes: list[dict], languages: dict[str, str]) -> dict:
    def cross(o: dict) -> bool:
        return languages[o["doc"]] != o["lang"]

    answerable = [o for o in outcomes if o["kind"] == "answerable"]
    absent = [o for o in outcomes if o["kind"] == "absent"]
    split = {"all": lambda o: True, "same_language": lambda o: not cross(o), "cross_language": cross}
    summary: dict[str, Any] = {
        "questions": {"all": len(outcomes), "answerable": len(answerable), "absent": len(absent)},
        "page_recall_at_5": {k: s4.rate([o["retrieval"]["gold_page_in_top5"] for o in answerable if f(o)])
                             for k, f in split.items()},
        "retrieval_errors": sum(bool(o["retrieval"]["error"]) for o in outcomes),
        "arms": {},
    }
    for arm in ARMS:
        def got(o: dict, arm=arm) -> dict:
            return o["arms"][arm]

        statuses: dict[str, int] = {}
        failures: dict[str, dict[str, list[str]]] = {"answerable": {}, "absent": {}}
        for o in outcomes:
            statuses[got(o)["status"]] = statuses.get(got(o)["status"], 0) + 1
            if got(o)["failure"]:
                failures[o["kind"]].setdefault(got(o)["failure"], []).append(o["id"])
        steps = [s for o in outcomes for s in got(o)["orchestration"]["steps"]]
        wbc_ans = [o["id"] for o in answerable if got(o)["score"]["wrong_but_confident"]]
        wbc_abs = [o["id"] for o in absent if got(o)["score"]["wrong_but_confident"]]
        body: dict[str, Any] = {
            "answerable_correct": {k: s4.rate([got(o)["score"]["correct"] for o in answerable if f(o)])
                                   for k, f in split.items()},
            "absent_correct": {k: s4.rate([got(o)["score"]["correct"] for o in absent if f(o)])
                               for k, f in split.items()},
            "wrong_but_confident": {"answerable": len(wbc_ans), "absent": len(wbc_abs),
                                    "total": len(wbc_ans) + len(wbc_abs), "ids": wbc_ans + wbc_abs},
            "needs_review": {"answerable": [o["id"] for o in answerable if got(o)["status"] == "NEEDS_REVIEW"],
                             "absent": [o["id"] for o in absent if got(o)["status"] == "NEEDS_REVIEW"]},
            "answered_found_true": sum(bool(got(o)["found"]) for o in outcomes),
            "status_counts": dict(sorted(statuses.items())),
            "failure_types": {kind: dict(sorted(v.items())) for kind, v in failures.items()},
            "missed_answerable": [o["id"] for o in answerable if not got(o)["score"]["correct"]],
            "wrong_absent": [o["id"] for o in absent if not got(o)["score"]["correct"]],
            "right_figure_wrong_citation": [o["id"] for o in answerable
                                            if got(o)["score"]["right_figure_wrong_citation"]],
            "flags": {"cites_other_report": [o["id"] for o in outcomes if got(o)["flags"]["cites_other_report"]],
                      "figures_not_in_cited": [o["id"] for o in outcomes if got(o)["flags"]["figures_not_in_cited"]]},
            "calls": {**_numbers([float(got(o)["calls"]) for o in outcomes]),
                      "by_step": {s: sum(x["step"] == s for x in steps) for s in STEPS
                                  if any(x["step"] == s for x in steps)},
                      "shared_with_another_arm": sum(bool(x.get("shared")) for x in steps)},
            "latency": {"end_to_end": s4.pct([got(o)["ms"]["end_to_end"] for o in outcomes]),
                        "model": s4.pct([got(o)["ms"]["model"] for o in outcomes]),
                        "retrieval": s4.pct([got(o)["ms"]["retrieval"] for o in outcomes])},
        }
        checks = [s for s in steps if s["role"] == "verifier"]
        if checks:
            reasons: dict[str, int] = {}
            for s in checks:
                label = "ERROR" if s["outcome"] == "ERROR" else s["reason"] if s["outcome"] == "PASS" \
                    else f"{s['reason']}:{s['failed']}"
                reasons[label] = reasons.get(label, 0) + 1
            after: dict[str, int] = {}
            for o in outcomes:
                names = [s["step"] for s in got(o)["orchestration"]["steps"]]
                if "retry" in names:
                    after[got(o)["status"]] = after.get(got(o)["status"], 0) + 1
            body["verifier"] = {"checks": len(checks), "verdicts": dict(sorted(reasons.items())),
                                "final_status_after_a_veto": dict(sorted(after.items()))}
        plans = [got(o)["orchestration"]["plan"] for o in outcomes if got(o)["orchestration"]["plan"]]
        if plans:
            counts: dict[str, int] = {}
            for p in plans:
                label = p["status"] if p["status"] != "INVALID" else f"INVALID:{p['reason']}"
                counts[label] = counts.get(label, 0) + 1
            body["plan"] = {
                "status_counts": dict(sorted(counts.items())),
                "page_recall_at_5_in_evidence": s4.rate([got(o)["evidence"]["gold_page_in_top5"] for o in answerable]),
                "gold_page_gained": [o["id"] for o in answerable if got(o)["evidence"]["gold_page_in_top5"]
                                     and not o["retrieval"]["gold_page_in_top5"]],
                "gold_page_lost": [o["id"] for o in answerable if not got(o)["evidence"]["gold_page_in_top5"]
                                   and o["retrieval"]["gold_page_in_top5"]],
                "planned_retrieval": s4.pct([got(o)["ms"]["planned_retrieval"] for o in outcomes]),
            }
        summary["arms"][arm] = body
    return summary


def permission_summary(outcomes: list[dict], embedded: list[str]) -> dict:
    totals = {k: 0 for k in LEAK_COUNTERS}
    for o in outcomes:
        for k in LEAK_COUNTERS:
            totals[k] += o["retrieval"].get(k, 0) + sum(a["audit"].get(k, 0) for a in o["arms"].values())
    marked = sum(bool(s4._markers(t)) for t in embedded)
    leak_total = sum(totals.values()) + marked
    return {**totals, "store_rows_seen": sum(o["retrieval"]["store_rows_seen"] for o in outcomes),
            "model_prompts": sum(a["audit"]["prompts"] for o in outcomes for a in o["arms"].values()),
            "embedder_texts": len(embedded), "embedder_texts_with_restricted_marker": marked,
            "leak_total": leak_total, "passed": leak_total == 0}


# --------------------------------------------------------------------------- model and embedder


def open_embedder(args, ollama: dict | None, reports: Path):
    cfg = s4.PREREG["retrieval"]
    if args.embedder == "ollama":
        digest = next((m["digest"] for m in ollama["models"]
                       if m["name"] in {cfg["embedder"], cfg["embedder"] + ":latest"}), None)
        if not digest:
            raise SystemExit("embedder model not found in ollama")
        inner = OllamaEmbedder(cfg["embedder"], args.ollama_url, timeout=300.0)
        model_key = f"{inner.model_id}@{digest}"
        cache = args.cache or reports / ".cache" / "embeddings.sqlite"
        cache.parent.mkdir(parents=True, exist_ok=True)
    else:
        inner, model_key, cache = s4.FakeEmbedder(), s4.FakeEmbedder.model_id, None
    return s4.CachedBatchEmbedder(inner, cache, cfg["embed_batch"], model_key), model_key


def open_chat(args, ollama: dict | None):
    """The registered served model, checked the way the S4 runner checks it, loaded and confirmed at 16k context."""
    cfg = s4.PREREG["model"]
    if args.llm != "ollama":
        return FakeRoles(), FakeRoles.model_id
    shown = ollama["show"]
    served, base = shown[cfg["served_as"]], shown[cfg["weights"]]

    def weights(entry: dict) -> dict:
        details = entry["details"] or {}
        return {k: details.get(k) for k in ("format", "family", "parameter_size", "quantization_level")}

    if weights(served) != weights(base) or (served["weights_blob"] and base["weights_blob"]
                                            and served["weights_blob"] != base["weights_blob"]):
        raise SystemExit("served model does not use the registered qwen3:8b weights")
    if f"num_ctx {cfg['num_ctx']}" not in re.sub(r"\s+", " ", served["parameters"] or ""):
        raise SystemExit("served model does not declare the registered num_ctx")
    chat = ans.OllamaChatModel(cfg["served_as"], args.ollama_url, timeout=600.0, max_tokens=cfg["max_tokens"])
    if chat.settings() != cfg["settings"]:
        raise SystemExit("model settings differ from the registered ones")
    # Load the model once on text unrelated to the corpus, then confirm the context it was loaded with.
    warm = (ContextHit(id="warm-1", version="1", content="The test bench is painted blue.", source_hash="0" * 64,
                       classification=0, status="CURRENT"),)
    ca.CitedAnswerer(chat).answer("What colour is the test bench?", warm)
    ollama["after_warmup"] = s4.ollama_detail(args.ollama_url, [cfg["served_as"]])["running"]
    loaded = [m for m in ollama["after_warmup"] if m["name"].split(":")[0] == cfg["served_as"]]
    if loaded and loaded[0].get("context_length") not in (None, cfg["num_ctx"]):
        raise SystemExit(f"model loaded with context {loaded[0]['context_length']}, not {cfg['num_ctx']}")
    chat.calls.clear()
    digest = next((m["digest"] for m in ollama["models"]
                   if m["name"] in {cfg["served_as"], cfg["served_as"] + ":latest"}), "")
    return chat, f"{chat.model_id}@{digest}|{json.dumps(chat.settings(), sort_keys=True)}"


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, help="folder with <key>.pdf (scripts/fetch_real_reports.sh)")
    parser.add_argument("--sets", nargs="+", choices=["dev", "heldout"], default=["dev", "heldout"])
    parser.add_argument("--llm", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--embedder", choices=["ollama", "fake"], default="ollama")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--cache", type=Path, help="embedding cache file (default: <reports>/.cache/embeddings.sqlite)")
    parser.add_argument("--limit", type=int, help="plumbing only: first N questions of each set")
    parser.add_argument("--out", type=Path, default=HERE / "runs")
    parser.add_argument("--resume", type=Path, help="run folder of an interrupted run: its model calls are not repeated")
    parser.add_argument("--pin", action="store_true", help="write pins.json from the current role prompts and exit")
    args = parser.parse_args(argv)
    if args.pin:
        note = {"note": "Hashes of the role prompts in forgeflow.report_orchestration and of the cited-answer "
                        "prompt. They may change on the development set only; every change is committed, and the "
                        "last commit of this file before the held-out run is the one that counts."}
        PINS.write_text(json.dumps(pins_now() | note, ensure_ascii=False, indent=1) + "\n")
        print(PINS.read_text())
        return 0
    if args.reports is None:
        parser.error("--reports is required")
    reports = args.reports.expanduser().resolve()
    real = args.llm == "ollama" and args.embedder == "ollama" and not args.limit
    if args.limit and args.llm == "ollama" and args.embedder == "ollama":
        raise SystemExit("--limit is for plumbing runs only")

    registration = verify_registration()
    inputs = s4.verify_inputs(reports)
    sets = list(dict.fromkeys(args.sets))
    loaded = {name: (load_dev() if name == "dev" else load_heldout()) for name in sets}
    if real and "heldout" in sets and not args.resume:
        earlier = earlier_real_heldout_runs(args.out)
        if earlier:
            raise SystemExit("the held-out set was already run for real (it is run once): " + ", ".join(earlier))

    if args.resume:
        output = args.resume.expanduser().resolve()
        started = json.loads((output / "run.json").read_text())
        if (output / "report.json").exists():
            raise SystemExit(f"{output} already has a report: nothing to resume")
    else:
        label = "" if real else "-fake" if args.llm == "fake" else "-plumbing"
        output = args.out / f"arms-{datetime.now(UTC):%Y%m%dT%H%M%SZ}{label}"
        output.mkdir(parents=True)
        started = None
    stamp = {"sets": {name: meta for name, (_, meta) in loaded.items()}, "pins_sha256": registration["pins_sha256"],
             "llm": args.llm, "embedder": args.embedder, "limit": args.limit}
    if started is None:
        started = stamp | {"started_at": datetime.now(UTC).isoformat(), "git_head": s4.git_head()}
        (output / "run.json").write_text(json.dumps(started, ensure_ascii=False, indent=1) + "\n")
    elif {k: started.get(k) for k in stamp} != stamp:
        raise SystemExit("this run folder was started with other question sets, prompts or models: start a new run")

    model_cfg, retrieval_cfg = s4.PREREG["model"], s4.PREREG["retrieval"]
    ollama = None
    if args.llm == "ollama" or args.embedder == "ollama":
        ollama = s4.ollama_detail(args.ollama_url, [model_cfg["served_as"], model_cfg["weights"],
                                                    retrieval_cfg["embedder"]])
    embedder, embedder_key = open_embedder(args, ollama, reports)
    chat, chat_key = open_chat(args, ollama)
    memo = CallMemo(chat, output / "calls.sqlite", chat_key)

    source_reports = json.loads((PROJECT / s4.PREREG["sources"]["path"]).read_text())["reports"]
    catalogue = [{k: r[k] for k in ("key", "company", "document", "language")} for r in source_reports]
    named, people = s4.scopes(), s4.principals()
    analyst, scope = people[s4.PREREG["asker"]], named[s4.PREREG["corpus"]["scope"]]
    results: dict[str, list[dict]] = {}
    with tempfile.TemporaryDirectory(prefix="s6-arms-") as scratch:
        store = EVTBoundStore(f"sqlite:///{Path(scratch) / 'store.sqlite'}")
        store.create_schema()
        try:
            began = time.perf_counter()
            ingest_log, languages = s4.ingest(store, reports, Path(scratch))
            ingest_log["total_ms"] = round((time.perf_counter() - began) * 1000, 1)
            readable = {(r["id"], r["source_hash"], r["content"]) for r in store.document_rows(analyst, scope)}
            audit = s4.AuditStore(store)
            vector = VectorRanker(embedder, retrieval_cfg["min_similarity"])
            service = RankedKnowledgeService(audit, HybridRanker(vector, retrieval_cfg["rrf_k"]))
            began = time.perf_counter()  # embeds every row the analyst may read, in batches, once
            service.search(analyst, scope, retrieval_cfg["warmup_query"], 5)
            index_ms = round((time.perf_counter() - began) * 1000, 1)
            for name in sets:
                questions = loaded[name][0][:args.limit] if args.limit else loaded[name][0]
                results[name] = []
                for q in questions:
                    o = ask(q, analyst, scope, service, audit, memo, catalogue, readable)
                    results[name].append(o)
                    arms = " ".join(f"{arm}={o['arms'][arm]['status']}/{o['arms'][arm]['calls']}" for arm in ARMS)
                    print(f"[arms] {name} {q['id']} retrieval {o['retrieval']['ms']:.0f} ms "
                          f"gold-page={o['retrieval'].get('gold_page_in_top5')} {arms} "
                          f"(sent {len(memo.sent)}, from disk {memo.from_disk})", flush=True)
        finally:
            store.engine.dispose()

    permissions = permission_summary([o for outcomes in results.values() for o in outcomes], vector.embedded_texts)
    report_sets = {}
    for name in sets:
        summary = summarize(results[name], languages)
        full = len(results[name]) == PREREG["sets"][name]["count"]
        report_sets[name] = {
            "role": PREREG["sets"][name]["role"], "questions_file": loaded[name][1], "questions_run": len(results[name]),
            "summary": summary,
            "decision": decide(summary, permissions["passed"]) if name == "heldout" and full else None,
            "questions": results[name],
        }
    num_ctx = model_cfg["num_ctx"] if args.llm == "ollama" else None
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": "REAL" if real else "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS",
        "git_head": s4.git_head(), "git_head_at_start": started.get("git_head"), "started_at": started["started_at"],
        "resumed": bool(args.resume),
        "preregistered": PREREG, "registration": registration,
        "code_sha256": {name: s4.sha256(PROJECT / name) for name in [
            "src/forgeflow/report_orchestration.py", "src/forgeflow/cited_answer.py", "src/forgeflow/retrieval.py",
            "src/forgeflow/pdf_ingest.py", "preparation/s6/multi-agent/run_arms.py",
            "preparation/s4/real-reports/run_qa.py"]},
        "llm": chat.model_id, "llm_settings": chat.settings() if hasattr(chat, "settings") else None,
        "embedder": embedder.model_id, "embedder_cache_key": embedder_key, "ollama": ollama,
        "environment": {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
                        "packages": {n: importlib.metadata.version(n)
                                     for n in ["pydantic", "sqlalchemy", "httpx", "pdfplumber"]}},
        "inputs_verified": inputs, "inputs_unchanged_after": s4.verify_inputs(reports) == inputs,
        "ingest": ingest_log,
        "index": {"ms": index_ms, "embedder": embedder.summary(), "texts_sent_to_embedder": len(vector.embedded_texts)},
        "model_calls": {"sent_this_process": s4.model_call_summary(memo.sent, num_ctx, model_cfg["max_tokens"]),
                        "reused_from_disk": memo.from_disk, "transport_retries": memo.transport_retries,
                        "distinct_prompts": memo.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],
                        "total_ms_all_distinct": round(memo.db.execute("SELECT SUM(ms) FROM calls").fetchone()[0] or 0, 1)},
        "permissions": permissions,
        "sets": report_sets,
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
    brief: dict[str, Any] = {"output": str(output), "status": report["status"], "model_calls": report["model_calls"],
                             "permissions_passed": permissions["passed"], "leak_total": permissions["leak_total"]}
    for name in sets:
        s = report_sets[name]["summary"]
        brief[name] = {arm: {
            "answerable_correct": f"{s['arms'][arm]['answerable_correct']['all']['n']}/{s['arms'][arm]['answerable_correct']['all']['of']}",
            "absent_correct": f"{s['arms'][arm]['absent_correct']['all']['n']}/{s['arms'][arm]['absent_correct']['all']['of']}",
            "wrong_but_confident": s["arms"][arm]["wrong_but_confident"]["total"],
            "calls_per_question": s["arms"][arm]["calls"].get("mean"),
            "end_to_end_median_ms": s["arms"][arm]["latency"]["end_to_end"].get("median_ms")} for arm in ARMS}
        if report_sets[name]["decision"]:
            brief[name]["decision"] = report_sets[name]["decision"]["conclusion"]
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
