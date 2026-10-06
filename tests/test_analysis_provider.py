"""TEST_DRIVER transport checks, not evidence of real LLM quality or billing."""

import asyncio
import json

import httpx
import pytest

from forgeflow.analysis_provider import MODEL, MODEL_DIGEST, OllamaProvider, ProviderInvalid, strict_json


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def response(value=None, *, status=200, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(value).encode()
    return httpx.Response(status, stream=BytesStream(data), headers=headers)


def call(**changes):
    value = {
        "call_id": "test-call-1",
        "profile_digest": MODEL_DIGEST,
        "timeout_seconds": 2.0,
        "messages": [{"role": "user", "content": "Synthetic fixture only. Return value 1."}],
        "schema": {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    value.update(changes)
    return value


class Driver:
    def __init__(self, result=None, *, chat=None):
        self.requests = []
        self.generated = False
        self.chat = chat
        self.result = result or {
            "model": MODEL,
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": '{"value":1}'},
            "prompt_eval_count": 50,
            "eval_count": 8,
        }
        self.tag_digest = MODEL_DIGEST
        self.loaded_digest = MODEL_DIGEST
        self.remote = None

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/api/version":
            return response({"version": "0.32.1"})
        if path == "/api/tags":
            return response({"models": [{"name": MODEL, "digest": self.tag_digest}]})
        if path == "/api/show":
            return response(
                {
                    "remote_host": self.remote,
                    "details": {"format": "gguf"},
                    "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
                    "capabilities": ["completion", "thinking", "tools"],
                }
            )
        if path == "/api/ps":
            return response(
                {"models": [{"name": MODEL, "digest": self.loaded_digest}] if self.generated else []}
            )
        if path == "/api/chat":
            self.generated = True
            if self.chat:
                return await self.chat(request)
            return response(self.result)
        raise AssertionError("unexpected path")

    def provider(self):
        return OllamaProvider(transport=httpx.MockTransport(self.handle))

    @property
    def chat_count(self):
        return sum(r.url.path == "/api/chat" for r in self.requests)


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"x":1,"x":2}', "DUPLICATE_JSON_KEY"),
        ('{"x":NaN}', "NONFINITE_JSON_NUMBER"),
        ('{"x":1e500}', "NONFINITE_JSON_NUMBER"),
        ("{}{}", "INVALID_JSON_DOCUMENT"),
        (b'"\xff"', "INVALID_JSON_DOCUMENT"),
        ('{"x":"\\ud800"}', "INVALID_JSON_DOCUMENT"),
    ],
)
def test_strict_json_rejects(raw, code):
    with pytest.raises(ProviderInvalid, match=code):
        strict_json(raw)


async def test_preflight_is_read_only_and_sanitized():
    driver = Driver()
    result = await driver.provider().preflight()
    assert result["status"] == "VERIFIED"
    assert result["digest"] == MODEL_DIGEST
    assert driver.chat_count == 0
    assert [r.url.path for r in driver.requests] == ["/api/version", "/api/tags", "/api/show", "/api/ps"]
    assert all(r.url.host == "127.0.0.1" and r.url.port == 11434 for r in driver.requests)
    assert "modelfile" not in result


async def test_success_fixed_profile_and_limits(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    driver = Driver()
    result = await driver.provider().generate(call())
    assert result["status"] == "SUCCEEDED"
    assert result["content"] == {"value": 1}
    assert result["actual_model_revision"] == MODEL_DIGEST
    assert result["usage"] == {
        "input_tokens": 50,
        "output_tokens": 8,
        "cost_microusd": None,
        "state": "CONFIRMED",
    }
    assert result["transport"] == "TEST_DRIVER"
    request = next(r for r in driver.requests if r.url.path == "/api/chat")
    sent = json.loads(request.content)
    assert sent["model"] == MODEL and sent["stream"] is False and sent["think"] is False
    assert sent["options"] == {"num_ctx": 8192, "num_predict": 800, "temperature": 0}
    assert "tools" not in sent and sent["keep_alive"] == "30s"
    assert driver.chat_count == 1


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"profile_digest": "other"}, "PROFILE_DIGEST_MISMATCH"),
        ({"call_id": ""}, "INVALID_CALL_ID"),
        ({"timeout_seconds": 61}, "INVALID_TIMEOUT"),
        ({"timeout_seconds": float("nan")}, "INVALID_TIMEOUT"),
        ({"timeout_seconds": True}, "INVALID_TIMEOUT"),
        ({"messages": [{"role": "tool", "content": "text"}]}, "INVALID_MESSAGES"),
        ({"messages": [{"role": "user", "content": "text", "url": "evil"}]}, "INVALID_MESSAGES"),
        ({"messages": [{"role": "user", "content": "x" * 3500}]}, "INPUT_CONSERVATIVE_BOUND_EXCEEDED"),
        ({"schema": {"type": "object", "properties": {}}}, "OPEN_OUTPUT_SCHEMA"),
        ({"output_schema": {}}, "AMBIGUOUS_OUTPUT_SCHEMA"),
    ],
)
async def test_invalid_call_has_zero_requests(changes, code):
    driver = Driver()
    result = await driver.provider().generate(call(**changes))
    assert result["status"] == "DENIED" and result["error_code"] == code
    assert result["dispatched"] is False and result["usage"]["state"] == "NOT_DISPATCHED"
    assert not driver.requests


