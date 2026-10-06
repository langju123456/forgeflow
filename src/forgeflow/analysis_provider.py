"""Bounded local Ollama transport; authorization and publication belong upstream.

This adapter has no cloud fallback, implicit retry, tool execution, model download,
credential loading, or daemon configuration. An unresolved dispatch quarantines
this provider instance; a fresh instance does not reconcile the original request.
The durable control plane must also enforce its profile-wide occupancy ledger.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from typing import Any

import httpx

ENDPOINT = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"
MODEL_DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
RESPONSE_BYTES = 128 * 1024
INPUT_TOKEN_BOUND = 3500
OUTPUT_TOKENS = 800
CONTEXT_TOKENS = 8192
CALL_SECONDS = 60.0


class ProviderInvalid(ValueError):
    """An allowlisted reason, never an upstream body or exception representation."""


def strict_json(raw: bytes | str) -> Any:
    """Reject duplicate keys, nonfinite numbers, and non-UTF8 JSON documents."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProviderInvalid("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def constant(_: str) -> Any:
        raise ProviderInvalid("NONFINITE_JSON_NUMBER")

    def finite(value: Any) -> None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ProviderInvalid("NONFINITE_JSON_NUMBER")
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        if isinstance(value, dict):
            for key, item in value.items():
                key.encode("utf-8", errors="strict")
                finite(item)
        elif isinstance(value, list):
            for item in value:
                finite(item)

    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        finite(result)
        return result
    except ProviderInvalid:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ProviderInvalid("INVALID_JSON_DOCUMENT") from exc


