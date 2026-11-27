"""Annual-report desk (DEV_ONLY): ask a question over public annual reports, get a cited answer or "not in the reports".

Wiring only; each decision lives in the module it calls:

    PDF ─ pdf_ingest (rows with file/page/bbox citations) ─ Store (S1 roles, classification, scope)
        ─ RankedKnowledgeService (hybrid if the local Ollama serves bge-m3, otherwise lexical)
        ─ AnswerabilityGate (optional, off by default: keep only hits that state the answer, with a quoted span)
        ─ cited answer (local model; may only cite hits it was given)
        ─ report_orchestration (planner + executor + verifier by default; single answer, self-check or verifier
          on request)

The desk reads PDFs from a local folder (default ``~/code/finreports``) and checks each against the sha256 in
``preparation/s4/real-reports/sources.json``; a file that is missing or differs is listed as unavailable, never
ingested. Everything runs on this machine; nothing is sent to a hosted model.
"""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import annual_cash, apple_sales, report_lookup, report_orchestration
from .contracts import DomainError, Principal, Scope

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ROOT / "preparation" / "s4" / "real-reports" / "sources.json"
SCOPE = Scope(tenant_id="local", workspace_id="reports", project_id="annual-reports")
LOADER = Principal(subject_id="desk-loader", kind="user", roles=frozenset({"admin", "finance"}), scopes=(SCOPE,),
                   clearance=3)
ANALYST = Principal(subject_id="desk-analyst", kind="user", roles=frozenset({"finance"}), scopes=(SCOPE,), clearance=1)
OLLAMA = "http://127.0.0.1:11434"
EMBED_BATCH = 32


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ollama_tags(base_url: str = OLLAMA) -> set[str]:
    """Full names of local models (``qwen3:8b``, ``bge-m3:latest``), or an empty set when no Ollama server answers."""
    import httpx

    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            tags = client.get(f"{base_url}/api/tags").json().get("models", [])
        return {str(m.get("name", "")) for m in tags}
    except (httpx.HTTPError, ValueError, AttributeError):
        return set()


def ollama_models(base_url: str = OLLAMA) -> set[str]:
    """Model names without their tag, or an empty set when no Ollama server answers."""
    return {name.split(":")[0] for name in ollama_tags(base_url)}


def pick_chat_model(listed: set[str]) -> str | None:
    """The chat model for the gate: the 16k-context tag first, plain ``qwen3:8b`` only as a fallback.

    Ollama loads ``qwen3:8b`` with a 4096-token context and silently truncates longer prompts; real annual-report
    prompts reach about 7k tokens (preparation/s4/real-reports/RESULTS.md), so the gate uses the same model list
    as ``cited_answer.default_answerer``.
    """
    from .cited_answer import PREFERRED_MODELS

    return next((m for m in PREFERRED_MODELS if m in listed or f"{m}:latest" in listed), None)


