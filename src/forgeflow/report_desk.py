"""Annual-report desk (DEV_ONLY): ask a question over public annual reports, get a cited answer or "not in the reports".

Wiring only; each decision lives in the module it calls:

    PDF ─ pdf_ingest (rows with file/page/bbox citations) ─ Store (S1 roles, classification, scope)
        ─ RankedKnowledgeService (hybrid if the local Ollama serves bge-m3, otherwise lexical)
        ─ AnswerabilityGate (optional: keep only hits that state the answer, with a quoted span)
        ─ cited answer (local model; may only cite hits it was given)

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def ollama_models(base_url: str = OLLAMA) -> set[str]:
    """Names of local models, or an empty set when no Ollama server answers."""
    import httpx

    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            tags = client.get(f"{base_url}/api/tags").json().get("models", [])
        return {m.get("name", "").split(":")[0] for m in tags}
    except (httpx.HTTPError, ValueError):
        return set()


class ReportDesk:
    def __init__(self, reports_dir: Path, work_dir: Path, *, ollama_url: str = OLLAMA,
                 answerer: Callable[[str, list[Any]], dict] | None = None, sources: Path = SOURCES):
        self.reports_dir = Path(reports_dir).expanduser()
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.ollama_url = ollama_url
        self.answerer = answerer
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
            out.append({**{k: source[k] for k in ("key", "company", "document", "language", "pages", "url")},
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

        models = ollama_models(self.ollama_url)
        if "bge-m3" not in models:
            self.mode = "lexical"
            self.service = RankedKnowledgeService(self.store, lexical_ranker)
            self.status["detail"] = "没有找到本机 Ollama 的 bge-m3，只用词面检索"
            return
        vector = VectorRanker(OllamaEmbedder("bge-m3", self.ollama_url, timeout=300), min_similarity=0.50)
        self._warm(vector, rows)
        self.mode = "hybrid"
        self.service = RankedKnowledgeService(self.store, HybridRanker(vector, k=60))
        if "qwen3" in models:
            from .answerability import AnswerabilityGate, OllamaChatModel

            self.gate = AnswerabilityGate(OllamaChatModel(base_url=self.ollama_url))

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
    def citation(self, hit: Any) -> dict:
        from .pdf_ingest import parse_source_line

        place = parse_source_line(hit.content) or {}
        row = self._row_index.get(hit.id, {})
        body = hit.content.split("\n", 1)[1] if "\n" in hit.content else hit.content
        report = next((r for r in self.reports if hit.id.startswith(r["key"])), None)
        pages = place.get("pages") or []
        first = pages[0] if pages else {}
        return {"id": hit.id, "doc": report["key"] if report else None,
                "company": report["company"] if report else None,
                "kind": row.get("kind"), "page": first.get("page"), "bbox": first.get("bbox"),
                "snippet": body[:600]}

    def ask(self, question: str, *, gate: bool = True, top_k: int = 5) -> dict:
        if self.service is None:
            raise DomainError("DESK_NOT_READY", self.status.get("detail") or "Reports are still loading", 503)
        question = question.strip()
        if not question:
            raise DomainError("QUERY_REQUIRED", "Ask a question", 422)
        timings: dict[str, float] = {}
        started = time.perf_counter()
        hits = self.service.search(ANALYST, SCOPE, question, top_k)
        timings["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 1)
        kept, dropped, gate_status = list(hits), [], "OFF"
        if gate and self.gate is not None and hits:
            started = time.perf_counter()
            result = self.gate.check(question, hits)
            timings["gate_ms"] = round((time.perf_counter() - started) * 1000, 1)
            gate_status = result.status
            kept_ids = {h.id for h in result.kept}
            kept = [h for h in hits if h.id in kept_ids]
            dropped = [h for h in hits if h.id not in kept_ids]
        answer: dict[str, Any] = {"found": False, "answer": "", "citations": []}
        if kept and self.answerer is not None:
            started = time.perf_counter()
            answer = self.answerer(question, kept)
            timings["answer_ms"] = round((time.perf_counter() - started) * 1000, 1)
        by_id = {h.id: h for h in hits}
        return {
            "question": question, "mode": self.mode, "gate": gate_status,
            "found": bool(answer.get("found")) and bool(answer.get("citations")),
            "answer": answer.get("answer", ""),
            "citations": [self.citation(by_id[c]) for c in answer.get("citations", []) if c in by_id],
            "kept": [self.citation(h) for h in kept], "dropped": [self.citation(h) for h in dropped],
            "answerer": answer.get("model") or ("none" if self.answerer is None else "local"),
            "timings": timings,
        }

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
    from fastapi import Body, HTTPException
    from fastapi.responses import Response

    def fail(error: DomainError):
        raise HTTPException(status_code=error.status, detail={"code": error.code, "message": error.message})

    @app.get("/reports")
    def reports():
        return {"reports": [{k: v for k, v in r.items() if k != "path"} for r in desk.reports or desk.catalogue()],
                "status": dict(desk.status), "mode": desk.mode, "gate_available": desk.gate is not None,
                "answer_available": desk.answerer is not None}

    @app.post("/reports/ask")
    def ask(question: str = Body(..., embed=True), gate: bool = Body(True, embed=True)):
        try:
            return desk.ask(question, gate=gate)
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
