"""Bring up the whole desk service on loopback for live tests and the benchmark.

Real: Redis, RabbitMQ (both must already run locally), the gRPC retrieval server over the synthetic PDFs from
scripts/make_financial_pdfs.py, workers as asyncio tasks or subprocesses, the gateway behind uvicorn.
Stand-in: the model (forgeflow.desk_service.testing), so no GPU is needed.

Each Harness uses its own queue names and Redis key prefix-free database number so runs do not see each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import grpc
import httpx

ROOT = Path(__file__).resolve().parents[1]
REPORTS = {
    "cc_form10k_2025_en": ("Example Co.", "Form 10-K", "en", 2),
    "cc_annual_report_2025_zh": ("示例公司", "年度报告", "zh", 4),
}


def services_up(redis_url: str = "redis://127.0.0.1:6379/0", amqp_port: int = 5672) -> bool:
    try:
        import redis

        redis.Redis.from_url(redis_url, socket_timeout=1).ping()
        with socket.create_connection(("127.0.0.1", amqp_port), timeout=1):
            return True
    except Exception:  # noqa: BLE001 - any failure means "not available here"
        return False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_reports(folder: Path) -> Path:
    spec = importlib.util.spec_from_file_location("make_financial_pdfs", ROOT / "scripts/make_financial_pdfs.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(folder)
    entries = []
    for key, (company, document, language, pages) in REPORTS.items():
        data = (folder / f"{key}.pdf").read_bytes()
        entries.append({"key": key, "company": company, "document": document, "language": language, "pages": pages,
                        "url": "-", "sha256": hashlib.sha256(data).hexdigest()})
    sources = folder / "sources.json"
    sources.write_text(json.dumps({"reports": entries}))
    return sources


class Uvicorn:
    def __init__(self, app, port: int):
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class Harness:
    def __init__(self, tmp: Path, *, max_queue: int = 200, prefetch: int = 2, retry_delay_ms: int = 300,
                 max_attempts: int = 3, redis_db: int = 9):
        from forgeflow.desk_service.config import DeskConfig
        from forgeflow.desk_service.testing import FakeModelState

        self.tmp = tmp
        suffix = uuid.uuid4().hex[:8]
        self.model_port, self.gateway_port, self.grpc_port = free_port(), free_port(), free_port()
        self.config = DeskConfig(
            redis_url=f"redis://127.0.0.1:6379/{redis_db}", grpc_target=f"127.0.0.1:{self.grpc_port}",
            gateway_port=self.gateway_port, ollama_url=f"http://127.0.0.1:{self.model_port}", model="fake-answerer",
            reports_dir=str(tmp / "reports"), work_dir=str(tmp / "work"), prefetch=prefetch, max_queue=max_queue,
            max_attempts=max_attempts, retry_delay_ms=retry_delay_ms, grpc_deadline_s=10, model_timeout_s=30,
            token_flush_ms=30, queue=f"desk.ask.test.{suffix}")
        self.model = FakeModelState()
        self._uvicorns: list[Uvicorn] = []
        self.grpc_server = None
        self.desk = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._worker_thread: threading.Thread | None = None
        self._worker_stops: list[asyncio.Event] = []
        self.processes: list[subprocess.Popen] = []

    # ------------------------------------------------------------------ parts
    def start_model(self) -> None:
        from forgeflow.desk_service.testing import create_fake_model_app

        server = Uvicorn(create_fake_model_app(self.model), self.model_port)
        server.start()
        self._uvicorns.append(server)

    def start_retrieval(self, *, load: bool = True) -> None:
        import redis

        from forgeflow.desk_service.retrieval_server import RetrievalServicer, build_server, load_and_publish
        from forgeflow.report_desk import ReportDesk

        redis.Redis.from_url(self.config.redis_url).flushdb()
        folder = Path(self.config.reports_dir)
        folder.mkdir(parents=True, exist_ok=True)
        sources = make_reports(folder)
        # Port 9 has no Ollama: lexical retrieval, no gate model; deterministic and fast.
        self.desk = ReportDesk(folder, Path(self.config.work_dir) / "reports", ollama_url="http://127.0.0.1:9",
                               sources=sources)
        self.grpc_server, _ = build_server(RetrievalServicer(self.desk), self.config.grpc_target)
        self.grpc_server.start()
        if load:
            load_and_publish(self.desk, self.config.redis_url)

    def stop_retrieval(self) -> None:
        if self.grpc_server is not None:
            self.grpc_server.stop(grace=None).wait()
            self.grpc_server = None

    def restart_retrieval(self) -> None:
        from forgeflow.desk_service.retrieval_server import RetrievalServicer, build_server

        self.grpc_server, _ = build_server(RetrievalServicer(self.desk), self.config.grpc_target)
        self.grpc_server.start()

    def start_gateway(self) -> None:
        from forgeflow.desk_service.gateway import create_app

        server = Uvicorn(create_app(self.config), self.gateway_port)
        server.start()
        self._uvicorns.append(server)

    def start_workers(self, count: int = 1) -> None:
        """Workers as asyncio tasks on one background loop (same code path as the worker process)."""
        if self._worker_loop is None:
            self._worker_loop = asyncio.new_event_loop()
            self._worker_thread = threading.Thread(target=self._worker_loop.run_forever, daemon=True)
            self._worker_thread.start()
        for _ in range(count):
            # The Event must be created on the worker loop that will wait on it.
            stop = asyncio.run_coroutine_threadsafe(self._new_event(), self._worker_loop).result(5)
            self._worker_stops.append(stop)
            asyncio.run_coroutine_threadsafe(self._run_worker(stop), self._worker_loop)
        self.wait_for_consumers(self._expected_consumers())

    @staticmethod
    async def _new_event() -> asyncio.Event:
        return asyncio.Event()

    async def _run_worker(self, stop: asyncio.Event) -> None:
        import redis.asyncio as aioredis

        from forgeflow.desk_service.model import StreamingChatModel
        from forgeflow.desk_service.proto import desk_pb2_grpc
        from forgeflow.desk_service.worker import Worker

        redis = aioredis.Redis.from_url(self.config.redis_url)
        from forgeflow.desk_service.retrieval_server import CHANNEL_OPTIONS

        channel = grpc.aio.insecure_channel(self.config.grpc_target, options=CHANNEL_OPTIONS)
        model = StreamingChatModel(self.config.model, self.config.ollama_url, timeout=self.config.model_timeout_s)
        try:
            await Worker(self.config, redis, desk_pb2_grpc.ReportRetrievalStub(channel), model).run(stop)
        finally:
            await model.aclose()
            await channel.close()
            await redis.aclose()

    def start_worker_process(self) -> subprocess.Popen:
        env = {**os.environ, **self.config.to_env(), "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen([sys.executable, "-m", "forgeflow.desk_service", "worker"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.processes.append(process)
        self.wait_for_consumers(self._expected_consumers())
        return process

    def _expected_consumers(self) -> int:
        return len(self._worker_stops) + sum(process.poll() is None for process in self.processes)

    def wait_for_consumers(self, count: int, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.queue_state()[1] >= count:
                return
            time.sleep(0.1)
        raise RuntimeError(f"expected {count} consumers on {self.config.queue}")

    def queue_state(self, queue: str | None = None) -> tuple[int, int]:
        """(messages waiting, consumers) for a queue, from rabbitmqctl-free AMQP passive declare."""
        async def check():
            import aio_pika

            connection = await aio_pika.connect(self.config.amqp_url)
            async with connection:
                channel = await connection.channel()
                try:
                    declared = await channel.declare_queue(queue or self.config.queue, passive=True)
                except Exception:  # noqa: BLE001 - not declared yet
                    return 0, 0
                return declared.declaration_result.message_count, declared.declaration_result.consumer_count

        return asyncio.run(check())

    # ------------------------------------------------------------------ client
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"

    def ask(self, question: str, gate: bool = False) -> httpx.Response:
        return httpx.post(f"{self.url}/v2/reports/ask", json={"question": question, "gate": gate}, timeout=30,
                          trust_env=False)

    def events(self, job_id: str, *, after: str | None = None, timeout: float = 60) -> list[dict]:
        """Read the SSE stream to its end; returns parsed events (id, type, data)."""
        headers = {"Last-Event-ID": after} if after else {}
        out: list[dict] = []
        with httpx.stream("GET", f"{self.url}/v2/jobs/{job_id}/events", headers=headers, timeout=timeout,
                          trust_env=False) as response:
            response.raise_for_status()
            assert response.headers["content-type"].startswith("text/event-stream")
            event: dict = {}
            for line in response.iter_lines():
                if line.startswith("id: "):
                    event["id"] = line[4:]
                elif line.startswith("event: "):
                    event["type"] = line[7:]
                elif line.startswith("data: "):
                    event["data"] = json.loads(line[6:])
                elif line == "" and "type" in event:
                    out.append(event)
                    if event["type"] in {"final", "error"}:
                        return out
                    event = {}
        return out

    def job(self, job_id: str) -> dict:
        return httpx.get(f"{self.url}/v2/jobs/{job_id}", timeout=10, trust_env=False).json()

    # ------------------------------------------------------------------ teardown
    def close(self) -> None:
        for stop in self._worker_stops:
            self._worker_loop.call_soon_threadsafe(stop.set)
        for process in self.processes:
            with contextlib.suppress(Exception):
                process.terminate()
                process.wait(timeout=10)
        if self._worker_loop is not None:
            time.sleep(0.5)
            self._worker_loop.call_soon_threadsafe(self._worker_loop.stop)
        for server in reversed(self._uvicorns):
            server.stop()
        self.stop_retrieval()

        async def drop_queues():
            import aio_pika

            connection = await aio_pika.connect(self.config.amqp_url)
            async with connection:
                channel = await connection.channel()
                for name in (self.config.queue, self.config.retry_queue, self.config.dead_queue):
                    with contextlib.suppress(Exception):
                        await channel.queue_delete(name)

        with contextlib.suppress(Exception):
            asyncio.run(drop_queues())


def with_config(harness: Harness, **changes) -> None:
    harness.config = replace(harness.config, **changes)