class ReportDesk:
    def __init__(self, reports_dir: Path, work_dir: Path, *, ollama_url: str = OLLAMA,
                 answerer: Callable[[str, list[Any]], dict] | None = None, sources: Path = SOURCES,
                 orchestrator: report_orchestration.Orchestrator | None = None, orchestration: str | None = None):
        self.reports_dir = Path(reports_dir).expanduser()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ollama_url = ollama_url
        self.answerer = answerer
        self.orchestrator = orchestrator  # built on first use from the local Ollama when not given
        # How ``ask`` answers when the caller does not say: report_orchestration.DEFAULT_MODE unless given here.
        self.orchestration = report_orchestration.DEFAULT_MODE if orchestration is None else orchestration
        if self.orchestration not in report_orchestration.MODES:
            raise ValueError(f"orchestration is one of {', '.join(report_orchestration.MODES)}")
        self.sources = Path(sources)
        self.reports: list[dict] = []
        self.status = {"state": "starting", "detail": "", "embedded": 0, "rows": 0}
        self._lock = threading.Lock()
        self.store = None
        self.service = None
        self.gate = None
        self.mode = "lexical"
        self._row_index: dict[str, dict] = {}

    # ------------------------------------------------------------------ loading
    def catalogue(self) -> list[dict]:
        sources = json.loads(self.sources.read_text())["reports"]
        out = []
        for source in sources:
            path = self.reports_dir / f"{source['key']}.pdf"
            state = "missing"
            if path.exists():
                state = "ok" if _sha256(path) == source["sha256"] else "changed"
            out.append({**{k: source[k] for k in ("key", "company", "document", "language", "pages", "url", "sha256")},
                        "state": state, "path": str(path)})
        return out

    def load(self) -> None:
        """Ingest the verified PDFs (cached by file hash), then build the vector index if bge-m3 is available."""
        from .evt_store import EVTBoundStore
        from .pdf_ingest import IngestResult, ingest_into_store, ingest_pdf

        self.reports = self.catalogue()
        store = EVTBoundStore(f"sqlite:///{self.work_dir / 'reports.db'}")
        store.create_schema()
        cache = self.work_dir / "ingest-cache"
        cache.mkdir(exist_ok=True)
        rows: list[dict] = []
        for report in self.reports:
            if report["state"] != "ok":
                continue
            self._set("ingesting", f"解析 {report['company']}")
            sha = _sha256(Path(report["path"]))
            cached = cache / f"{sha}.json"
            if cached.exists():
                data = json.loads(cached.read_text())
                result = IngestResult(**{k: data[k] for k in ("file", "file_sha256", "pages", "tables", "rows",
                                                               "warnings")})
            else:
                result = ingest_pdf(report["path"], scope=SCOPE, roles=["finance"], classification=1,
                                    doc_key=report["key"])
                cached.write_text(json.dumps(result.to_dict(), ensure_ascii=False))
            ingest_into_store(store, LOADER, SCOPE, result)
            report["rows"] = len(result.rows)
            report["tables"] = len(result.tables)
            rows.extend(result.rows)
        self.store = store
        self._row_index = {row["id"]: row for row in rows}
        self.status["rows"] = len(rows)
        self._build_service(rows)
        self._set("ready", "")

    def _set(self, state: str, detail: str) -> None:
        with self._lock:
            self.status["state"], self.status["detail"] = state, detail

    def _build_service(self, rows: list[dict]) -> None:
        from .retrieval import (
            HybridRanker,
            OllamaEmbedder,
            RankedKnowledgeService,
            VectorRanker,
            lexical_ranker,
        )

        listed = ollama_tags(self.ollama_url)
        models = {name.split(":")[0] for name in listed}
        if "bge-m3" not in models:
            self.mode = "lexical"
            self.service = RankedKnowledgeService(self.store, lexical_ranker)
            self.status["detail"] = "没有找到本机 Ollama 的 bge-m3，只用词面检索"
            return
        vector = VectorRanker(OllamaEmbedder("bge-m3", self.ollama_url, timeout=300), min_similarity=0.50)
        self._warm(vector, rows)
        self.mode = "hybrid"
        self.service = RankedKnowledgeService(self.store, HybridRanker(vector, k=60))
        chat = pick_chat_model(listed)
        if chat is not None:
            from .answerability import AnswerabilityGate, OllamaChatModel

            self.gate = AnswerabilityGate(OllamaChatModel(chat, base_url=self.ollama_url, timeout=300))

    def _warm(self, vector: Any, rows: list[dict]) -> None:
        """Embed every row once, in batches, and keep the vectors on disk keyed by content hash."""
        path = self.work_dir / "embeddings-bge-m3.pkl"
        stored: dict[str, list[float]] = {}
        if path.exists():
            stored = pickle.loads(path.read_bytes())
        model = vector.embedder.model_id
        todo = [r for r in rows if r["source_hash"] not in stored]
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start:start + EMBED_BATCH]
            for row, emb in zip(batch, vector.embedder.embed([r["content"] for r in batch]), strict=True):
                stored[row["source_hash"]] = emb
            self._set("embedding", f"建立向量索引 {min(start + EMBED_BATCH, len(todo))}/{len(todo)}")
            self.status["embedded"] = len(rows) - len(todo) + min(start + EMBED_BATCH, len(todo))
            if start % (EMBED_BATCH * 20) == 0:
                path.write_bytes(pickle.dumps(stored))
        path.write_bytes(pickle.dumps(stored))
        for row in rows:
            vector._cache[(model, row["source_hash"])] = stored[row["source_hash"]]
        self.status["embedded"] = len(rows)

    # ------------------------------------------------------------------ asking
    def citation(self, hit: Any, question: str = "") -> dict:
        from .pdf_ingest import parse_source_line

        place = parse_source_line(hit.content) or {}
        row = self._row_index.get(hit.id, {})
        body = hit.content.split("\n", 1)[1] if "\n" in hit.content else hit.content
        report = next((r for r in self.reports if hit.id.startswith(r["key"])), None)
        pages = place.get("pages") or []
        first = pages[0] if pages else {}
        year = annual_cash.requested_year(question)
        cell = annual_cash.read_cell(hit.id, hit.content, year) if year else None
        if cell:
            body = f"{annual_cash.METRIC} · {year}年年度列 · 人民币千元\n原格 {cell.raw}\n{body}"
        sales_request = apple_sales.request(question)
        if sales_request:
            fiscal_year, _ = sales_request
            sales = apple_sales.read_sales(hit.id, hit.content, fiscal_year)
            if sales:
                body = (f"百万美元 · FY{fiscal_year} / FY{fiscal_year-1}\n"
                        f"Services | {sales.current_services:,} | {sales.prior_services:,}\n"
                        f"Total net sales | {sales.current_total:,} | {sales.prior_total:,}\n{body}")
        return {"id": hit.id, "doc": report["key"] if report else None,
                "company": report["company"] if report else None,
                "kind": row.get("kind"), "page": first.get("page"), "bbox": first.get("bbox"),
                "snippet": body[:600], "file_prefix": place.get("file_sha256_prefix"),
                "source_hash": hit.source_hash, "row_version": hit.version}

    def evidence(self, question: str, *, gate: bool = False, top_k: int = 5,
                 principal: Principal = ANALYST, search_query: str | None = None, doc: str | None = None) -> dict:
        """Retrieval and the optional gate, without the answer step: what the desk service's gRPC server returns.

        ``ask`` is this plus the cited answer in the same process; the desk service (forgeflow.desk_service) runs
        the answer step in a separate worker, so both paths share exactly this evidence step.

        ``search_query`` and ``doc`` are a planner's plan (report_orchestration): rank with other words than the
        question, inside one report. Both act after the Store's ACL filter and can only narrow what is returned;
        the gate, when on, still judges the hits against the question itself.
        """
        if self.service is None:
            raise DomainError("DESK_NOT_READY", self.status.get("detail") or "Reports are still loading", 503)
        question = question.strip()
        if not question:
            raise DomainError("QUERY_REQUIRED", "Ask a question", 422)
        timings: dict[str, float] = {}
        started = time.perf_counter()
        service = self.service
        lookup = report_lookup.for_question(question)
        if lookup:
            from .retrieval import RankedKnowledgeService

            service = RankedKnowledgeService(self.store, lookup.rank)
        elif doc:
            from .retrieval import RankedKnowledgeService

            service = RankedKnowledgeService(self.store, report_orchestration.within(doc, self.service.ranker))
        # A conflicting answer needs two separately authorized links, even for one requested answer.
        limit = 2 if lookup and type(top_k) is int and top_k == 1 else top_k
        hits = service.search(principal, SCOPE, question if lookup or not search_query else search_query, limit)
        timings["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 1)
        kept, dropped, gate_status = list(hits), [], "OFF"
        if lookup:
            if lookup.conflicting(hits):
                gate_status, kept, dropped = "ANNUAL_CONFLICT", [], list(hits)
            else:
                gate_status, kept = lookup.gate, kept[:top_k]
        elif gate and self.gate is not None and hits:
            started = time.perf_counter()
            result = self.gate.check(question, hits)
            timings["gate_ms"] = round((time.perf_counter() - started) * 1000, 1)
            gate_status = result.status
            # AnswerabilityResult.kept holds positions; .hits holds the kept hits themselves, in retrieval order.
            kept = list(result.hits)
            kept_ids = {h.id for h in kept}
            dropped = [h for h in hits if h.id not in kept_ids]
        return {"question": question, "hits": list(hits), "kept": kept, "dropped": dropped,
                "gate": gate_status, "timings": timings}

    def index_version(self) -> str:
        """Fingerprint of what retrieval can return: every loaded row's content hash plus the ranking mode.

        A cached answer is only valid for the index it was computed on; the desk service puts this in its cache
        key, so re-ingesting a report or switching lexical/hybrid never serves an answer from the old index.
        """
        from .report_plugin import RUNTIME_VERSION

        material = {"mode": self.mode, "source_metadata_version": 1, "report_runtime": RUNTIME_VERSION,
                    "annual_lookup": [annual_cash.VERSION, apple_sales.VERSION],
                    "rows": sorted(r["source_hash"] for r in self._row_index.values())}
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]

    def ask(self, question: str, *, gate: bool = False, top_k: int = 5, orchestration: str | None = None) -> dict:
        """Retrieve, optionally run the answerability gate, then answer from what is left.

        The gate is off unless asked for: on the real annual reports it met the permission and answerable rules
        but turned only 1 of the 12 absent questions into a correct "not in the reports" (the pre-registered bar
        was 3), so by that rule it stays off by default (preparation/s4/real-reports/RESULTS.md).

        ``orchestration`` defaults to the desk's own (``report_orchestration.DEFAULT_MODE``, plan_verify: it met the
        registered rule on the held-out and the confirmation set, preparation/s6/multi-agent/RESULTS.md); ``single``
        and the others stay selectable. Without the 16k-context model an orchestration is refused, not downgraded:
        ask for ``single`` there. Questions the report tools answer never reach it.
        """
        orchestration = self.orchestration if orchestration is None else orchestration
        if orchestration not in report_orchestration.MODES:
            raise DomainError("INVALID_ORCHESTRATION", f"orchestration is one of {', '.join(report_orchestration.MODES)}",
                              422)
        found = self.evidence(question, gate=gate, top_k=top_k)
        question, kept, timings = found["question"], found["kept"], found["timings"]
        answer: dict[str, Any] = {"found": False, "answer": "", "citations": []}
        lookup = report_lookup.for_question(question)
        if lookup:
            answer = lookup.answer(question, kept)
        elif orchestration != "single":
            found, answer = self._orchestrate(orchestration, found, gate=gate, top_k=top_k)
        elif kept and self.answerer is not None:
            started = time.perf_counter()
            answer = self.answerer(question, kept)
            timings["answer_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return self.assemble(found, answer)

    def _orchestrate(self, mode: str, found: dict, *, gate: bool, top_k: int) -> tuple[dict, dict]:
        """Answer through report_orchestration; returns the evidence the answer was produced from and the answer."""
        if self.orchestrator is None:
            self.orchestrator = report_orchestration.default_orchestrator(self.ollama_url)
        if self.orchestrator is None:
            raise DomainError("ORCHESTRATION_UNAVAILABLE",
                              f"需要本机 Ollama 的 {report_orchestration.REQUIRED_MODEL}（16k 上下文）", 503)
        question, planned = found["question"], {}

        def search(query: str, doc: str | None) -> list[Any]:
            planned["found"] = self.evidence(question, gate=gate, top_k=top_k, search_query=query, doc=doc)
            return planned["found"]["kept"]

        started = time.perf_counter()
        outcome = self.orchestrator.run(mode, question, found["kept"], search=search,
                                        catalogue=[r for r in self.reports if r["state"] == "ok"])
        timings = dict(found["timings"])
        if outcome.evidence == "planned":
            timings["planned_retrieval_ms"] = planned["found"]["timings"]["retrieval_ms"]
            found = planned["found"]
        timings["answer_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return {**found, "timings": timings}, outcome.result

    def assemble(self, found: dict, answer: dict, *, run_id=None, index_version=None, tool_trace=None) -> dict:
        """Map only independently retrieved row IDs back to citation objects."""
        question, hits = found["question"], found["hits"]
        by_id = {hit.id: hit for hit in hits}
        result = {
            "question": question, "mode": self.mode, "gate": found["gate"], "status": answer.get("status"),
            **({"calculation": answer["calculation"]} if "calculation" in answer else {}),
            **({"orchestration": answer["orchestration"]} if "orchestration" in answer else {}),
            "found": bool(answer.get("found")) and bool(answer.get("citations")),
            "answer": answer.get("answer", ""),
            "citations": [self.citation(by_id[c], question) for c in answer.get("citations", []) if c in by_id],
            "kept": [self.citation(h, question) for h in found["kept"]],
            "dropped": [self.citation(h, question) for h in found["dropped"]],
            "answerer": answer.get("tool_version") or answer.get("model") or ("none" if self.answerer is None else "local"),
            "timings": found["timings"],
        }
        if tool_trace is not None:
            result.update(run_id=run_id, index_version=index_version, tool_trace=tool_trace)
        return result

    def page_png(self, key: str, page: int, box: list[float] | None, resolution: int = 110) -> bytes:
        import pdfplumber

        report = next((r for r in self.reports if r["key"] == key and r["state"] == "ok"), None)
        if report is None:
            raise DomainError("NOT_FOUND", "Report unavailable", 404)
        with pdfplumber.open(report["path"]) as pdf:
            if not 1 <= page <= len(pdf.pages):
                raise DomainError("NOT_FOUND", "Page out of range", 404)
            image = pdf.pages[page - 1].to_image(resolution=resolution)
            if box and len(box) == 4:
                image.draw_rect(tuple(box), fill=(255, 196, 0, 60), stroke=(220, 120, 0), stroke_width=2)
            buffer = io.BytesIO()
            image.annotated.save(buffer, format="PNG")
            return buffer.getvalue()


def default_answerer() -> Callable[[str, list[Any]], dict] | None:
    """The cited-answer step from forgeflow.cited_answer when it is installed and a local model answers."""
    try:
        from . import cited_answer
    except ImportError:
        return None
    factory = getattr(cited_answer, "default_answerer", None)
    return factory() if callable(factory) else None


def mount_report_desk(app: Any, desk: ReportDesk) -> None:
    """Add /reports routes to a FastAPI app. DEV_ONLY: one fixed analyst identity, local files only."""
    import asyncio

    from fastapi import Body, Depends, HTTPException
    from fastapi.responses import Response

    from . import research_api
    from .report_plugin import ReportProcessError, ReportProtocolError, ReportRuntime
    from .report_plugin import run as run_report_tool

    recent: OrderedDict[str, dict] = OrderedDict()
    recent_lock = threading.Lock()

    async def resolve(run_id):
        with recent_lock:
            result = recent.get(run_id)
        if result is None:
            raise DomainError("RUN_EXPIRED", "请重新运行该查表问题，然后保存。", 409)
        return research_api.fact_record(result, desk.reports, "sync")

    research_api.mount(app, resolve, "sync")
    runtime = ReportRuntime()
    app.state.report_runtime = runtime
    app.router.on_shutdown.append(runtime.close)
    author_dep = Depends(research_api.optional_author)

    def fail(error: DomainError):
        raise HTTPException(status_code=error.status, detail={"code": error.code, "message": error.message})

    @app.get("/reports")
    def reports():
        return {"reports": [{k: v for k, v in r.items() if k != "path"} for r in desk.reports or desk.catalogue()],
                "status": dict(desk.status), "mode": desk.mode, "gate_available": desk.gate is not None,
                "answer_available": desk.answerer is not None}

    @app.post("/reports/ask")
    async def ask(question: str = Body(..., embed=True), gate: bool = Body(False, embed=True),
                  orchestration: str | None = Body(None, embed=True), actor=author_dep):
        try:
            if report_lookup.for_question(question):
                found = await asyncio.to_thread(desk.evidence, question, gate=gate)
                run_id, index_version = uuid4().hex, desk.index_version()
                started = time.perf_counter()
                answer, trace = await run_report_tool(
                    found["question"], [h.model_dump() for h in found["kept"]], run_id=run_id,
                    index_version=index_version, journal_dir=desk.work_dir, runtime=runtime)
                found["timings"]["tool_ms"] = round((time.perf_counter() - started) * 1000, 1)
                result = desk.assemble(found, answer, run_id=run_id, index_version=index_version, tool_trace=trace)
            else:
                result = await asyncio.to_thread(desk.ask, question, gate=gate, orchestration=orchestration)
            if actor and result.get("found") and report_lookup.for_question(question):
                # Revalidate outward/trace agreement before creating a source grant.
                research_api.fact_record(result, desk.reports, "sync")
                with recent_lock:
                    recent[result["run_id"]] = result
                    if len(recent) > 128:
                        recent.popitem(last=False)
                result = {**result, "research_grant": research_api.store.grant_source(actor, result["run_id"], "sync")}
            return result
        except ReportProcessError:
            raise HTTPException(503, detail={"code": "REPORT_TOOL_PROCESS", "message": "查表工具暂时不可用，请稍后重试。"}) from None
        except ReportProtocolError:
            raise HTTPException(502, detail={"code": "REPORT_TOOL_PROTOCOL", "message": "工具结果未通过校验，未生成入稿许可。"}) from None
        except DomainError as error:
            fail(error)

    @app.get("/reports/{key}/pages/{page}.png")
    def page_png(key: str, page: int, box: str | None = None):
        try:
            parsed = [float(x) for x in box.split(",")] if box else None
            return Response(desk.page_png(key, page, parsed), media_type="image/png")
        except DomainError as error:
            fail(error)
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "BAD_BOX", "message": "box is x0,top,x1,bottom"}) from None
