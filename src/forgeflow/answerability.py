"""Answerability: after retrieval, keep only hits that actually answer the question, or say there is none.

Runs strictly after RankedKnowledgeService, so it only ever sees hits the S1 ACL and citation chain
already returned for this caller, and it can only drop hits, never add or rewrite one: the result is a
subset of the input objects, in their original order. A local model judges each hit and must quote
the supporting span; a "yes" whose span is not literally in that hit's content is rejected.

Failure is closed and visible: unusable model output or a transport error returns no hits with status
GATE_ERROR, never a silent pass-through and never a disguised "no answer".
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from .analysis_provider import ProviderInvalid, strict_json
from .contracts import ContextHit, Principal, Scope
from .retrieval import RankedKnowledgeService, _loopback

PROMPT_VERSION = "answerability-v1"
MIN_QUOTE_CHARS = 6

SYSTEM_PROMPT = (
    "You check retrieved passages against a question. Judge each passage on its own text only, "
    "never on outside knowledge.\n"
    "answers = true only if the passage itself states the specific fact, rule or procedure the question "
    "asks for. Being on the same topic is not enough: if the question asks for a detail the passage does "
    "not give, answers = false.\n"
    "If answers = true, quote = the shortest span copied character for character from that passage that "
    'states the answer. If answers = false, quote = "".\n'
    'Return JSON only, one verdict per passage: {"verdicts": [{"passage": <number>, "answers": '
    '<true|false>, "quote": "<span>"}]}'
)
USER_TEMPLATE = "Question: {query}\n\nPassages:\n{passages}"
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["passage", "answers", "quote"],
                "properties": {
                    "passage": {"type": "integer"},
                    "answers": {"type": "boolean"},
                    "quote": {"type": "string"},
                },
            },
        }
    },
}
# Identifier lookups ("evt-boot", "NEVER-APPROVED-P1") are navigation, not questions: hyphenated tokens.
IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+")


def prompt_sha256() -> str:
    """Hash of everything that shapes the model's judgement; recorded before any real run."""
    material = {
        "version": PROMPT_VERSION,
        "system": SYSTEM_PROMPT,
        "user_template": USER_TEMPLATE,
        "schema": VERDICT_SCHEMA,
        "min_quote_chars": MIN_QUOTE_CHARS,
        "identifier_pattern": IDENTIFIER.pattern,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_messages(query: str, hits: Sequence[ContextHit]) -> list[dict[str, str]]:
    passages = "\n".join(f"[{number}] {hit.content}" for number, hit in enumerate(hits, start=1))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TEMPLATE.format(query=query, passages=passages)},
    ]


class ChatModel(Protocol):
    """Anything that turns chat messages plus a JSON schema into the model's raw text reply."""

    model_id: str

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str: ...


class OllamaChatModel:
    """Local Ollama through its OpenAI-compatible endpoint, thinking off, greedy, schema-constrained."""

    def __init__(
        self,
        model: str = "qwen3:8b",
        base_url: str = "http://127.0.0.1:11434",
        timeout: float = 120.0,
        max_tokens: int = 512,
        transport: httpx.BaseTransport | None = None,
    ):
        if not _loopback(base_url):
            raise ValueError("OllamaChatModel only talks to a local server")
        self.model_id = f"ollama/{model}"
        self.model = model
        self.max_tokens = max_tokens
        # Desktop proxies answer localhost with 502, so loopback traffic ignores proxy settings.
        self._client = httpx.Client(base_url=base_url, timeout=timeout, trust_env=False, transport=transport)
        self.calls: list[dict[str, Any]] = []  # per-call usage and timing, for the run report

    def settings(self) -> dict[str, Any]:
        return {
            "endpoint": "/v1/chat/completions",
            "model": self.model,
            "temperature": 0,
            "seed": 0,
            "reasoning_effort": "none",
            "max_tokens": self.max_tokens,
            "response_format": "json_schema (strict)",
        }

    def complete(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        started = time.perf_counter()
        response = self._client.post(
            "/v1/chat/completions",
            json={
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "seed": 0,
                "reasoning_effort": "none",
                "max_tokens": self.max_tokens,
                "stream": False,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "answerability", "strict": True, "schema": schema},
                },
            },
        )
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        self.calls.append({
            "ms": round((time.perf_counter() - started) * 1000, 1),
            "finish_reason": choice.get("finish_reason"),
            "usage": body.get("usage"),
            "reasoning_returned": bool(message.get("reasoning") or message.get("reasoning_content")),
        })
        if choice.get("finish_reason") != "stop":
            raise ProviderInvalid("OUTPUT_TRUNCATED")
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderInvalid("INVALID_ASSISTANT_CONTENT")
        return content


@dataclass(frozen=True)
class Verdict:
    passage: int  # 1-based position in the hits the gate was given
    answers: bool  # what the model said
    quote: str
    accepted: bool  # what the gate concluded after checking the quote
    reason: str  # SUPPORTED, NOT_ANSWERING, QUOTE_NOT_IN_PASSAGE, QUOTE_TOO_SHORT, NO_VERDICT, IDENTIFIER_MATCH


@dataclass(frozen=True)
class AnswerabilityResult:
    status: Literal["SUPPORTED", "NO_ANSWER_IN_KNOWLEDGE_BASE", "GATE_ERROR"]
    hits: tuple[ContextHit, ...]  # supported hits only: a subset of the input, original order
    kept: tuple[int, ...]  # 0-based positions of `hits` in the input
    verdicts: tuple[Verdict, ...] = ()
    error: str | None = None
    raw_output: str | None = None
    model_called: bool = False
    elapsed_ms: float = 0.0
    prompt_sha256: str = field(default_factory=prompt_sha256)

    @property
    def answerable(self) -> bool:
        return self.status == "SUPPORTED"


