"""A stand-in for the local model server, for tests and the service benchmark: NOT a model.

It speaks the part of Ollama's OpenAI-compatible API the worker uses (POST /v1/chat/completions with
``stream: true``) and answers deterministically from the passages in the prompt: it cites the first passage and
quotes the start of it. Latency per chunk, failure modes and a call counter are settable, so tests can check what
the service does around a slow, failing or truncating model without a GPU. Numbers measured with it say how the
serving layer behaves; they say nothing about answer quality.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


@dataclass
class FakeModelState:
    chunk_delay_s: float = 0.02
    chunk_chars: int = 6
    first_token_delay_s: float = 0.0
    mode: str = "ok"  # ok | down (HTTP 503) | truncate (finish_reason length) | garbage (not JSON) | made_up
    down_calls: int = 0  # answer HTTP 503 to this many calls first, then behave as ``mode`` says
    calls: int = 0
    concurrent: int = 0
    peak_concurrent: int = 0
    questions: list[str] = field(default_factory=list)


def answer_for(messages: list[dict[str, str]], made_up: bool = False) -> str:
    user = messages[-1]["content"]
    question = user.split("\n", 1)[0].removeprefix("Question: ")
    match = re.search(r"^\[([^\]]+)\]\n(.*)$", user, flags=re.MULTILINE)
    if match is None or "absent" in question.lower():
        return json.dumps({"answer": "", "citations": [], "found": False})
    passage_id, first_line = match.group(1), match.group(2)
    body = user[match.end():].split("\n\n[", 1)[0].strip() or first_line
    text = f"根据年报：{body[:48]}"
    cited = ["not-a-given-passage"] if made_up else [passage_id]
    return json.dumps({"answer": text, "citations": cited, "found": True}, ensure_ascii=False)


def create_fake_model_app(state: FakeModelState) -> Any:
    app = FastAPI()

    @app.get("/api/tags")
    async def tags():
        return {"models": [{"name": "fake-answerer:latest"}]}

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        state.calls += 1
        state.questions.append(body["messages"][-1]["content"].split("\n", 1)[0])
        if state.mode == "down" or state.down_calls > 0:
            state.down_calls = max(0, state.down_calls - 1)
            return JSONResponse({"error": "model unavailable"}, status_code=503)
        text = (answer_for(body["messages"], made_up=state.mode == "made_up") if state.mode != "garbage"
                else "I think the answer is 42")

        async def chunks():
            state.concurrent += 1
            state.peak_concurrent = max(state.peak_concurrent, state.concurrent)
            try:
                await asyncio.sleep(state.first_token_delay_s)
                for start in range(0, len(text), state.chunk_chars):
                    piece = text[start:start + state.chunk_chars]
                    chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    await asyncio.sleep(state.chunk_delay_s)
                finish = "length" if state.mode == "truncate" else "stop"
                yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})}\n\n"
                usage = {"prompt_tokens": len(body["messages"][-1]["content"]) // 4, "completion_tokens": len(text) // 4}
                yield f"data: {json.dumps({'choices': [], 'usage': usage})}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                state.concurrent -= 1

        return StreamingResponse(chunks(), media_type="text/event-stream")

    return app
