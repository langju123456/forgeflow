"""Roles around the cited answer: one self-check, a verifier after the answer, or a planner before retrieval.

Four ways to answer one annual-report question. All of them run after retrieval, so every role only ever sees
hits the S1 ACL and citation chain already returned for this caller (or a part of them), and none can add evidence.

    single       the executor answers once: forgeflow.cited_answer, unchanged
    self_check   the executor looks at its own answer once more (same role, same passages): keep, revise or withdraw
    verify       A  a verifier sees the question, the answer and the cited passages only, and may only veto.
                    Pass stops. A veto sends the executor back once with the kind of objection; the new answer is
                    verified again; a second veto ends as NEEDS_REVIEW and no answer is shown
    plan_verify  B  a planner sees the question and the report list (never the corpus), names the report and
                    rewrites the search query; retrieval runs inside that report with the rewritten query; an
                    unusable plan or a planned retrieval without hits falls back to the original retrieval.
                    Then exactly ``verify``

The order of steps is fixed in code. No role decides who acts next.

What each role is handed, and what it may return (``ROLES``):

    planner    question, report list                        -> company, metric, period, search_query
    executor   question, hits (+ on a retry: its vetoed     -> answer, citations, found
               answer, the ids it cited, the kind of veto)
    verifier   question, answer, the cited passages         -> verdict, failed check, quote

Who wins a disagreement (``PRECEDENCE``): code checks > verifier > executor.

* The executor's output goes through CitedAnswerer's checks first (strict JSON, citations only from the given
  hits, an answer needs text and one valid citation). What fails them is never shown and never reaches the verifier.
* The verifier's output goes through code as well. It passes only when it says pass, names no failed check and
  quotes a span that is literally in a cited passage; a "pass" whose quote is not there is a veto. Output that
  is not the expected JSON is VERIFY_ERROR: no answer shown, never a silent pass.
* A veto stands even if the executor repeats its answer: the same answer again ends as NEEDS_REVIEW.
* The planner has no say over the answer. Its plan is checked by code and dropped when unusable.

Recovery is the caller's existing one: a redelivered desk-service job runs the whole orchestration again from
the top (no per-step checkpoint), and the cache key carries ``cache_tag`` so the modes never share an entry.

The flow is a generator (``flow``) that yields what it needs next, a model call or a search, and is sent the
reply. ``Orchestrator`` drives it with a blocking model (ReportDesk.ask, the evaluation runner); the desk-service
worker drives the same generator with its streaming model. Answers produced here are model output: they never
enter the research approval flow, which accepts only the two deterministic report tools.

Every role must run on ``REQUIRED_MODEL``: the plain ``qwen3:8b`` tag loads with a 4096-token context and
silently drops the start of a longer prompt, system instruction included.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from . import cited_answer
from .analysis_provider import ProviderInvalid, strict_json
from .answerability import MIN_QUOTE_CHARS, ChatModel, OllamaChatModel, _strip_think
from .cited_answer import ANSWER_SCHEMA, CitedAnswerer
from .contracts import ContextHit, DomainError
from .retrieval import _loopback

ORCHESTRATION_VERSION = "report-orchestration-v1"
MODES = ("single", "self_check", "verify", "plan_verify")
SERVICE_MODES = MODES  # the worker runs plan_verify's planned search as a second gRPC Search (doc + search_query)
REQUIRED_MODEL = "forgeflow-qwen3-8b-ctx16k"
TRUNCATING_MODELS = frozenset({"qwen3:8b"})
PRECEDENCE = ("code", "verifier", "executor")
MAX_PLAN_QUERY_CHARS = 200
# What a driver may hand back in place of a model reply; the same failures CitedAnswerer turns into ANSWER_ERROR.
MODEL_ERRORS = (ProviderInvalid, httpx.HTTPError, KeyError, IndexError, TypeError, ValueError)


@dataclass(frozen=True)
class Role:
    name: str
    sees: tuple[str, ...]
    returns: tuple[str, ...]
    may: str


ROLES = {
    "planner": Role("planner", ("question", "report list: key, company, document, language"),
                    ("company", "metric", "period", "search_query"),
                    "narrow retrieval to one report and reword the query; never answers"),
    "executor": Role("executor", ("question", "retrieved hits",
                                  "on a retry: its vetoed answer, the ids it cited, the kind of veto"),
                     ("answer", "citations", "found"), "answer from the hits or say not found"),
    "verifier": Role("verifier", ("question", "answer", "the cited passages"), ("verdict", "failed", "quote"),
                     "veto only: cannot change the answer or add a citation"),
}

# The one check list. The self-check and the verifier get the same words, so those two modes differ only in who
# checks, what the checker sees and whether it may rewrite.
CHECKS = (
    "1. company: the passage is from the report of the company the question asks about. Each passage starts "
    "with a [source] line naming its file; a passage from another company's report does not count.\n"
    "2. metric: the passage states the metric the question asks for under its own label. A neighbouring row, "
    "a related metric, or a number whose row or column label is missing does not count.\n"
    "3. period: the figure is for the period the question asks about, not another year and not a single "
    "quarter.\n"
    "4. figure: every figure in the answer is printed in the passage for that company, metric and period. "
    "A figure that was computed, converted or estimated does not count."
)

VERIFIER_SYSTEM = (
    "You check an answer that someone else wrote. You see only the question, the answer and the passages the "
    "answer cites. Judge from these passages alone, never from outside knowledge, and do not answer the "
    "question yourself.\n"
    "The answer passes only if the cited passages state it. Check in this order:\n" + CHECKS + "\n"
    'If every check holds: verdict = "pass", failed = "none", and quote = the shortest span copied character '
    "for character from one cited passage that states the answer.\n"
    'Otherwise: verdict = "reject", failed = the first check that does not hold, and quote = "".\n'
    'Return JSON only: {"verdict": "pass" | "reject", "failed": "none" | "company" | "metric" | "period" | '
    '"figure", "quote": "<span>"}'
)
VERIFIER_USER = "Question: {question}\n\nAnswer: {answer}\n\nCited passages:\n{passages}"
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "failed", "quote"],
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "reject"]},
        "failed": {"type": "string", "enum": ["none", "company", "metric", "period", "figure"]},
        "quote": {"type": "string"},
    },
}

# Appended to the executor's own prompt (same system prompt, same passages), so it is the same role speaking.
SELF_CHECK_SUFFIX = (
    "\n\nYour previous answer: {answer}\nCited: {citations}\n"
    "Check it against the passages it cites:\n" + CHECKS + "\n"
    "If every check holds, return the same answer and citations. If one does not, return a corrected answer "
    "that the passages state, or set found = false."
)
RETRY_SUFFIX = (
    "\n\nA checker rejected your previous answer.\nPrevious answer: {answer}\nCited: {citations}\n"
    "Reason: {reason}.\n"
    "Answer again from the passages above. Cite a passage that does state the answer, or set found = false."
)
# The verifier hands back a category, never wording for the new answer.
VETO_REASONS = {
    "company": "the cited passage is not from the report of the company the question asks about",
    "metric": "the cited passage does not state the metric asked for under its own label",
    "period": "the figure is not for the period the question asks about",
    "figure": "a figure in the answer is not printed in the cited passage",
    "quote": "no span of the cited passage states the answer",
    "unspecified": "the cited passages do not state the answer",
}

PLANNER_SYSTEM = (
    "You plan a search over a set of annual reports. You see only the question and the list of reports, not "
    "their content. Do not answer the question.\n"
    'company = the key of the one report the question is about, copied from the list, or "" if the question '
    "names no company on the list.\n"
    "metric = the metric or fact asked for, in the words that report would print, in the report's language.\n"
    'period = the fiscal year or period asked for (for example 2025), or "" if the question names none.\n'
    "search_query = a short search string in the report's language: the period and the labels that a table "
    "row or sentence stating this metric would carry, separated by spaces. No question words, no full "
    "sentence, and no company name when company is set.\n"
    'Return JSON only: {"company": "<report key or empty>", "metric": "<text>", "period": "<text>", '
    '"search_query": "<text>"}'
)
PLANNER_USER = "Question: {question}\n\nReports:\n{reports}"
REPORT_LINE = "- key: {key} | company: {company} | document: {document} | language: {language}"
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["company", "metric", "period", "search_query"],
    "properties": {
        "company": {"type": "string"},
        "metric": {"type": "string"},
        "period": {"type": "string"},
        "search_query": {"type": "string"},
    },
}


def prompt_sha256() -> str:
    """Hash of everything here that shapes a role's output; pinned before the held-out run."""
    material = {
        "version": ORCHESTRATION_VERSION, "executor": cited_answer.prompt_sha256(), "checks": CHECKS,
        "verifier": [VERIFIER_SYSTEM, VERIFIER_USER, VERDICT_SCHEMA], "min_quote_chars": MIN_QUOTE_CHARS,
        "self_check": SELF_CHECK_SUFFIX, "retry": [RETRY_SUFFIX, VETO_REASONS],
        "planner": [PLANNER_SYSTEM, PLANNER_USER, REPORT_LINE, PLAN_SCHEMA], "max_plan_query_chars": MAX_PLAN_QUERY_CHARS,
        "passage_format": "[{id}]\\n{content}, joined by a blank line",
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def cache_tag(mode: str) -> str:
    """What a cache key must carry so that answers produced one way are never served for another way.

    ``single`` is the plain cited answer, whose prompt hash is already in the key. Every other mode adds the
    orchestration prompt hash: a changed check list or role prompt must not hit yesterday's entries.
    """
    if mode not in MODES:
        raise ValueError(f"unknown orchestration mode: {mode!r}")
    return mode if mode == "single" else f"{mode}@{prompt_sha256()[:16]}"


# --------------------------------------------------------------------------- handoffs


@dataclass(frozen=True)
class ModelCall:
    role: str  # planner | executor | verifier
    step: str  # plan | answer | self_check | verify | retry | verify_retry
    messages: list[dict[str, str]]
    schema: dict[str, Any]


@dataclass(frozen=True)
class Search:
    query: str
    doc: str | None  # report key to stay inside, or None for every report the caller may read


def _passages(hits: Sequence[ContextHit]) -> str:
    return "\n\n".join(f"[{hit.id}]\n{hit.content}" for hit in hits)


def cited_hits(result: dict[str, Any], hits: Sequence[ContextHit]) -> tuple[ContextHit, ...]:
    """The hits an answer cites, in retrieval order: all the verifier is given besides question and answer."""
    cited = set(result["citations"])
    return tuple(hit for hit in hits if hit.id in cited)


def verifier_messages(question: str, answer: str, cited: Sequence[ContextHit]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VERIFIER_SYSTEM},
        {"role": "user", "content": VERIFIER_USER.format(question=question, answer=answer,
                                                         passages=_passages(cited))},
    ]


