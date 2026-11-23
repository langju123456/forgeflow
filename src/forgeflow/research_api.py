"""Researcher/reviewer routes for both local report frontends; input evidence comes only from the desk."""
from __future__ import annotations

import json
import re
from html import escape
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from . import annual_cash, apple_sales
from . import research_store as store
from .contracts import DomainError, StrictModel, canonical, digest

Short = Annotated[str, Field(min_length=1, max_length=128)]
Head = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class NewDraft(StrictModel):
    grant_id: Short
    note: str = Field(default="", max_length=4000)


class Revision(NewDraft):
    expected: Head


class Decision(StrictModel):
    expected: Head
    action: Literal["submit", "return", "approve", "deliver"]
    reviewer: Short | None = None
    memo: str = Field(default="", max_length=2000)


def fact_record(result, reports, transport):
    """Accept two locally verified public tools. An older result without source bindings requires a fresh run."""
    cases = {annual_cash.VERSION: ("catl-operating-cash-period-dev", annual_cash.DOCUMENT, "ANNUAL_COLUMN"),
             apple_sales.VERSION: ("apple-services-growth-contribution-dev", apple_sales.DOCUMENT, "SALES_CALCULATION")}
    spec = cases.get(result.get("answerer"))
    if not spec or result.get("gate") != spec[2] or result.get("status") != "ANSWERED" or not result.get("found"):
        raise DomainError("UNVERIFIED", "只能保存已核年度原格或已核收入计算。", 422)
    report = next((r for r in reports if r["key"] == spec[1] and r["state"] == "ok"), None)
    citations = result.get("citations") or []
    if not report or not citations or not result.get("run_id") or not result.get("index_version"):
        raise DomainError("SOURCE_CHANGED", "来源版本不完整，请重新查询已校验的年报。", 409)
    sha = report.get("sha256", "")
    if not re.fullmatch("[a-f0-9]{64}", sha):
        raise DomainError("SOURCE_CHANGED", "缺少年报完整指纹。", 409)
    for citation in citations:
        if (citation.get("doc") != spec[1] or citation.get("file_prefix") != sha[:16] or
                not citation.get("source_hash") or not citation.get("row_version") or not citation.get("page")):
            raise DomainError("SOURCE_CHANGED", "原格与当前年报指纹不一致，请重新查询。", 409)
    return {"schema_version": "forgeflow.research.evidence.v1", "case_id": spec[0], "split": "public-dev",
            "run_id": result["run_id"], "question": result["question"], "answer": result["answer"],
            "source": {k: report[k] for k in ("key", "document", "url", "sha256")},
            "citations": citations, "calculation": result.get("calculation"), "tool_version": result["answerer"],
            "index_version": result["index_version"], "transport": transport, "model": None, "model_calls": 0,
            "external_cost": 0, "timings": result["timings"], "cached": bool(result.get("cached")),
            "attempt": result.get("attempt"), "source_scope": "local/reports/annual-reports",
            "checks": {"gate": result["gate"], "state": result["status"]}}


def archive(actor, draft_id, version):
    with store.connection() as db:
        draft = db.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if draft and actor["subject"] in {draft["owner"], draft["reviewer"]}:
            rows = db.execute("SELECT * FROM event WHERE draft=? AND version=? ORDER BY seq", (draft_id, version))
            events = [dict(row) for row in rows]
            if events and (actor["subject"] == draft["owner"] or any(e["kind"] == "submitted" for e in events)):
                row = db.execute("SELECT digest,payload FROM revision WHERE draft=? AND version=?",
                                 (draft_id, version)).fetchone()
                payload = json.loads(row["payload"])
                if digest(payload) != row["digest"] or any(e["digest"] != row["digest"] for e in events):
                    raise DomainError("INTEGRITY_ERROR", "稿件或审阅记录与保存的版本摘要不一致。", 409)
                receipt = next((e for e in events if e["kind"] == "delivered"), None)
                return {"schema_version": "forgeflow.research.export.v1", "payload": payload, "digest": row["digest"],
                        "state": events[-1]["kind"], "events": events, "local_receipt": digest(receipt) if receipt else None}
    raise DomainError("DRAFT_NOT_FOUND", "当前身份没有这份可见版本。", 404)


