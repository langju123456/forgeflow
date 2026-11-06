"""Cited answer: a short answer to a question from retrieved hits, citing the hits it used, or "not found".

Runs after retrieval (and, when enabled, after the answerability gate), so it only ever sees hits the S1 ACL
and citation chain already returned for this caller. It never adds evidence: a citation must be the id of
one of the hits it was given, anything else is dropped and reported. An answer is only ``found`` when the
model says so, the answer text is not empty and at least one citation survives; otherwise it is "not
found", and the model's own words are kept for audit, not shown as an answer.

    answerer = CitedAnswerer(OllamaChatModel("qwen3:8b"))
    result = answerer.answer(question, hits)   # {"answer", "citations", "found", "status", ...}

No hits: returns found=False without calling the model. Unusable model output or a transport error:
found=False with status ANSWER_ERROR, never a silent "not found".

Context length: Ollama loads qwen3:8b with a 4096-token context on a 24 GB Mac and silently drops the start
of a longer prompt (system instruction included); five real annual-report hits can exceed that. The
OpenAI-compatible endpoint ignores ``options.num_ctx``, so ``default_answerer`` prefers a local tag of the
same weights with a larger context (``ollama create forgeflow-qwen3-8b-ctx16k`` from
``FROM qwen3:8b`` + ``PARAMETER num_ctx 16384``, as scripts/mac_real_reports_eval.sh does).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from .analysis_provider import ProviderInvalid, strict_json
from .answerability import ChatModel, OllamaChatModel, _strip_think
from .contracts import ContextHit
from .retrieval import _loopback

PREFERRED_MODELS = ("forgeflow-qwen3-8b-ctx16k", "qwen3:8b")

PROMPT_VERSION = "cited-answer-v1"

SYSTEM_PROMPT = (
    "You answer a question using only the passages given, never outside knowledge. Each passage starts "
    "with its id in square brackets.\n"
    "If the passages state the answer, set found = true, write a short answer (one or two sentences) in "
    "the same language as the question, copy figures exactly as printed in the passage together with their "
    "unit, and list in citations the ids of the passages that state it.\n"
    "If the passages do not state the specific figure or fact the question asks for, set found = false, "
    'answer = "" and citations = []. Being on the same topic is not enough. Do not estimate, do not '
    "compute it from other figures, do not guess.\n"
    'Return JSON only: {"answer": "<text>", "citations": ["<passage id>"], "found": <true|false>}'
)
USER_TEMPLATE = "Question: {question}\n\nPassages:\n{passages}"
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "citations", "found"],
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "found": {"type": "boolean"},
    },
}


def prompt_sha256() -> str:
    """Hash of everything that shapes the answer; recorded before any real run."""
    material = {"version": PROMPT_VERSION, "system": SYSTEM_PROMPT, "user_template": USER_TEMPLATE,
                "schema": ANSWER_SCHEMA, "passage_format": "[{id}]\\n{content}, joined by a blank line"}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_messages(question: str, hits: Sequence[ContextHit]) -> list[dict[str, str]]:
    passages = "\n\n".join(f"[{hit.id}]\n{hit.content}" for hit in hits)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(question=question, passages=passages)},
    ]


def parse_answer(raw: str) -> tuple[str, list[str], bool]:
    """Strict JSON in exactly the schema's shape; raises ProviderInvalid for anything else."""
    document = strict_json(_strip_think(raw))
    if not isinstance(document, dict) or set(document) != {"answer", "citations", "found"}:
        raise ProviderInvalid("ANSWER_SHAPE")
    answer, citations, found = document["answer"], document["citations"], document["found"]
    if not isinstance(answer, str) or type(found) is not bool or not isinstance(citations, list) \
            or not all(isinstance(c, str) for c in citations):
        raise ProviderInvalid("ANSWER_TYPES")
    return answer, citations, found


def _result(status: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "answer": "", "citations": [], "found": False, "status": status, "rejected_citations": [],
        "model_answer": None, "model_found": None, "model_called": False, "raw_output": None, "error": None,
        "elapsed_ms": 0.0, "prompt_sha256": prompt_sha256(), "model": None,
    }
    base.update(extra)
    return base


class CitedAnswerer:
    """One model call per question over all hits it is given; citations checked against those hits."""

    def __init__(self, model: ChatModel):
        self.model = model
        self.seen: list[tuple[str, tuple[ContextHit, ...]]] = []  # every (question, hits) handed in, for audits

    def answer(self, question: str, hits: Sequence[ContextHit]) -> dict[str, Any]:
        hits = tuple(hits)
        self.seen.append((question, hits))
        if not hits:
            return _result("NO_HITS")
        return self._answer(question, hits) | {"model": self.model.model_id}

    def _answer(self, question: str, hits: tuple[ContextHit, ...]) -> dict[str, Any]:
        started = time.perf_counter()
        raw = None
        try:
            raw = self.model.complete(build_messages(question, hits), ANSWER_SCHEMA)
            text, cited, model_found = parse_answer(raw)
        except (ProviderInvalid, httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            code = str(exc) if isinstance(exc, ProviderInvalid) else type(exc).__name__
            return _result("ANSWER_ERROR", error=code, raw_output=raw, model_called=True,
                           elapsed_ms=(time.perf_counter() - started) * 1000)
        ids = {hit.id for hit in hits}
        valid: list[str] = []
        rejected: list[str] = []
        for citation in cited:
            token = citation.strip().strip("[]").strip()
            if token in ids:
                if token not in valid:
                    valid.append(token)
            else:
                rejected.append(citation)
        audit = {"model_answer": text, "model_found": model_found, "model_called": True, "raw_output": raw,
                 "rejected_citations": rejected, "elapsed_ms": (time.perf_counter() - started) * 1000}
        if not model_found:
            return _result("NOT_FOUND", **audit)
        if not text.strip() or not valid:
            # The model claims an answer it cannot tie to a given hit: not shown as an answer.
            return _result("UNSUPPORTED", **audit)
        return _result("ANSWERED", answer=text.strip(), citations=valid, found=True, **audit)


def default_answerer(base_url: str = "http://127.0.0.1:11434",
                     models: Sequence[str] = PREFERRED_MODELS) -> Callable[[str, Sequence[ContextHit]], dict] | None:
    """``CitedAnswerer.answer`` over the first of ``models`` the local Ollama lists, or None without one."""
    if not _loopback(base_url):
        raise ValueError("default_answerer only talks to a local server")
    try:
        with httpx.Client(base_url=base_url, timeout=3.0, trust_env=False) as client:
            listed = {str(m.get("name")) for m in client.get("/api/tags").json().get("models", [])}
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    for name in models:
        if name in listed or f"{name}:latest" in listed:
            return CitedAnswerer(OllamaChatModel(name, base_url)).answer
    return None