def _extended(question: str, hits: Sequence[ContextHit], suffix: str) -> list[dict[str, str]]:
    system, user = cited_answer.build_messages(question, hits)
    return [system, {"role": "user", "content": user["content"] + suffix}]


def self_check_messages(question: str, hits: Sequence[ContextHit], previous: dict[str, Any]) -> list[dict[str, str]]:
    return _extended(question, hits, SELF_CHECK_SUFFIX.format(answer=previous["answer"],
                                                              citations=", ".join(previous["citations"])))


def retry_messages(question: str, hits: Sequence[ContextHit], vetoed: dict[str, Any],
                   failed: str) -> list[dict[str, str]]:
    return _extended(question, hits, RETRY_SUFFIX.format(answer=vetoed["answer"],
                                                         citations=", ".join(vetoed["citations"]),
                                                         reason=VETO_REASONS[failed]))


def planner_messages(question: str, catalogue: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
    reports = "\n".join(REPORT_LINE.format(key=r["key"], company=r.get("company", ""),
                                           document=r.get("document", ""), language=r.get("language", ""))
                        for r in catalogue)
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": PLANNER_USER.format(question=question, reports=reports)},
    ]


# --------------------------------------------------------------------------- code checks on role output


@dataclass(frozen=True)
class Verdict:
    outcome: str  # PASS | REJECT
    failed: str  # none | company | metric | period | figure | quote | unspecified
    decided_by: str  # verifier | code
    reason: str  # PASS | VERIFIER_REJECT | INCONSISTENT_PASS | QUOTE_TOO_SHORT | QUOTE_NOT_IN_CITED
    quote: str = ""


