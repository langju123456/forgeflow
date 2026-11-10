"""Gateway: accepts questions, answers from cache, joins duplicates, enqueues the rest, streams events as SSE.

It never touches the index or the model, so it stays responsive however slow answering is:

    POST /v2/reports/ask       → 202 {job_id, state: cached | joined | queued, events}
    GET  /v2/jobs/{id}/events  → text/event-stream, resumable with Last-Event-ID
    GET  /v2/jobs/{id}         → the job record (status, result or error, deliveries)
    GET  /v2/reports           → catalogue and readiness (gRPC Info) plus queue depth and worker count
    GET  /v2/reports/{doc}/pages/{page}.png → the cited page (gRPC PageImage)

Three ways a question avoids a model call, cheapest first:
1. **cache**: the same question, caller, index and answer settings were answered before → the final event is
   written at once (Redis GET, about a millisecond instead of seconds to tens of seconds);
2. **join**: the same question is being answered right now → return that job's id (Redis ``SET NX`` on the
   in-flight key decides atomically which request creates the job; ten identical requests make one model call);
3. **backpressure**: the queue is full (RabbitMQ ``x-max-length`` with ``reject-publish``) → the broker nacks
   the publish under publisher confirms and the gateway answers 429 with Retry-After, instead of accepting work
   it cannot finish in reasonable time.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import grpc
from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..cited_answer import prompt_sha256
from . import keys
from .config import DeskConfig
from .events import TERMINAL, decode_entry, emit, sse_frame
from .proto import desk_pb2, desk_pb2_grpc
from .retrieval_server import CHANNEL_OPTIONS, SUBJECT_HEADER
from .worker import RELEASE, declare_topology

HEARTBEAT_MS = 15000
MAX_STREAM_S = 900


def _text(value: Any) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


class Gateway:
    def __init__(self, config: DeskConfig, redis: Any, channel: Any, stub: Any):
        self.config, self.redis, self.channel, self.stub = config, redis, channel, stub
        self.queue: Any = None

    async def setup(self) -> None:
        self.queue = await declare_topology(self.channel, self.config)

    def _metadata(self):
        return ((SUBJECT_HEADER, self.config.subject),)

    async def ask(self, question: str, gate: bool) -> tuple[int, dict[str, Any]]:
        question = keys.normalize_question(question)
        if not question:
            return 422, {"code": "QUERY_REQUIRED", "message": "Ask a question"}
        index_version = _text(await self.redis.get(keys.INDEX_VERSION))
        if not index_version:
            return 503, {"code": "DESK_NOT_READY", "message": "The retrieval server is still loading the reports"}
        prompt = prompt_sha256()
        key = keys.cache_key(subject=self.config.subject, index_version=index_version, model=self.config.model,
                             prompt_sha256=prompt, gate=gate, top_k=self.config.top_k, question=question)
        job_id = uuid.uuid4().hex
        now = time.time()

        cached = await self.redis.get(keys.cache(key))
        if cached is not None:
            result = json.loads(cached) | {"cached": True}
            await self.redis.hset(keys.job(job_id), mapping={
                "status": "done", "question": question, "created_at": now, "finished_at": now, "cached": 1,
                "result": json.dumps(result, ensure_ascii=False)})
            await self.redis.expire(keys.job(job_id), self.config.job_ttl_s)
            await emit(self.redis, job_id, "final", result, ttl_s=self.config.job_ttl_s)
            return 200, self._accepted(job_id, "cached")

        # One request per key creates the job; the others join it. NX makes that decision atomic in Redis.
        for _ in range(3):
            if await self.redis.set(keys.inflight(key), job_id, nx=True, ex=self.config.job_ttl_s):
                break
            other = _text(await self.redis.get(keys.inflight(key)))
            if other and await self.redis.exists(keys.job(other)):
                return 202, self._accepted(other, "joined")
            await asyncio.sleep(0.01)  # the key expired between SET and GET; try again
        else:
            return 503, {"code": "BUSY", "message": "Could not register the question; try again"}

        job = {"job_id": job_id, "question": question, "gate": bool(gate), "top_k": self.config.top_k,
               "subject": self.config.subject, "model": self.config.model, "prompt_sha256": prompt,
               "index_version": index_version, "cache_key": key, "enqueued_at": now}
        await self.redis.hset(keys.job(job_id), mapping={"status": "queued", "question": question,
                                                         "created_at": now, "cache_key": key})
        await self.redis.expire(keys.job(job_id), self.config.job_ttl_s)
        await emit(self.redis, job_id, "queued", {"cache_key": key}, ttl_s=self.config.job_ttl_s)
        try:
            await self._publish(job)
        except Exception as exc:  # nack (queue full) or broker unreachable: nothing was accepted
            from aiormq.exceptions import DeliveryError

            full = isinstance(exc, DeliveryError)
            code = "QUEUE_FULL" if full else "BROKER_UNAVAILABLE"
            await self.redis.hset(keys.job(job_id), mapping={"status": "failed", "error": json.dumps({"code": code})})
            await emit(self.redis, job_id, "error", {"code": code, "message": str(exc) or code},
                       ttl_s=self.config.job_ttl_s)
            await self.redis.eval(RELEASE, 1, keys.inflight(key), job_id)
            return (429 if full else 503), {"code": code, "message": "Too many questions waiting; try again soon"
                                            if full else "The job queue is unreachable"}
        return 202, self._accepted(job_id, "queued")

    async def _publish(self, job: dict[str, Any]) -> None:
        import aio_pika

        await self.channel.default_exchange.publish(
            aio_pika.Message(json.dumps(job, ensure_ascii=False).encode(), message_id=job["job_id"],
                             delivery_mode=aio_pika.DeliveryMode.PERSISTENT, content_type="application/json"),
            routing_key=self.config.queue, mandatory=True)

    @staticmethod
    def _accepted(job_id: str, state: str) -> dict[str, Any]:
        return {"job_id": job_id, "state": state, "events": f"/v2/jobs/{job_id}/events", "job": f"/v2/jobs/{job_id}"}

    async def job(self, job_id: str) -> dict[str, Any] | None:
        raw = await self.redis.hgetall(keys.job(job_id))
        if not raw:
            return None
        record = {_text(k): _text(v) for k, v in raw.items()}
        for field in ("result", "error"):
            if record.get(field):
                record[field] = json.loads(record[field])
        record["deliveries"] = int(record.get("deliveries") or 0)
        record.pop("cache_key", None)
        return {"job_id": job_id, **record}

    async def events(self, job_id: str, last_id: str | None, is_disconnected) -> Any:
        """Replay the job's events after ``last_id``, then follow new ones until ``final`` or ``error``."""
        stream = keys.events(job_id)
        cursor = last_id or "0-0"
        deadline = time.monotonic() + MAX_STREAM_S
        yield "retry: 2000\n\n"  # EventSource reconnect delay; it will send Last-Event-ID
        while time.monotonic() < deadline:
            if await is_disconnected():
                return
            batch = await self.redis.xread({stream: cursor}, block=HEARTBEAT_MS, count=200)
            if not batch:
                yield ": keep-alive\n\n"
                continue
            for _, entries in batch:
                for entry_id, fields in entries:
                    event = decode_entry(entry_id, fields)
                    cursor = event["id"]
                    yield sse_frame(event)
                    if event["type"] in TERMINAL:
                        return

    async def info(self) -> dict[str, Any]:
        try:
            reply = await self.stub.Info(desk_pb2.InfoRequest(), timeout=5, metadata=self._metadata())
        except grpc.aio.AioRpcError as error:
            return {"reports": [], "status": {"state": "offline", "detail": f"检索服务不可用（{error.code().name}）",
                                              "embedded": 0, "rows": 0},
                    "mode": "lexical", "gate_available": False, "answer_available": False, "service": True}
        # A passive declare through aio-pika's robust channel reports 0 consumers; the raw channel reports the truth.
        underlay = await self.channel.get_underlay_channel()
        declared = await underlay.queue_declare(self.config.queue, passive=True)
        return {
            "reports": json.loads(reply.reports_json or "[]"),
            "status": {"state": reply.state, "detail": reply.detail, "embedded": 0, "rows": reply.rows},
            "mode": reply.mode or "lexical", "gate_available": reply.gate_available, "answer_available": True,
            "service": True, "index_version": reply.index_version, "model": self.config.model,
            "queue": {"waiting": declared.message_count, "workers": declared.consumer_count,
                      "max": self.config.max_queue},
        }

    async def page_png(self, doc: str, page: int, box: list[float] | None) -> tuple[int, bytes | dict]:
        try:
            reply = await self.stub.PageImage(desk_pb2.PageImageRequest(doc=doc, page=page, bbox=box or []),
                                              timeout=20, metadata=self._metadata())
        except grpc.aio.AioRpcError as error:
            status = 404 if error.code() == grpc.StatusCode.NOT_FOUND else 503
            return status, {"code": error.code().name, "message": error.details()}
        return 200, reply.png


