"""Desk worker: takes jobs from RabbitMQ, gets evidence over gRPC, streams the answer, checks it, caches it.

Delivery is at-least-once, so every step is written to be safe to repeat:

* ``prefetch`` bounds how many unacknowledged jobs this worker holds, which is how many model calls it runs at
  once. A local model serves a few requests in parallel at best; more would only queue inside the model server,
  where nobody can see or cancel them. Extra jobs wait in RabbitMQ, visible as queue depth.
* A job is acknowledged only after its result is cached, the job record says ``done`` and the ``final`` event is
  written. A worker killed before that leaves the message unacknowledged; RabbitMQ gives it to another worker
  when the dead worker's connection closes. That worker counts the delivery (``attempt`` 2), checks the cache
  first (the first worker may have finished the expensive part) and otherwise starts over.
* A message for a job already ``done`` or ``failed`` (a duplicate delivery) is acknowledged and ignored.
* Transient failures (retrieval server unavailable or past its deadline, model server unreachable) do not block
  the worker with a sleep: the job is republished to ``desk.ask.retry``, whose message TTL dead-letters it back
  to ``desk.ask`` after ``retry_delay_ms``. After ``max_attempts`` deliveries it goes to ``desk.ask.dead`` with
  the reason, and the reader gets an ``error`` event.
* What is final, not transient: bad input, unknown caller, an unusable model output (ANSWER_ERROR — at
  temperature 0 the same input would give the same output, so retrying only burns time).
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
import traceback
import uuid
from typing import Any

import grpc

from ..analysis_provider import ProviderInvalid
from ..cited_answer import ANSWER_SCHEMA, CitedAnswerer, build_messages, prompt_sha256
from ..contracts import ContextHit
from . import keys
from .events import AnswerFieldExtractor, emit
from .model import ModelUnavailable, StreamingChatModel
from .proto import desk_pb2, desk_pb2_grpc
from .retrieval_server import CHANNEL_OPTIONS, SUBJECT_HEADER

TRANSIENT_GRPC = {grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED,
                  grpc.StatusCode.RESOURCE_EXHAUSTED}
CACHEABLE = {"ANSWERED", "NOT_FOUND", "NO_HITS", "UNSUPPORTED"}

# Delete the in-flight marker only if it still names this job (a later job may own the key by now).
RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class Transient(Exception):
    """Worth trying again later: the failure is in a dependency, not in the job."""


class Final(Exception):
    """Trying again cannot help: the job itself is not answerable as asked."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class _Replay:
    """Hands the already-streamed text to CitedAnswerer, so the streamed answer gets exactly its checks."""

    def __init__(self, model_id: str, raw: str | None, error: Exception | None = None):
        self.model_id, self.raw, self.error = model_id, raw, error

    def complete(self, messages, schema):  # noqa: ARG002 - the call already happened, streamed
        if self.error is not None:
            raise self.error
        return self.raw


async def declare_topology(channel: Any, config: Any) -> Any:
    """Queues, idempotently. Arguments must stay identical across processes or RabbitMQ refuses the redeclare."""
    await channel.declare_queue(config.dead_queue, durable=True)
    await channel.declare_queue(config.retry_queue, durable=True, arguments={
        "x-message-ttl": config.retry_delay_ms,
        "x-dead-letter-exchange": "", "x-dead-letter-routing-key": config.queue,
    })
    return await channel.declare_queue(config.queue, durable=True, arguments={
        "x-max-length": config.max_queue, "x-overflow": "reject-publish",
        "x-dead-letter-exchange": "", "x-dead-letter-routing-key": config.dead_queue,
    })


def _hit(pb: Any) -> ContextHit:
    return ContextHit(id=pb.id, version=pb.version, content=pb.content, source_hash=pb.source_hash,
                      classification=pb.classification, status=pb.status)


def _citation(pb: Any) -> dict[str, Any]:
    return {"id": pb.id, "doc": pb.doc or None, "company": pb.company or None, "kind": pb.kind or None,
            "page": pb.page or None, "bbox": list(pb.bbox) or None, "snippet": pb.snippet}