def parse_verdict(raw: str, cited: Sequence[ContextHit], min_quote_chars: int = MIN_QUOTE_CHARS) -> Verdict:
    """Strict JSON in the schema's shape, then the pass conditions. Raises ProviderInvalid for anything else.

    Code outranks the verifier here: a "pass" counts only with ``failed = none`` and a quote that is literally in
    one of the cited passages. A pass that names a failed check, or whose quote is too short or not there, is a veto.
    """
    document = strict_json(_strip_think(raw))
    if not isinstance(document, dict) or set(document) != {"verdict", "failed", "quote"}:
        raise ProviderInvalid("VERDICT_SHAPE")
    verdict, failed, quote = document["verdict"], document["failed"], document["quote"]
    if not all(isinstance(value, str) for value in (verdict, failed, quote)):
        raise ProviderInvalid("VERDICT_TYPES")
    if verdict not in {"pass", "reject"} or failed not in {"none", "company", "metric", "period", "figure"}:
        raise ProviderInvalid("VERDICT_VALUE")
    if verdict == "reject":
        return Verdict("REJECT", failed if failed != "none" else "unspecified", "verifier", "VERIFIER_REJECT", quote)
    if failed != "none":
        return Verdict("REJECT", failed, "code", "INCONSISTENT_PASS", quote)
    span = quote.strip()
    if len(span) < min_quote_chars:
        return Verdict("REJECT", "quote", "code", "QUOTE_TOO_SHORT", quote)
    if not any(span in hit.content for hit in cited):
        return Verdict("REJECT", "quote", "code", "QUOTE_NOT_IN_CITED", quote)
    return Verdict("PASS", "none", "verifier", "PASS", quote)