def reading_copy(data):
    payload = data["payload"]
    evidence = payload["evidence"]
    state = {"saved": "草稿·未送审", "returned": "已退回", "submitted": "待审阅",
             "approved": "已批准", "delivered": "本地交付"}[data["state"]]
    source = evidence["source"]
    sections = [("年报研究稿", f"第 {payload['version']} 版 · {state}"), ("研究问题", evidence["question"]),
                ("工具核对的结论", evidence["answer"]), ("研究员说明", payload["note"]),
                ("来源", f"{source['document']}\n{source['url']}\nPDF SHA-256 {source['sha256']}")]
    for c in evidence["citations"]:
        sections.append((f"原表第 {c['page']} 页", c["snippet"]))
    calc = evidence.get("calculation") or {}
    if calc:
        lines = [f"金额单位　{calc['unit']}"]
        for i, period in enumerate(calc["periods"]):
            lines.append(f"FY{period}　服务收入 {calc['services'][i]:,}　公司总收入 {calc['total_sales'][i]:,}")
        for op in calc["operations"]:
            ratio = "分母为零" if op["percent"] is None else op["percent"] + "%"
            lines.append(f"{op['metric']}　{op['formula']}　{op['numerator']:,} / {op['denominator']:,}　{ratio}")
        sections.append(("计算依据", "\n".join(lines)))
    sections += [("审阅记录", "\n".join(f"{e['at']} {e['actor']} {e['kind']} {e['memo']}".rstrip() for e in data["events"])),
                 ("版本 SHA-256", data["digest"]), ("本地交付回执", data["local_receipt"] or "尚未交付")]
    body = "".join(f"<section><h2>{escape(label)}</h2><pre>{escape(value)}</pre></section>" for label, value in sections)
    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>年报研究稿</title>"
            "<style>body{max-width:52rem;margin:3rem auto;padding:1rem;font:16px/1.55 system-ui}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}h2{font-size:1.12rem}</style>"
            + body + "</html>\n")


def identity(request: Request, authorization: str | None = Header(default=None)):
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, detail={"message": "研究库仅限本机访问。"})
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) > 200:
        raise HTTPException(401, detail={"message": "请提供本机研究身份凭证。"})
    return call(store.identify, authorization[7:])


def optional_author(request: Request, authorization: str | None = Header(default=None)):
    if authorization is None:
        return None
    actor = identity(request, authorization)
    call(store.role_is, actor, "author")
    return actor


def mount(app, resolve, transport):
    """resolve(run_id) reads the producer result for an authenticated researcher's request."""
    actor_dep = Depends(identity)

    @app.get("/research/me")
    def me(actor=actor_dep):
        return JSONResponse(actor, headers={"Cache-Control": "no-store"})

    @app.get("/research/drafts")
    def drafts(actor=actor_dep):
        return private(call(store.list_drafts, actor))

    @app.get("/research/drafts/{draft_id}")
    def get(draft_id: str, actor=actor_dep):
        return private(call(store.get_draft, actor, draft_id))

    async def source(actor, grant_id):
        call(store.role_is, actor, "author")
        run_id = call(store.granted_source, actor, grant_id, transport)
        try:
            facts = await resolve(run_id)
            return {**facts, "request_owner": actor["subject"], "request_id": run_id}
        except DomainError as exc:
            fail(exc)

    @app.post("/research/drafts")
    async def create(body: NewDraft, actor=actor_dep):
        return private(call(store.save, actor, await source(actor, body.grant_id), body.note))

    @app.post("/research/drafts/{draft_id}/revisions")
    async def revise(draft_id: str, body: Revision, actor=actor_dep):
        return private(call(store.save, actor, await source(actor, body.grant_id), body.note,
                            draft_id=draft_id, expected=body.expected))

    @app.post("/research/drafts/{draft_id}/decisions")
    def decision(draft_id: str, body: Decision, actor=actor_dep):
        return private(call(store.decide, actor, draft_id, body.expected, body.action, body.reviewer, body.memo))

    @app.get("/research/drafts/{draft_id}/versions/{version}/export/{fmt}")
    def export(draft_id: str, version: int, fmt: Literal["json", "html"], actor=actor_dep):
        data = call(archive, actor, draft_id, version)
        content = canonical(data) if fmt == "json" else reading_copy(data)
        return Response(content, media_type="application/json" if fmt == "json" else "text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                                 "Content-Disposition": f'attachment; filename="research-v{version}.{fmt}"'})


def fail(exc):
    raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from exc


def call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except DomainError as exc:
        fail(exc)


def private(data):
    return JSONResponse(data, headers={"Cache-Control": "no-store"})
