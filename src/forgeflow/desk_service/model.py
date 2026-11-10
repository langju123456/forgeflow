"""The cited-answer model call, streamed: same request as answerability.OllamaChatModel, with ``stream: true``.

Settings are identical (temperature 0, seed 0, thinking off, strict JSON schema, loopback only), so the complete
text a stream produces is what the non-streaming call would have returned, and it goes through the same checks
(forgeflow.cited_answer). Streaming changes when the reader sees words, not what counts as the answer.

Failure classes matter to the worker:

* ``ModelUnavailable`` — connection refused, timeout, 5xx: transient; the job is retried later.
* ``ProviderInvalid`` — the model answered but the output is unusable (cut off at max_tokens, not text):
  a result (ANSWER_ERROR), not retried, because the same input at temperature 0 gives the same output.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..analysis_provider import ProviderInvalid
from ..retrieval import _loopback


class ModelUnavailable(RuntimeError):
    pass


class StreamingChatModel:
    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: float = 300.0,
                 max_tokens: int = 512, transport: httpx.AsyncBaseTransport | None = None):
        if not _loopback(base_url):
            raise ValueError("StreamingChatModel only talks to a local server")
        self.model = model
        self.model_id = f"ollama/{model}"
        self.max_tokens = max_tokens
        # Desktop proxies answer localhost with 502, so loopback traffic ignores proxy settings.
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False, transport=transport)
        self.last_call: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def request(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self.model, "messages": messages, "temperature": 0, "seed": 0, "reasoning_effort": "none",
            "max_tokens": self.max_tokens, "stream": True, "stream_options": {"include_usage": True},
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "cited_answer", "strict": True, "schema": schema}},
        }

    async def stream(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> AsyncIterator[str]:
        """Yield content deltas. Raises ModelUnavailable before the first delta, ProviderInvalid at the end."""
        started = time.perf_counter()
        first_token_ms: float | None = None
        finish: str | None = None
        usage: dict[str, Any] | None = None
        try:
            async with self._client.stream("POST", "/v1/chat/completions",
                                           json=self.request(messages, schema)) as response:
                if response.status_code >= 500:
                    raise ModelUnavailable(f"model server answered {response.status_code}")
                if response.status_code != 200:
                    await response.aread()
                    raise ProviderInvalid(f"MODEL_HTTP_{response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            if first_token_ms is None:
                                first_token_ms = (time.perf_counter() - started) * 1000
                            yield delta
                        finish = choice.get("finish_reason") or finish
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            raise ModelUnavailable(type(exc).__name__) from exc
        finally:
            self.last_call = {"ms": round((time.perf_counter() - started) * 1000, 1),
                              "first_token_ms": None if first_token_ms is None else round(first_token_ms, 1),
                              "finish_reason": finish, "usage": usage}
        if finish != "stop":
            raise ProviderInvalid("OUTPUT_TRUNCATED")
