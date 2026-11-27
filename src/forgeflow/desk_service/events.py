"""Job events: written to a Redis Stream by the worker, read by the gateway and sent to the browser as SSE.

Why a Redis Stream and not RabbitMQ for these: RabbitMQ carries *work* (one job, one consumer, ack, redelivery).
Events are a *log* per job that any number of readers replay from any position: the browser that started the job,
a second browser that asked the same question and joined it, and a browser that reconnects after a dropped
connection and sends ``Last-Event-ID``. A stream entry id is that position. A queue message, once consumed, is gone.

Event types, in the order a worker writes them for one attempt:

    queued      gateway accepted the job (or ``cached``: the final event follows immediately)
    started     a worker took it; ``attempt`` counts deliveries, so a redelivered job starts again at 2, 3, ...
    retrieved   the evidence step finished: kept hits (citations only), dropped hits, gate status, timings
    token       a chunk of the model's answer text as it is generated: a DRAFT, not checked yet
    final       the checked result (the same shape as ReportDesk.ask); the stream ends here
    retrying    a transient failure; the job waits in the retry queue and a new ``started`` will follow
    error       the job failed for good; the stream ends here

The draft is extracted from the model's JSON as it streams (``AnswerFieldExtractor``) so the reader sees words,
not braces. It can still be withdrawn: the final event may say "not found" or drop citations, because the
checks in forgeflow.cited_answer run only on the complete output. Readers must replace the draft with ``final``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import keys

TERMINAL = frozenset({"final", "error"})
STREAM_MAXLEN = 5000


async def emit(redis: Any, job_id: str, kind: str, data: dict[str, Any] | None = None, *, attempt: int = 0,
               ttl_s: int = 3600) -> str:
    """Append one event; the stream expires with the job so finished jobs do not pile up in Redis."""
    name = keys.events(job_id)
    payload = {"type": kind, "attempt": str(attempt), "data": json.dumps(data or {}, ensure_ascii=False)}
    event_id = await redis.xadd(name, payload, maxlen=STREAM_MAXLEN, approximate=True)
    await redis.expire(name, ttl_s)
    return event_id.decode() if isinstance(event_id, bytes) else event_id


def decode_entry(entry_id: Any, fields: dict) -> dict[str, Any]:
    def text(value: Any) -> str:
        return value.decode() if isinstance(value, bytes) else str(value)

    flat = {text(k): text(v) for k, v in fields.items()}
    return {"id": text(entry_id), "type": flat["type"], "attempt": int(flat.get("attempt", "0")),
            "data": json.loads(flat.get("data", "{}"))}


def sse_frame(event: dict[str, Any]) -> str:
    """One Server-Sent Events frame. ``id`` lets EventSource resume with Last-Event-ID after a reconnect."""
    body = json.dumps({"attempt": event["attempt"], **event["data"]}, ensure_ascii=False)
    lines = [f"id: {event['id']}", f"event: {event['type']}"]
    lines += [f"data: {line}" for line in body.split("\n")]
    return "\n".join(lines) + "\n\n"


_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class AnswerFieldExtractor:
    """Feed the model's raw JSON chunk by chunk; get back the newly completed characters of the "answer" value.

    The schema lists ``answer`` first and grammar-constrained decoding keeps that order, so the answer text streams
    before the citations. An escape split across two chunks (``\\`` at the end of one, ``n`` at the start of the
    next, or half of a ``\\uXXXX``) is held back until it is complete, so no reader ever sees a broken character.
    """

    def __init__(self) -> None:
        self.raw = ""
        self.pos: int | None = None  # index in raw of the next undecoded answer character
        self.done = False
        self.text = ""

    def feed(self, chunk: str) -> str:
        self.raw += chunk
        if self.done:
            return ""
        if self.pos is None:
            match = _ANSWER_KEY.search(self.raw)
            if match is None:
                return ""
            self.pos = match.end()
        out: list[str] = []
        raw, pos = self.raw, self.pos
        while pos < len(raw):
            char = raw[pos]
            if char == '"':
                self.done = True
                break
            if char != "\\":
                out.append(char)
                pos += 1
                continue
            if pos + 1 >= len(raw):
                break  # escape continues in the next chunk
            marker = raw[pos + 1]
            if marker in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[marker])
                pos += 2
                continue
            if marker != "u":
                pos += 2  # invalid escape; strict parsing of the full output will reject it anyway
                continue
            if pos + 6 > len(raw):
                break
            code = int(raw[pos + 2:pos + 6], 16) if re.fullmatch(r"[0-9a-fA-F]{4}", raw[pos + 2:pos + 6]) else None
            if code is None:
                pos += 6
                continue
            if 0xD800 <= code <= 0xDBFF:  # high surrogate: needs its low half
                if pos + 12 > len(raw):
                    break
                try:
                    out.append(json.loads(f'"{raw[pos:pos + 12]}"'))
                except ValueError:
                    pass
                pos += 12
                continue
            out.append(chr(code))
            pos += 6
        self.pos = pos
        new = "".join(out)
        self.text += new
        return new
