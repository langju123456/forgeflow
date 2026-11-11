"""gRPC retrieval server: the one process that holds the annual-report index.

Why a separate process behind gRPC instead of retrieval inside every worker:

* Loading is heavy and stateful: parse 700+ pages, build the Store, embed every row (bge-m3) and keep the
  vectors in memory. Done once here; workers stay small, start fast and can be added or killed freely.
* The ACL lives with the data: the server maps the caller's subject (gRPC metadata) to a Principal it knows,
  and the Store filters by that Principal's roles before ranking, exactly as in the in-process desk. A worker
  cannot widen what it sees by sending different roles, because it never sends roles.
* The contract is typed (desk.proto) and calls carry a deadline: a worker that stops waiting cancels the call,
  and the server checks the remaining time before starting the optional gate (a model call).

Status codes the worker relies on: UNAVAILABLE (index still loading, or server down) and DEADLINE_EXCEEDED are
transient, so the worker retries the job later; INVALID_ARGUMENT and PERMISSION_DENIED are final.
"""

from __future__ import annotations

import json
import threading
from concurrent import futures
from typing import Any

import grpc

from ..contracts import DomainError, Principal
from ..report_desk import ANALYST
from . import keys
from .proto import desk_pb2, desk_pb2_grpc

SUBJECT_HEADER = "x-forgeflow-subject"
# Client channels reconnect quickly after the server restarts (gRPC's default first backoff is 1 s, then grows).
CHANNEL_OPTIONS = (("grpc.initial_reconnect_backoff_ms", 200), ("grpc.min_reconnect_backoff_ms", 200),
                   ("grpc.max_reconnect_backoff_ms", 2000))
MAX_TOP_K = 20
_STATUS = {503: grpc.StatusCode.UNAVAILABLE, 422: grpc.StatusCode.INVALID_ARGUMENT,
           403: grpc.StatusCode.PERMISSION_DENIED, 404: grpc.StatusCode.NOT_FOUND}


def default_principals() -> dict[str, Principal]:
    """DEV_ONLY: the desk's fixed analyst identity is the only caller the server accepts."""
    return {ANALYST.subject_id: ANALYST}


def _citation(c: dict[str, Any]) -> desk_pb2.Citation:
    return desk_pb2.Citation(id=c["id"], doc=c.get("doc") or "", company=c.get("company") or "",
                             kind=c.get("kind") or "", page=c.get("page") or 0, bbox=c.get("bbox") or [],
                             snippet=c.get("snippet") or "")


class RetrievalServicer(desk_pb2_grpc.ReportRetrievalServicer):
    def __init__(self, desk: Any, principals: dict[str, Principal] | None = None):
        self.desk = desk
        self.principals = principals if principals is not None else default_principals()

    def _principal(self, context: grpc.ServicerContext) -> Principal:
        subject = dict(context.invocation_metadata()).get(SUBJECT_HEADER)
        principal = self.principals.get(subject or "")
        if principal is None:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "unknown caller")
        return principal

    def _fail(self, context: grpc.ServicerContext, error: DomainError) -> None:
        context.abort(_STATUS.get(error.status, grpc.StatusCode.INTERNAL), f"{error.code}: {error.message}")

    def Search(self, request, context):  # noqa: N802 - gRPC method name
        principal = self._principal(context)
        top_k = request.top_k or 5
        if not 1 <= top_k <= MAX_TOP_K:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"top_k must be 1..{MAX_TOP_K}")
        # The gate is a model call; do not start it for a caller that has already given up.
        remaining = context.time_remaining()
        if request.gate and remaining is not None and remaining < 1.0:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "not enough time left for the gate")
        try:
            found = self.desk.evidence(request.question, gate=request.gate, top_k=top_k, principal=principal)
        except DomainError as error:
            self._fail(context, error)
        kept = []
        for hit in found["kept"]:
            kept.append(desk_pb2.Hit(id=hit.id, version=hit.version, content=hit.content,
                                     source_hash=hit.source_hash, classification=hit.classification,
                                     status=hit.status, citation=_citation(self.desk.citation(hit))))
        timings = found["timings"]
        return desk_pb2.SearchReply(kept=kept, dropped=[_citation(self.desk.citation(h)) for h in found["dropped"]],
                                    gate_status=found["gate"], mode=self.desk.mode,
                                    index_version=self.desk.index_version(),
                                    retrieval_ms=timings.get("retrieval_ms", 0.0), gate_ms=timings.get("gate_ms", 0.0))

    def Info(self, request, context):  # noqa: N802
        self._principal(context)
        status = dict(self.desk.status)
        ready = status.get("state") == "ready"
        reports = [{k: v for k, v in r.items() if k != "path"} for r in (self.desk.reports or self.desk.catalogue())]
        return desk_pb2.InfoReply(state=status.get("state", ""), detail=status.get("detail", ""), mode=self.desk.mode,
                                  index_version=self.desk.index_version() if ready else "",
                                  gate_available=self.desk.gate is not None, rows=int(status.get("rows") or 0),
                                  reports_json=json.dumps(reports, ensure_ascii=False))

    def PageImage(self, request, context):  # noqa: N802
        self._principal(context)
        try:
            png = self.desk.page_png(request.doc, request.page, list(request.bbox) or None)
        except DomainError as error:
            self._fail(context, error)
        return desk_pb2.PageImageReply(png=png)


def _loopback_bind(target: str) -> str:
    host = target.rsplit(":", 1)[0]
    if host not in {"127.0.0.1", "localhost", "[::1]"}:
        raise ValueError("DEV_ONLY: the retrieval server binds to loopback only")
    return target


def build_server(servicer: RetrievalServicer, target: str, max_workers: int = 8) -> tuple[grpc.Server, int]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="retrieval"),
                         options=[("grpc.max_send_message_length", 16 << 20)])
    desk_pb2_grpc.add_ReportRetrievalServicer_to_server(servicer, server)
    port = server.add_insecure_port(_loopback_bind(target))
    return server, port


def load_and_publish(desk: Any, redis_url: str | None) -> None:
    """Load the index, then tell the gateway which index version answers are now computed on."""
    desk.load()
    if redis_url:
        import redis

        client = redis.Redis.from_url(redis_url)
        client.set(keys.INDEX_VERSION, desk.index_version())
        client.close()


def serve(config: Any, desk: Any | None = None) -> None:
    from pathlib import Path

    from ..report_desk import ReportDesk

    if desk is None:
        desk = ReportDesk(Path(config.reports_dir), Path(config.work_dir) / "reports", ollama_url=config.ollama_url)
    server, port = build_server(RetrievalServicer(desk), config.grpc_target)
    server.start()
    print(f"retrieval server on {config.grpc_target} (port {port}); loading reports from {config.reports_dir}",
          flush=True)
    threading.Thread(target=load_and_publish, args=(desk, config.redis_url), name="desk-load", daemon=True).start()
    server.wait_for_termination()