def mount_desk_gateway(app: Any, gateway_factory) -> None:
    """Add the /v2 routes. ``gateway_factory()`` returns the Gateway (built in the app's lifespan).

    FastAPI reads these handlers' annotations from this module's globals (``from __future__ import annotations``
    turns them into strings), which is why Body, Request and the responses are imported at module level.
    """
    @app.post("/v2/reports/ask")
    async def ask(question: str = Body(..., embed=True), gate: bool = Body(False, embed=True)):
        status, body = await gateway_factory().ask(question, gate)
        if status >= 400:
            headers = {"Retry-After": "5"} if status in {429, 503} else None
            return JSONResponse({"detail": body}, status_code=status, headers=headers)
        return JSONResponse(body, status_code=status)

    @app.get("/v2/jobs/{job_id}")
    async def job(job_id: str):
        record = await gateway_factory().job(job_id)
        if record is None:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "No such job (or it expired)"})
        return record

    @app.get("/v2/jobs/{job_id}/events")
    async def events(job_id: str, request: Request):
        gateway = gateway_factory()
        if not await gateway.redis.exists(keys.events(job_id)):
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "No such job (or it expired)"})
        last_id = request.headers.get("last-event-id") or request.query_params.get("after")
        return StreamingResponse(gateway.events(job_id, last_id, request.is_disconnected),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/v2/reports")
    async def reports():
        return await gateway_factory().info()

    @app.get("/v2/reports/{doc}/pages/{page}.png")
    async def page_png(doc: str, page: int, box: str | None = None):
        try:
            parsed = [float(x) for x in box.split(",")] if box else None
        except ValueError:
            raise HTTPException(422, detail={"code": "BAD_BOX", "message": "box is x0,top,x1,bottom"}) from None
        status, body = await gateway_factory().page_png(doc, page, parsed)
        if status != 200:
            raise HTTPException(status, detail=body)
        return Response(body, media_type="image/png")


def create_app(config: DeskConfig, *, static_dir: Any = None) -> Any:
    import aio_pika
    import redis.asyncio as aioredis
    from fastapi import FastAPI

    state: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app):
        redis = aioredis.Redis.from_url(config.redis_url)
        connection = await aio_pika.connect_robust(config.amqp_url)
        channel = await connection.channel(publisher_confirms=True)
        grpc_channel = grpc.aio.insecure_channel(config.grpc_target, options=CHANNEL_OPTIONS)
        gateway = Gateway(config, redis, channel, desk_pb2_grpc.ReportRetrievalStub(grpc_channel))
        await gateway.setup()
        state["gateway"] = gateway
        try:
            yield
        finally:
            await grpc_channel.close()
            await connection.close()
            await redis.aclose()

    app = FastAPI(title="ForgeFlow desk service (DEV_ONLY)", lifespan=lifespan)
    mount_desk_gateway(app, lambda: state["gateway"])
    if static_dir is not None and static_dir.exists():
        from fastapi.responses import RedirectResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/workbench", StaticFiles(directory=static_dir, html=True), name="workbench")

        @app.get("/")
        async def root():
            return RedirectResponse("/workbench/#reports")
    return app