class OllamaProvider:
    """Single-instance, nonqueuing local profile transport.

    ``transport`` exists for explicitly identified TEST_DRIVER tests. Production
    construction leaves it unset and cannot select an endpoint, model or digest.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._busy = False
        self._unresolved_call: str | None = None

    @property
    def unresolved_call_id(self) -> str | None:
        return self._unresolved_call

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=ENDPOINT,
            trust_env=False,
            follow_redirects=False,
            transport=self._transport,
            timeout=httpx.Timeout(CALL_SECONDS, connect=3.0),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    async def _request(
        self, client: httpx.AsyncClient, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict[str, Any]]:
        async with client.stream(method, path, json=payload) as response:
            # Refuse compression rather than count only an inflated/decompressed body.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ProviderInvalid("COMPRESSED_RESPONSE")
            body = bytearray()
            async for part in response.aiter_raw():
                if len(body) + len(part) > RESPONSE_BYTES:
                    raise ProviderInvalid("RESPONSE_BYTE_LIMIT")
                body.extend(part)
            value = strict_json(bytes(body))
            if not isinstance(value, dict):
                raise ProviderInvalid("RESPONSE_NOT_OBJECT")
            return response.status_code, value

    @staticmethod
    def _local_record(record: dict[str, Any]) -> None:
        if record.get("remote_host") or record.get("remote_model"):
            raise ProviderInvalid("REMOTE_PROFILE_REJECTED")

    async def _preflight(self, client: httpx.AsyncClient) -> dict[str, Any]:
        status, version = await self._request(client, "GET", "/api/version")
        if status != 200 or not isinstance(version.get("version"), str):
            raise ProviderInvalid("VERSION_UNVERIFIED")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version["version"]):
            raise ProviderInvalid("VERSION_UNVERIFIED")
        status, tags = await self._request(client, "GET", "/api/tags")
        models = tags.get("models")
        if status != 200 or not isinstance(models, list):
            raise ProviderInvalid("MODEL_INVENTORY_UNVERIFIED")
        matches = [item for item in models if isinstance(item, dict) and item.get("name") == MODEL]
        if len(matches) != 1 or matches[0].get("digest") != MODEL_DIGEST:
            raise ProviderInvalid("PINNED_MODEL_MISMATCH")
        self._local_record(matches[0])
        status, show = await self._request(client, "POST", "/api/show", {"model": MODEL, "verbose": False})
        if status != 200:
            raise ProviderInvalid("MODEL_METADATA_UNVERIFIED")
        self._local_record(show)
        details, info, capabilities = show.get("details"), show.get("model_info"), show.get("capabilities")
        if not isinstance(details, dict) or not isinstance(info, dict) or not isinstance(capabilities, list):
            raise ProviderInvalid("MODEL_CAPABILITY_UNVERIFIED")
        if details.get("format") != "gguf" or info.get("general.architecture") != "qwen3":
            raise ProviderInvalid("MODEL_ARCHITECTURE_MISMATCH")
        context = info.get("qwen3.context_length")
        if type(context) is not int or context < CONTEXT_TOKENS:
            raise ProviderInvalid("MODEL_CONTEXT_UNVERIFIED")
        if "completion" not in capabilities or "thinking" not in capabilities:
            raise ProviderInvalid("MODEL_CAPABILITY_UNVERIFIED")
        status, running = await self._request(client, "GET", "/api/ps")
        if status != 200 or not isinstance(running.get("models"), list):
            raise ProviderInvalid("LOADED_MODEL_UNVERIFIED")
        for item in running["models"]:
            if not isinstance(item, dict):
                raise ProviderInvalid("LOADED_MODEL_UNVERIFIED")
            if item.get("name") == MODEL:
                self._local_record(item)
                if item.get("digest") != MODEL_DIGEST:
                    raise ProviderInvalid("LOADED_MODEL_REVISION_MISMATCH")
        # Do not return modelfile, template, parameters, paths, other model names or license body.
        return {
            "status": "VERIFIED",
            "endpoint": ENDPOINT,
            "model": MODEL,
            "digest": MODEL_DIGEST,
            "ollama_version": version["version"],
            "capabilities": [x for x in ("completion", "tools", "thinking") if x in capabilities],
            "context_length": context,
            "remote_profile": False,
        }

    async def preflight(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Read-only profile checks; no generation, model loading or daemon mutation."""
        if type(timeout_seconds) not in (float, int) or not 0 < timeout_seconds <= CALL_SECONDS:
            return {"status": "DENIED", "error_code": "INVALID_TIMEOUT"}
        try:
            async with asyncio.timeout(timeout_seconds), self._client() as client:
                return await self._preflight(client)
        except ProviderInvalid as exc:
            return {"status": "DENIED", "error_code": str(exc)}
        except (TimeoutError, httpx.HTTPError):
            return {"status": "DENIED", "error_code": "PREFLIGHT_UNAVAILABLE"}

    @staticmethod
    def _validate_call(call: dict[str, Any]) -> tuple[list[dict[str, str]], dict, float, int]:
        if not isinstance(call, dict):
            raise ProviderInvalid("INVALID_CALL")
        if call.get("profile_digest") != MODEL_DIGEST:
            raise ProviderInvalid("PROFILE_DIGEST_MISMATCH")
        if not isinstance(call.get("call_id"), str) or not 1 <= len(call["call_id"]) <= 128:
            raise ProviderInvalid("INVALID_CALL_ID")
        timeout = call.get("timeout_seconds")
        if type(timeout) not in (float, int) or not math.isfinite(timeout) or not 0 < timeout <= CALL_SECONDS:
            raise ProviderInvalid("INVALID_TIMEOUT")
        messages = call.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 8:
            raise ProviderInvalid("INVALID_MESSAGES")
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise ProviderInvalid("INVALID_MESSAGES")
            if message["role"] not in ("system", "user", "assistant"):
                raise ProviderInvalid("INVALID_MESSAGES")
            if not isinstance(message["content"], str) or not message["content"]:
                raise ProviderInvalid("INVALID_MESSAGES")
        if "schema" in call and "output_schema" in call:
            raise ProviderInvalid("AMBIGUOUS_OUTPUT_SCHEMA")
        schema = call.get("schema", call.get("output_schema"))
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ProviderInvalid("INVALID_OUTPUT_SCHEMA")
        if schema.get("additionalProperties") is not False or not isinstance(schema.get("properties"), dict):
            raise ProviderInvalid("OPEN_OUTPUT_SCHEMA")
        try:
            material = json.dumps(
                {"messages": messages, "schema": schema},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", errors="strict")
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ProviderInvalid("INVALID_INPUT_MATERIAL") from exc
        # Conservative byte-level upper bound, not a claim of exact tokenizer counts.
        # It covers schema and message framing and deliberately rejects large inputs.
        token_bound = len(material) + 256 + 64 * len(messages)
        if token_bound > INPUT_TOKEN_BOUND:
            raise ProviderInvalid("INPUT_CONSERVATIVE_BOUND_EXCEEDED")
        return messages, schema, float(timeout), token_bound

    @staticmethod
    def _usage(response: dict[str, Any] | None, dispatched: bool) -> dict[str, Any]:
        if not dispatched:
            return {
                "input_tokens": None,
                "output_tokens": None,
                "cost_microusd": None,
                "state": "NOT_DISPATCHED",
            }
        response = response or {}
        input_count, output_count = response.get("prompt_eval_count"), response.get("eval_count")
        valid = all(type(x) is int and x >= 0 for x in (input_count, output_count))
        return {
            "input_tokens": input_count if type(input_count) is int and input_count >= 0 else None,
            "output_tokens": output_count if type(output_count) is int and output_count >= 0 else None,
            # Loopback metadata does not prove cloud mode or absent billing; no invented zero cost.
            "cost_microusd": None,
            "state": "CONFIRMED" if valid else "UNKNOWN",
        }

    async def _verify_loaded_revision(self, client: httpx.AsyncClient) -> None:
        status, running = await self._request(client, "GET", "/api/ps")
        models = running.get("models")
        if status != 200 or not isinstance(models, list):
            raise ProviderInvalid("ACTUAL_MODEL_REVISION_UNVERIFIED")
        matches = [x for x in models if isinstance(x, dict) and x.get("name") == MODEL]
        if len(matches) != 1 or matches[0].get("digest") != MODEL_DIGEST:
            raise ProviderInvalid("ACTUAL_MODEL_REVISION_UNVERIFIED")
        self._local_record(matches[0])
        status, tags = await self._request(client, "GET", "/api/tags")
        if status != 200 or not isinstance(tags.get("models"), list):
            raise ProviderInvalid("ACTUAL_MODEL_REVISION_UNVERIFIED")
        matches = [x for x in tags["models"] if isinstance(x, dict) and x.get("name") == MODEL]
        if len(matches) != 1 or matches[0].get("digest") != MODEL_DIGEST:
            raise ProviderInvalid("ACTUAL_MODEL_REVISION_UNVERIFIED")

    async def generate(self, call: dict[str, Any]) -> dict[str, Any]:
        """Execute one authorized call; strict content validation remains upstream."""
        started = time.monotonic()
        dispatched = False
        completed = False
        response: dict[str, Any] | None = None
        finish_reason: str | None = None
        input_bound: int | None = None
        held = False

        def reply(
            status: str, error: str | None = None, content: dict | None = None, raw: str | None = None
        ) -> dict:
            return {
                "status": status,
                "content": content,
                "content_utf8": raw,
                "model": MODEL,
                "digest": MODEL_DIGEST if status == "SUCCEEDED" else None,
                "actual_model_revision": MODEL_DIGEST if status == "SUCCEEDED" else None,
                "finish_reason": finish_reason,
                "usage": self._usage(response, dispatched),
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "error_code": error,
                "dispatched": dispatched,
                "completion_confirmed": completed,
                "input_count_method": "CONSERVATIVE_UTF8_BYTE_UPPER_BOUND",
                "input_token_bound": input_bound,
                "transport": "TEST_DRIVER" if self._transport is not None else "OLLAMA_LOCAL_HTTP",
            }

        try:
            messages, schema, timeout, input_bound = self._validate_call(call)
            if self._busy or self._unresolved_call is not None:
                return reply("DENIED", "PROFILE_OCCUPIED_OR_UNRESOLVED")
            self._busy, held = True, True
            async with asyncio.timeout(timeout), self._client() as client:
                await self._preflight(client)
                # No await between this marker and entering the HTTP request: upstream
                # has already persisted its intent/reservation before calling us.
                dispatched = True
                http_status, response = await self._request(
                    client,
                    "POST",
                    "/api/chat",
                    {
                        "model": MODEL,
                        "messages": messages,
                        "format": schema,
                        "stream": False,
                        "think": False,
                        "keep_alive": "30s",
                        "options": {
                            "num_ctx": CONTEXT_TOKENS,
                            "num_predict": OUTPUT_TOKENS,
                            "temperature": 0,
                        },
                    },
                )
                if http_status != 200 or "error" in response:
                    return reply("PROVIDER_ERROR", "PROVIDER_HTTP_ERROR")
                completed = response.get("done") is True and response.get("done_reason") in ("stop", "length")
                finish_reason = (
                    response.get("done_reason") if isinstance(response.get("done_reason"), str) else None
                )
                if response.get("model") != MODEL:
                    raise ProviderInvalid("RESPONSE_MODEL_MISMATCH")
                if not completed:
                    raise ProviderInvalid("COMPLETION_UNCONFIRMED")
                if finish_reason != "stop":
                    raise ProviderInvalid("OUTPUT_TRUNCATED")
                usage = self._usage(response, True)
                if usage["state"] != "CONFIRMED":
                    raise ProviderInvalid("TOKEN_USAGE_UNVERIFIED")
                if usage["input_tokens"] > INPUT_TOKEN_BOUND or usage["output_tokens"] > OUTPUT_TOKENS:
                    raise ProviderInvalid("PROVIDER_TOKEN_LIMIT_EXCEEDED")
                message = response.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ProviderInvalid("INVALID_ASSISTANT_MESSAGE")
                if message.get("tool_calls") or message.get("thinking"):
                    raise ProviderInvalid("UNEXPECTED_TOOL_OR_THINKING_OUTPUT")
                raw = message.get("content")
                if not isinstance(raw, str):
                    raise ProviderInvalid("INVALID_ASSISTANT_CONTENT")
                content = strict_json(raw)
                if not isinstance(content, dict):
                    raise ProviderInvalid("CONTENT_NOT_OBJECT")
                await self._verify_loaded_revision(client)
                return reply("SUCCEEDED", content=content, raw=raw)
        except ProviderInvalid as exc:
            status = (
                "OUTPUT_INVALID"
                if dispatched and completed
                else "OUTCOME_UNKNOWN"
                if dispatched
                else "DENIED"
            )
            return reply(status, str(exc))
        except (TimeoutError, httpx.HTTPError):
            return reply("OUTCOME_UNKNOWN" if dispatched else "DENIED", "PROVIDER_TIMEOUT_OR_CONNECTION")
        except asyncio.CancelledError:
            # Cancellation of the client task does not establish server cancellation.
            return reply("OUTCOME_UNKNOWN" if dispatched else "DENIED", "CLIENT_CANCELLED")
        finally:
            if held:
                self._busy = False
                if dispatched and not completed:
                    self._unresolved_call = call["call_id"]
