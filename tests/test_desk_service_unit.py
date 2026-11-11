"""Desk service pieces that need no Redis, RabbitMQ or model: cache keys, the streamed-answer extractor,
SSE frames, and the streaming model client's failure classes."""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("grpc")

from forgeflow.analysis_provider import ProviderInvalid  # noqa: E402
from forgeflow.desk_service import keys  # noqa: E402
from forgeflow.desk_service.events import AnswerFieldExtractor, sse_frame  # noqa: E402
from forgeflow.desk_service.model import ModelUnavailable, StreamingChatModel  # noqa: E402

BASE = dict(subject="desk-analyst", index_version="abc", model="m", prompt_sha256="p", gate=False, top_k=5,
            question="What was revenue?")


def test_cache_key_ignores_spacing_only():
    assert keys.cache_key(**BASE) == keys.cache_key(**{**BASE, "question": "  What   was\trevenue? "})
    assert keys.cache_key(**BASE) != keys.cache_key(**{**BASE, "question": "what was revenue?"})


@pytest.mark.parametrize("field,value", [("subject", "someone-else"), ("index_version", "def"), ("model", "m2"),
                                         ("prompt_sha256", "p2"), ("gate", True), ("top_k", 3)])
def test_cache_key_changes_with_everything_that_changes_the_answer(field, value):
    # A different caller can see different hits (ACL before ranking): sharing a key would leak answers.
    assert keys.cache_key(**BASE) != keys.cache_key(**{**BASE, field: value})


RAW = json.dumps({"answer": 'Revenue was "RMB 1,234" million\\ 中文 😀\nline two', "citations": ["a#1"],
                  "found": True}, ensure_ascii=True)


@pytest.mark.parametrize("split", range(1, len(RAW)))
def test_extractor_decodes_the_answer_however_the_stream_is_split(split):
    extractor = AnswerFieldExtractor()
    pieces = [extractor.feed(RAW[:split]), extractor.feed(RAW[split:])]
    assert "".join(pieces) == json.loads(RAW)["answer"]
    assert extractor.raw == RAW


def test_extractor_one_character_at_a_time_and_nothing_after_the_answer():
    extractor = AnswerFieldExtractor()
    out = [extractor.feed(ch) for ch in RAW]
    assert "".join(out) == json.loads(RAW)["answer"]
    assert extractor.done and extractor.feed('{"answer": "again"}') == ""


def test_extractor_finds_the_answer_when_it_is_not_the_first_key():
    raw = '<think></think>{"found": false, "citations": [], "answer": ""}'
    extractor = AnswerFieldExtractor()
    assert extractor.feed(raw) == "" and extractor.done


def test_sse_frame_has_id_event_and_one_data_line_per_line():
    frame = sse_frame({"id": "1-0", "type": "token", "attempt": 2, "data": {"text": "a\nb"}})
    lines = frame.split("\n")
    assert lines[0] == "id: 1-0" and lines[1] == "event: token" and frame.endswith("\n\n")
    payload = json.loads("".join(line[6:] for line in lines if line.startswith("data: ")))
    assert payload == {"attempt": 2, "text": "a\nb"}


def _sse(chunks, finish="stop"):
    body = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': c}, 'finish_reason': None}]})}\n\n"
                   for c in chunks)
    body += f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}\n\n"
    body += f"data: {json.dumps({'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}})}\n\n"
    return body + "data: [DONE]\n\n"


def _collect(model):
    async def run():
        return [d async for d in model.stream([{"role": "user", "content": "q"}], {"type": "object"})]

    return asyncio.run(run())


def test_streaming_client_sends_the_same_settings_and_yields_deltas():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=_sse(["{\"ans", "wer\": \"x\"}"]))

    model = StreamingChatModel("m", transport=httpx.MockTransport(handler))
    assert "".join(_collect(model)) == '{"answer": "x"}'
    assert seen["stream"] is True and seen["temperature"] == 0 and seen["seed"] == 0
    assert seen["reasoning_effort"] == "none" and seen["response_format"]["json_schema"]["strict"] is True
    assert model.last_call["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}
    assert model.last_call["finish_reason"] == "stop" and model.last_call["first_token_ms"] is not None


def test_cut_off_output_is_a_result_not_a_retry():
    model = StreamingChatModel("m", transport=httpx.MockTransport(lambda r: httpx.Response(200, text=_sse(["{"],
                                                                                                         "length"))))
    with pytest.raises(ProviderInvalid, match="OUTPUT_TRUNCATED"):
        _collect(model)


@pytest.mark.parametrize("response", [httpx.Response(503, text="busy"), httpx.ConnectError("refused")])
def test_an_unreachable_model_is_transient(response):
    def handler(request):
        if isinstance(response, Exception):
            raise response
        return response

    with pytest.raises(ModelUnavailable):
        _collect(StreamingChatModel("m", transport=httpx.MockTransport(handler)))


def test_streaming_client_talks_to_loopback_only():
    with pytest.raises(ValueError):
        StreamingChatModel("m", base_url="http://10.0.0.5:11434")