async def test_preflight_digest_mismatch_prevents_dispatch():
    driver = Driver()
    driver.tag_digest = "wrong"
    result = await driver.provider().generate(call())
    assert result["status"] == "DENIED" and result["error_code"] == "PINNED_MODEL_MISMATCH"
    assert driver.chat_count == 0


async def test_remote_model_prevents_dispatch():
    driver = Driver()
    driver.remote = "https://cloud.invalid"
    result = await driver.provider().generate(call())
    assert result["status"] == "DENIED" and result["error_code"] == "REMOTE_PROFILE_REJECTED"
    assert driver.chat_count == 0


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"model": "evil"}, "RESPONSE_MODEL_MISMATCH"),
        ({"done_reason": "length"}, "OUTPUT_TRUNCATED"),
        ({"prompt_eval_count": None}, "TOKEN_USAGE_UNVERIFIED"),
        ({"eval_count": True}, "TOKEN_USAGE_UNVERIFIED"),
        ({"eval_count": 801}, "PROVIDER_TOKEN_LIMIT_EXCEEDED"),
        ({"prompt_eval_count": 3501}, "PROVIDER_TOKEN_LIMIT_EXCEEDED"),
        ({"message": {"role": "assistant", "content": '{"x":1,"x":2}'}}, "DUPLICATE_JSON_KEY"),
        ({"message": {"role": "assistant", "content": '{"x":1e500}'}}, "NONFINITE_JSON_NUMBER"),
        ({"message": {"role": "assistant", "content": "{} tail"}}, "INVALID_JSON_DOCUMENT"),
        ({"message": {"role": "assistant", "content": "[]"}}, "CONTENT_NOT_OBJECT"),
        (
            {"message": {"role": "assistant", "content": "{}", "tool_calls": [{}]}},
            "UNEXPECTED_TOOL_OR_THINKING_OUTPUT",
        ),
        (
            {"message": {"role": "assistant", "content": "{}", "thinking": "secret"}},
            "UNEXPECTED_TOOL_OR_THINKING_OUTPUT",
        ),
    ],
)
async def test_completed_invalid_reply_has_no_content(changes, code):
    driver = Driver()
    driver.result.update(changes)
    result = await driver.provider().generate(call())
    assert result["status"] == "OUTPUT_INVALID" and result["error_code"] == code
    assert result["content"] is None and result["content_utf8"] is None
    assert result["dispatched"] is True and result["completion_confirmed"] is True
    assert driver.chat_count == 1


async def test_actual_revision_mismatch_rejects_result():
    driver = Driver()
    driver.loaded_digest = "wrong"
    result = await driver.provider().generate(call())
    assert result["status"] == "OUTPUT_INVALID" and result["error_code"] == "ACTUAL_MODEL_REVISION_UNVERIFIED"
    assert result["content"] is None


async def test_http_error_preserves_unknown_usage_and_does_not_retry():
    async def chat(_):
        return response({"error": "Synthetic secret error text"}, status=500)

    driver = Driver(chat=chat)
    provider = driver.provider()
    result = await provider.generate(call())
    assert result["status"] == "PROVIDER_ERROR" and result["usage"]["state"] == "UNKNOWN"
    assert "Synthetic secret" not in json.dumps(result)
    second = await provider.generate(call(call_id="other"))
    assert second["status"] == "DENIED"
    assert provider.unresolved_call_id == "test-call-1" and driver.chat_count == 1


async def test_timeout_after_dispatch_quarantines_profile():
    async def chat(_):
        await asyncio.Event().wait()

    driver = Driver(chat=chat)
    provider = driver.provider()
    result = await provider.generate(call(timeout_seconds=0.03))
    assert result["status"] == "OUTCOME_UNKNOWN" and result["dispatched"] is True
    assert result["usage"]["input_tokens"] is None and result["usage"]["output_tokens"] is None
    assert result["usage"]["state"] == "UNKNOWN"
    assert (await provider.generate(call()))["status"] == "DENIED"
    assert driver.chat_count == 1


async def test_cancel_after_dispatch_is_unknown_and_no_parallel_call():
    entered = asyncio.Event()

    async def chat(_):
        entered.set()
        await asyncio.Event().wait()

    driver = Driver(chat=chat)
    provider = driver.provider()
    task = asyncio.create_task(provider.generate(call()))
    await entered.wait()
    assert (await provider.generate(call(call_id="parallel")))[
        "error_code"
    ] == "PROFILE_OCCUPIED_OR_UNRESOLVED"
    task.cancel()
    result = await task
    assert result["status"] == "OUTCOME_UNKNOWN" and result["error_code"] == "CLIENT_CANCELLED"
    assert driver.chat_count == 1


@pytest.mark.parametrize("kind", ["large", "compressed", "redirect", "bad-json"])
async def test_response_transport_invariants(kind):
    async def chat(_):
        if kind == "large":
            return response(raw=b"x" * (128 * 1024 + 1))
        if kind == "compressed":
            return response(raw=b"not-decoded", headers={"content-encoding": "gzip"})
        if kind == "redirect":
            return response({}, status=307, headers={"location": "https://cloud.invalid"})
        return response(raw=b'{"x":1,"x":2}')

    driver = Driver(chat=chat)
    provider = driver.provider()
    result = await provider.generate(call())
    assert result["status"] in ("OUTCOME_UNKNOWN", "PROVIDER_ERROR")
    assert result["content"] is None and driver.chat_count == 1
    assert all(r.url.host == "127.0.0.1" for r in driver.requests)
    assert provider.unresolved_call_id == "test-call-1"