def _strip_think(text: str) -> str:
    # Qwen3 sometimes emits an empty <think></think> block even with thinking off; it is not the verdict.
    return re.sub(r"^\s*<think>.*?</think>\s*", "", text, count=1, flags=re.DOTALL)


def parse_verdicts(raw: str, hits: Sequence[ContextHit], min_quote_chars: int = MIN_QUOTE_CHARS) -> list[Verdict]:
    """Strict JSON in the schema's shape; each "yes" must quote a literal span of its own passage.

    Raises ProviderInvalid for anything that is not exactly the expected structure, including a verdict
    for a passage that does not exist or two verdicts for one passage. A missing verdict is a "no".
    """
    document = strict_json(_strip_think(raw))
    if not isinstance(document, dict) or set(document) != {"verdicts"} or not isinstance(document["verdicts"], list):
        raise ProviderInvalid("VERDICT_SHAPE")
    seen: dict[int, Verdict] = {}
    for item in document["verdicts"]:
        if not isinstance(item, dict) or set(item) != {"passage", "answers", "quote"}:
            raise ProviderInvalid("VERDICT_SHAPE")
        passage, answers, quote = item["passage"], item["answers"], item["quote"]
        if type(passage) is not int or type(answers) is not bool or not isinstance(quote, str):
            raise ProviderInvalid("VERDICT_TYPES")
        if not 1 <= passage <= len(hits):
            raise ProviderInvalid("VERDICT_UNKNOWN_PASSAGE")
        if passage in seen:
            raise ProviderInvalid("VERDICT_DUPLICATE_PASSAGE")
        span = quote.strip()
        if not answers:
            reason = "NOT_ANSWERING"
        elif len(span) < min_quote_chars:
            reason = "QUOTE_TOO_SHORT"
        elif span not in hits[passage - 1].content:
            reason = "QUOTE_NOT_IN_PASSAGE"
        else:
            reason = "SUPPORTED"
        seen[passage] = Verdict(passage, answers, quote, reason == "SUPPORTED", reason)
    return [seen.get(n, Verdict(n, False, "", False, "NO_VERDICT")) for n in range(1, len(hits) + 1)]


class AnswerabilityGate:
    def __init__(self, model: ChatModel, min_quote_chars: int = MIN_QUOTE_CHARS):
        self.model = model
        self.min_quote_chars = min_quote_chars
        self.seen: list[tuple[str, tuple[ContextHit, ...]]] = []  # every (query, hits) handed in, for audits

    @staticmethod
    def _result(status, hits, kept, **extra) -> AnswerabilityResult:
        return AnswerabilityResult(status, tuple(hits[i] for i in kept), tuple(kept), **extra)

    def check(self, query: str, hits: Sequence[ContextHit]) -> AnswerabilityResult:
        hits = tuple(hits)
        self.seen.append((query, hits))
        started = time.perf_counter()
        if not hits:
            return self._result("NO_ANSWER_IN_KNOWLEDGE_BASE", hits, [])
        token = query.strip()
        if IDENTIFIER.fullmatch(token):
            # An identifier lookup is answered by the record that carries the identifier, and only by it.
            kept = [i for i, h in enumerate(hits) if h.id.casefold() == token.casefold() or token in h.content]
            verdicts = tuple(
                Verdict(i + 1, i in kept, token if i in kept else "", i in kept,
                        "IDENTIFIER_MATCH" if i in kept else "NOT_ANSWERING")
                for i in range(len(hits))
            )
            status = "SUPPORTED" if kept else "NO_ANSWER_IN_KNOWLEDGE_BASE"
            return self._result(status, hits, kept, verdicts=verdicts)
        raw = None
        try:
            raw = self.model.complete(build_messages(query, hits), VERDICT_SCHEMA)
            verdicts = parse_verdicts(raw, hits, self.min_quote_chars)
        except (ProviderInvalid, httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            code = str(exc) if isinstance(exc, ProviderInvalid) else type(exc).__name__
            return self._result("GATE_ERROR", hits, [], error=code, raw_output=raw, model_called=True,
                                elapsed_ms=(time.perf_counter() - started) * 1000)
        kept = [v.passage - 1 for v in verdicts if v.accepted]
        return self._result(
            "SUPPORTED" if kept else "NO_ANSWER_IN_KNOWLEDGE_BASE", hits, kept, verdicts=tuple(verdicts),
            raw_output=raw, model_called=True, elapsed_ms=(time.perf_counter() - started) * 1000,
        )


class GatedKnowledgeService:
    """RankedKnowledgeService, then the answerability gate on exactly what it returned."""

    def __init__(self, service: RankedKnowledgeService, gate: AnswerabilityGate):
        self.service = service
        self.gate = gate
        self.results: list[AnswerabilityResult] = []

    def answer_search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> AnswerabilityResult:
        # Authorization, validation and citation all happen inside the ranked service; its errors propagate.
        result = self.gate.check(query, self.service.search(principal, scope, query, top_k))
        self.results.append(result)
        return result

    def search(self, principal: Principal, scope: Scope, query: str, top_k: int = 5) -> list[ContextHit]:
        return list(self.answer_search(principal, scope, query, top_k).hits)