class Worker:
    def __init__(self, config: Any, redis: Any, stub: Any, model: StreamingChatModel, *,
                 worker_id: str | None = None, clock=time.time):
        self.config, self.redis, self.stub, self.model = config, redis, stub, model
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:6]}"
        self.clock = clock
        self.channel: Any = None
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ one job
    async def process(self, job: dict[str, Any], attempt: int) -> dict[str, Any]:
        """Evidence → streamed answer → checks. Returns the result in ReportDesk.ask's shape plus ``status``."""
        job_id, question = job["job_id"], job["question"]
        cached = await self.redis.get(keys.cache(job["cache_key"]))
        if cached is not None:
            return json.loads(cached) | {"cached": True}
        try:
            # wait_for_ready: while the retrieval server restarts, wait for it (up to the deadline) instead of
            # failing at once with UNAVAILABLE; an outage longer than the deadline becomes a transient retry.
            reply = await self.stub.Search(
                desk_pb2.SearchRequest(question=question, gate=job["gate"], top_k=job["top_k"]),
                timeout=self.config.grpc_deadline_s, metadata=((SUBJECT_HEADER, job["subject"]),),
                wait_for_ready=True)
        except grpc.aio.AioRpcError as error:
            if error.code() in TRANSIENT_GRPC:
                raise Transient(f"retrieval {error.code().name}: {error.details()}") from error
            raise Final(f"RETRIEVAL_{error.code().name}", error.details() or "") from error
        kept = [_citation(h.citation) for h in reply.kept]
        dropped = [_citation(c) for c in reply.dropped]
        timings = {"retrieval_ms": round(reply.retrieval_ms, 1)}
        if reply.gate_ms:
            timings["gate_ms"] = round(reply.gate_ms, 1)
        await emit(self.redis, job_id, "retrieved", {"kept": kept, "dropped": dropped, "gate": reply.gate_status,
                                                     "mode": reply.mode, "timings": timings},
                   attempt=attempt, ttl_s=self.config.job_ttl_s)
        hits = [_hit(h) for h in reply.kept]
        answered = await self._answer(job_id, question, hits, attempt, timings)
        by_id = {h.id: c for h, c in zip(reply.kept, kept, strict=True)}
        result = {
            "question": question, "mode": reply.mode, "gate": reply.gate_status,
            "found": bool(answered.get("found")) and bool(answered.get("citations")),
            "answer": answered.get("answer", ""),
            "citations": [by_id[c] for c in answered.get("citations", []) if c in by_id],
            "kept": kept, "dropped": dropped, "answerer": answered.get("model") or "none",
            "status": answered["status"], "timings": timings, "index_version": reply.index_version,
            "rejected_citations": answered.get("rejected_citations", []),
        }
        if reply.index_version != job["index_version"]:
            # The index changed between enqueue and now: cache under the key of the index actually used.
            result["cache_key"] = keys.cache_key(
                subject=job["subject"], index_version=reply.index_version, model=job["model"],
                prompt_sha256=job["prompt_sha256"], gate=job["gate"], top_k=job["top_k"], question=question)
        return result

    async def _answer(self, job_id: str, question: str, hits: list[ContextHit], attempt: int,
                      timings: dict[str, float]) -> dict[str, Any]:
        if not hits:  # nothing to cite: "not in the reports" without calling the model
            return CitedAnswerer(_Replay(self.model.model_id, None)).answer(question, hits)
        extractor = AnswerFieldExtractor()
        pending, last_flush = "", time.perf_counter()
        started = time.perf_counter()
        error: Exception | None = None
        try:
            async for delta in self.model.stream(build_messages(question, hits), ANSWER_SCHEMA):
                pending += extractor.feed(delta)
                if pending and (time.perf_counter() - last_flush) * 1000 >= self.config.token_flush_ms:
                    await emit(self.redis, job_id, "token", {"text": pending}, attempt=attempt,
                               ttl_s=self.config.job_ttl_s)
                    pending, last_flush = "", time.perf_counter()
        except ModelUnavailable as exc:
            raise Transient(f"model unavailable: {exc}") from exc
        except ProviderInvalid as exc:
            error = exc
        if pending:
            await emit(self.redis, job_id, "token", {"text": pending}, attempt=attempt, ttl_s=self.config.job_ttl_s)
        timings["answer_ms"] = round((time.perf_counter() - started) * 1000, 1)
        if self.model.last_call.get("first_token_ms") is not None:
            timings["first_token_ms"] = self.model.last_call["first_token_ms"]
        return CitedAnswerer(_Replay(self.model.model_id, extractor.raw, error)).answer(question, hits)

    # ------------------------------------------------------------------ delivery
    async def handle(self, message: Any) -> None:
        job = json.loads(message.body)
        job_id = job["job_id"]
        record = keys.job(job_id)
        status = await self.redis.hget(record, "status")
        status = status.decode() if isinstance(status, bytes) else status
        if status in {"done", "failed"}:
            await message.ack()  # duplicate delivery of a finished job
            return
        if job.get("model") != self.model.model or job.get("prompt_sha256") != prompt_sha256():
            await self._fail(job, 0, "CONFIG_MISMATCH",
                             "the job was keyed for another model or prompt than this worker runs")
            await message.reject(requeue=False)  # → desk.ask.dead, for a person to look at
            return
        attempt = await self.redis.hincrby(record, "deliveries", 1)
        now = self.clock()
        fields = {"status": "running", "worker": self.worker_id, "started_at": now}
        if attempt == 1:
            fields["queue_ms"] = round((now - float(job["enqueued_at"])) * 1000, 1)
        await self.redis.hset(record, mapping=fields)
        await emit(self.redis, job_id, "started", {"worker": self.worker_id}, attempt=attempt,
                   ttl_s=self.config.job_ttl_s)
        try:
            result = await self.process(job, attempt)
        except Transient as exc:
            if attempt >= self.config.max_attempts:
                await self._fail(job, attempt, "GAVE_UP", f"{exc} (after {attempt} attempts)")
                await self._publish(self.config.dead_queue, job, reason=str(exc))
            else:
                await emit(self.redis, job_id, "retrying", {"reason": str(exc),
                                                            "delay_ms": self.config.retry_delay_ms},
                           attempt=attempt, ttl_s=self.config.job_ttl_s)
                await self.redis.hset(record, "status", "retrying")
                await self._publish(self.config.retry_queue, job)
            await message.ack()
            return
        except Final as exc:
            await self._fail(job, attempt, exc.code, exc.message)
            await message.ack()
            return
        except Exception as exc:  # a bug: keep the message for inspection instead of retrying forever
            traceback.print_exc()
            await self._fail(job, attempt, "WORKER_ERROR", repr(exc))
            await message.reject(requeue=False)
            return
        await self._finish(job, attempt, result)
        await message.ack()

    async def _finish(self, job: dict[str, Any], attempt: int, result: dict[str, Any]) -> None:
        job_id = job["job_id"]
        queue_ms = await self.redis.hget(keys.job(job_id), "queue_ms")
        if queue_ms is not None and not result.get("cached"):
            result["timings"]["queue_ms"] = float(queue_ms)
        if result.get("status") in CACHEABLE and not result.get("cached"):
            key = result.pop("cache_key", job["cache_key"])
            await self.redis.set(keys.cache(key), json.dumps(result, ensure_ascii=False),
                                 ex=self.config.cache_ttl_s)
        result.pop("cache_key", None)
        await self.redis.hset(keys.job(job_id), mapping={"status": "done", "finished_at": self.clock(),
                                                         "result": json.dumps(result, ensure_ascii=False)})
        await emit(self.redis, job_id, "final", result, attempt=attempt, ttl_s=self.config.job_ttl_s)
        await self.redis.eval(RELEASE, 1, keys.inflight(job["cache_key"]), job_id)

    async def _fail(self, job: dict[str, Any], attempt: int, code: str, message: str) -> None:
        job_id = job["job_id"]
        await self.redis.hset(keys.job(job_id), mapping={"status": "failed", "finished_at": self.clock(),
                                                         "error": json.dumps({"code": code, "message": message})})
        await emit(self.redis, job_id, "error", {"code": code, "message": message}, attempt=attempt,
                   ttl_s=self.config.job_ttl_s)
        await self.redis.eval(RELEASE, 1, keys.inflight(job["cache_key"]), job_id)

    async def _publish(self, queue: str, job: dict[str, Any], reason: str | None = None) -> None:
        import aio_pika

        body = dict(job)
        if reason:
            body["dead_reason"] = reason
        await self.channel.default_exchange.publish(
            aio_pika.Message(json.dumps(body, ensure_ascii=False).encode(), message_id=job["job_id"],
                             delivery_mode=aio_pika.DeliveryMode.PERSISTENT, content_type="application/json"),
            routing_key=queue)

    # ------------------------------------------------------------------ loop
    async def run(self, stop: asyncio.Event | None = None) -> None:
        import aio_pika

        stop = stop or asyncio.Event()
        connection = await aio_pika.connect_robust(self.config.amqp_url)
        async with connection:
            self.channel = await connection.channel(publisher_confirms=True)
            await self.channel.set_qos(prefetch_count=self.config.prefetch)
            queue = await declare_topology(self.channel, self.config)

            async def on_message(message):
                task = asyncio.current_task()
                self._tasks.add(task)
                try:
                    await self.handle(message)
                finally:
                    self._tasks.discard(task)

            tag = await queue.consume(on_message)
            print(f"{self.worker_id}: consuming {self.config.queue} (prefetch {self.config.prefetch})", flush=True)
            await stop.wait()
            # Graceful stop: take no new jobs, let running ones finish; anything still unacknowledged when the
            # connection closes goes back to the queue for another worker.
            await queue.cancel(tag)
            if self._tasks:
                await asyncio.wait(set(self._tasks), timeout=30)


async def run_worker(config: Any) -> None:
    import redis.asyncio as aioredis

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    redis = aioredis.Redis.from_url(config.redis_url)
    channel = grpc.aio.insecure_channel(config.grpc_target, options=CHANNEL_OPTIONS)
    model = StreamingChatModel(config.model, config.ollama_url, timeout=config.model_timeout_s)
    try:
        await Worker(config, redis, desk_pb2_grpc.ReportRetrievalStub(channel), model).run(stop)
    finally:
        await model.aclose()
        await channel.close()
        await redis.aclose()