@dataclass(frozen=True)
class Plan:
    company: str  # a report key from the list, or "" for no narrowing
    metric: str
    period: str
    search_query: str


def parse_plan(raw: str, report_keys: Sequence[str]) -> Plan:
    """Strict JSON in the schema's shape and a plan retrieval can use. Raises ProviderInvalid otherwise."""
    document = strict_json(_strip_think(raw))
    if not isinstance(document, dict) or set(document) != {"company", "metric", "period", "search_query"}:
        raise ProviderInvalid("PLAN_SHAPE")
    if not all(isinstance(value, str) for value in document.values()):
        raise ProviderInvalid("PLAN_TYPES")
    company, metric, period, query = (document[k].strip() for k in ("company", "metric", "period", "search_query"))
    if company and company not in report_keys:
        raise ProviderInvalid("PLAN_UNKNOWN_COMPANY")
    if not metric:
        raise ProviderInvalid("PLAN_EMPTY_METRIC")
    if not query:
        raise ProviderInvalid("PLAN_EMPTY_QUERY")
    if len(query) > MAX_PLAN_QUERY_CHARS:
        raise ProviderInvalid("PLAN_QUERY_TOO_LONG")
    return Plan(company, metric, period, query)


def within(doc: str, ranker: Callable[[str, list[dict]], list[tuple[float, dict]]]):
    """``ranker`` over the rows of one report only. Runs after the Store's ACL filter, so it can only narrow."""
    prefix = f"{doc}#"

    def scoped(query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        return ranker(query, [row for row in rows if str(row["id"]).startswith(prefix)])

    return scoped


# --------------------------------------------------------------------------- the flow


class _Given:
    """Hands a reply that already exists to CitedAnswerer, so every executor output gets exactly its checks."""

    def __init__(self, model_id: str, reply: Any):
        self.model_id, self.reply = model_id, reply

    def complete(self, messages, schema):  # noqa: ARG002 - the call already happened
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@dataclass
class Outcome:
    result: dict[str, Any]  # CitedAnswerer's result shape plus an "orchestration" trace; JSON-serializable
    hits: tuple[ContextHit, ...]  # the hits the answer was produced from: the planned ones when the plan was used

    @property
    def evidence(self) -> str:
        return self.result["orchestration"]["evidence"]


def _error_code(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ProviderInvalid) else type(exc).__name__


def _same_answer(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["answer"] == b["answer"] and a["citations"] == b["citations"]


def _withheld(status: str, candidate: dict[str, Any], model_id: str, **extra: Any) -> dict[str, Any]:
    """No answer shown; the candidate that was not let through stays in the record for audit."""
    return cited_answer._result(status, model_answer=candidate["answer"], model_found=True, model_called=True,
                                raw_output=candidate["raw_output"], rejected_citations=candidate["rejected_citations"],
                                model=model_id, **extra)


def flow(mode: str, question: str, hits: Sequence[ContextHit], *, model_id: str,
         catalogue: Sequence[dict[str, Any]] = ()) -> Generator[ModelCall | Search, Any, Outcome]:
    """Yield the next model call or search, receive its reply, return the Outcome.

    A ``ModelCall`` is answered with the model's raw text, or with one of ``MODEL_ERRORS`` when the call failed
    (the step then fails closed). A ``Search`` is answered with the hits it returned, or with a DomainError.
    Exactly one entry is appended to the trace's ``steps`` per ModelCall, in order.
    """
    if mode not in MODES:
        raise ValueError(f"unknown orchestration mode: {mode!r}")
    hits = tuple(hits)
    steps: list[dict[str, Any]] = []
    trace: dict[str, Any] = {"version": ORCHESTRATION_VERSION, "mode": mode, "prompt_sha256": prompt_sha256(),
                             "evidence": "original", "plan": None, "steps": steps}

    def done(result: dict[str, Any]) -> Outcome:
        trace["calls"] = len(steps)
        return Outcome(result | {"orchestration": trace}, hits)

    def executor(step: str, messages: list[dict[str, str]]) -> Generator[ModelCall, Any, dict[str, Any]]:
        reply = yield ModelCall("executor", step, messages, ANSWER_SCHEMA)
        result = CitedAnswerer(_Given(model_id, reply)).answer(question, hits)
        steps.append({"step": step, "role": "executor", "outcome": result["status"],
                      "answer": result["model_answer"], "citations": result["citations"],
                      "rejected_citations": result["rejected_citations"], "error": result["error"],
                      "raw_output": result["raw_output"]})
        return result

    def verifier(step: str, answer: dict[str, Any]) -> Generator[ModelCall, Any, Verdict | None]:
        cited = cited_hits(answer, hits)
        reply = yield ModelCall("verifier", step, verifier_messages(question, answer["answer"], cited),
                                VERDICT_SCHEMA)
        record: dict[str, Any] = {"step": step, "role": "verifier", "cited": [hit.id for hit in cited],
                                  "raw_output": None if isinstance(reply, Exception) else reply}
        steps.append(record)
        try:
            if isinstance(reply, Exception):
                raise reply
            verdict = parse_verdict(reply, cited)
        except MODEL_ERRORS as exc:
            record.update(outcome="ERROR", error=_error_code(exc))
            return None
        record.update(outcome=verdict.outcome, failed=verdict.failed, decided_by=verdict.decided_by,
                      reason=verdict.reason, quote=verdict.quote, error=None)
        return verdict

    if not hits:  # nothing to cite: "not in the reports" without calling any role
        return done(CitedAnswerer(_Given(model_id, None)).answer(question, hits))

    if mode == "plan_verify":
        reply = yield ModelCall("planner", "plan", planner_messages(question, catalogue), PLAN_SCHEMA)
        record = {"step": "plan", "role": "planner", "raw_output": None if isinstance(reply, Exception) else reply}
        steps.append(record)
        try:
            if isinstance(reply, Exception):
                raise reply
            plan = parse_plan(reply, [str(r["key"]) for r in catalogue])
        except MODEL_ERRORS as exc:
            record.update(outcome="INVALID", error=_error_code(exc))
            trace["plan"] = {"status": "INVALID", "reason": record["error"]}
        else:
            record.update(outcome="VALID", error=None)
            planned = yield Search(plan.search_query, plan.company or None)
            trace["plan"] = {"company": plan.company, "metric": plan.metric, "period": plan.period,
                             "search_query": plan.search_query}
            if isinstance(planned, Exception):
                trace["plan"] |= {"status": "FALLBACK_SEARCH_ERROR", "reason": getattr(planned, "code", None)
                                  or type(planned).__name__}
            elif not planned:
                trace["plan"] |= {"status": "FALLBACK_NO_HITS", "reason": None}
            else:
                hits = tuple(planned)
                trace["plan"] |= {"status": "USED", "reason": None}
                trace["evidence"] = "planned"

    first = yield from executor("answer", cited_answer.build_messages(question, hits))
    if mode == "single" or first["status"] != "ANSWERED":
        return done(first)  # no answer to check: errors and "not found" stay what they are

    if mode == "self_check":
        checked = yield from executor("self_check", self_check_messages(question, hits, first))
        steps[-1]["change"] = ("withdrawn" if checked["status"] != "ANSWERED"
                               else "kept" if _same_answer(checked, first) else "revised")
        return done(checked)

    verdict = yield from verifier("verify", first)
    if verdict is None:
        return done(_withheld("VERIFY_ERROR", first, model_id, error=steps[-1]["error"]))
    if verdict.outcome == "PASS":
        return done(first)
    second = yield from executor("retry", retry_messages(question, hits, first, verdict.failed))
    if second["status"] != "ANSWERED":
        return done(second)  # withdrawn after the veto, or unusable: nothing to verify
    if _same_answer(second, first):
        # The same answer and citations would get the same verdict at temperature 0: no second verifier call.
        steps[-1]["repeated_vetoed_answer"] = True
        return done(_withheld("NEEDS_REVIEW", second, model_id))
    verdict = yield from verifier("verify_retry", second)
    if verdict is None:
        return done(_withheld("VERIFY_ERROR", second, model_id, error=steps[-1]["error"]))
    if verdict.outcome == "PASS":
        return done(second)
    return done(_withheld("NEEDS_REVIEW", second, model_id))


def record_timings(outcome: Outcome, call_ms: Sequence[float], search_ms: float = 0.0) -> Outcome:
    """Write what a driver measured into the trace: one duration per model call, in the order of ``steps``."""
    trace = outcome.result["orchestration"]
    for step, ms in zip(trace["steps"], call_ms, strict=True):
        step["ms"] = ms
    trace["model_ms"] = round(sum(call_ms), 1)
    if trace["plan"] is not None:
        trace["plan"]["search_ms"] = round(search_ms, 1)
    return outcome


# --------------------------------------------------------------------------- blocking driver


class Orchestrator:
    """Drives ``flow`` with a blocking chat model. One instance serves every mode; nothing is kept between runs."""

    def __init__(self, model: ChatModel, *, catalogue: Sequence[dict[str, Any]] = ()):
        self.model = model
        self.catalogue = tuple(catalogue)

    def run(self, mode: str, question: str, hits: Sequence[ContextHit], *,
            search: Callable[[str, str | None], Sequence[ContextHit]] | None = None,
            catalogue: Sequence[dict[str, Any]] | None = None) -> Outcome:
        """``search(query, doc)`` is the caller's retrieval for the planned query; only plan_verify uses it."""
        if mode == "plan_verify" and search is None:
            raise ValueError("plan_verify needs a search callback for the planned retrieval")
        steps = flow(mode, question, hits, model_id=self.model.model_id,
                     catalogue=self.catalogue if catalogue is None else catalogue)
        call_ms: list[float] = []
        search_ms = 0.0
        try:
            request = next(steps)
            while True:
                started = time.perf_counter()
                if isinstance(request, Search):
                    try:
                        reply: Any = list(search(request.query, request.doc))
                    except DomainError as exc:
                        reply = exc
                    search_ms += (time.perf_counter() - started) * 1000
                else:
                    try:
                        reply = self.model.complete(request.messages, request.schema)
                    except MODEL_ERRORS as exc:
                        reply = exc
                    call_ms.append(round((time.perf_counter() - started) * 1000, 1))
                request = steps.send(reply)
        except StopIteration as stop:
            outcome: Outcome = stop.value
        return record_timings(outcome, call_ms, search_ms)


def default_orchestrator(base_url: str = "http://127.0.0.1:11434", timeout: float = 300.0) -> Orchestrator | None:
    """An Orchestrator on ``REQUIRED_MODEL`` when the local Ollama lists it, otherwise None.

    Unlike ``cited_answer.default_answerer`` there is no fallback to ``qwen3:8b``: the verifier and the retry
    prompts are longer than the plain answer prompt, and that tag would truncate them without an error.
    """
    if not _loopback(base_url):
        raise ValueError("default_orchestrator only talks to a local server")
    try:
        with httpx.Client(base_url=base_url, timeout=3.0, trust_env=False) as client:
            listed = {str(m.get("name")) for m in client.get("/api/tags").json().get("models", [])}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if REQUIRED_MODEL not in listed and f"{REQUIRED_MODEL}:latest" not in listed:
        return None
    return Orchestrator(OllamaChatModel(REQUIRED_MODEL, base_url, timeout=timeout))
