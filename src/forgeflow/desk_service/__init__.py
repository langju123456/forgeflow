"""年报问答 as a service: gateway (FastAPI + SSE) → RabbitMQ → workers → gRPC retrieval server, Redis in between.

DEV_ONLY, loopback only, one fixed analyst identity, like the in-process desk it wraps (forgeflow.report_desk).
What it adds is the serving layer, not new retrieval or answer logic:

    browser ── POST /v2/reports/ask ──► gateway ── cache hit? ──► Redis  cache:{key}
                                          │  in flight already? ──► Redis  inflight:{key}  (SET NX: join that job)
                                          │  otherwise publish (publisher confirm) ──► RabbitMQ  desk.ask
                                          ▼
    browser ◄── GET /v2/jobs/{id}/events (SSE, resumable) ◄── Redis Stream  events:{job}
                                                                  ▲
    worker (prefetch = concurrency) ── consume desk.ask ──────────┤ stage / token / final / error events
        ├─ gRPC ReportRetrieval.Search (deadline) ──► retrieval server (holds the index, the Store, the ACL)
        ├─ local model, streamed; tokens coalesced; the draft is never shown as the answer
        ├─ cited_answer's checks on the full output (only given hits may be cited)
        └─ cache the checked result, release inflight, ack; transient failure → desk.ask.retry (TTL) → desk.ask;
           out of attempts → desk.ask.dead

Modules: config, keys (cache key and Redis names), events (Redis Stream + SSE + the answer-field extractor),
model (streaming chat client), retrieval_server (gRPC), worker, gateway. ``python -m forgeflow.desk_service``
starts them. Design notes and measured runs: docs/20-desk-service.md.
"""
