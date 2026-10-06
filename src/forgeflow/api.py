"""Local-only DEV identity adapter. Production SSO is intentionally not implemented."""

from __future__ import annotations

import argparse
import os
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from .contracts import DomainError, Principal, RunRequest, Scope, StrictModel


class Decision(StrictModel):
    scope: Scope
    proposal_hash: str
    expected_run_revision: int = Field(ge=0)
    decision: Literal["APPROVED", "REJECTED"]


def create_app(store, identities: dict[str, Principal], *, test_harness: bool = False) -> FastAPI:
    if os.environ.get("FORGEFLOW_MODE", "development") != "development":
        raise RuntimeError("DEV_ONLY adapter refuses production mode")
    app = FastAPI(title="ForgeFlow local DEV_ONLY runtime", version="0.1.0")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if not test_harness and (not request.client or request.client.host not in {"127.0.0.1", "::1"}):
            return JSONResponse({"code": "DEV_ONLY"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_handler(request, error: DomainError):
        return JSONResponse({"code": error.code, "message": error.message}, status_code=error.status)

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        if token not in identities:
            raise DomainError("UNAUTHENTICATED", "Use a generated local development token", 401)
        return identities[token]

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "identity": "DEV_ONLY",
            "llm": "NOT_CONNECTED",
            "external_actions": "LOCAL_FIXTURE_ONLY",
        }

    @app.post("/runs", status_code=202)
    def submit(
        body: RunRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        principal: Principal = Depends(identity),
    ):
        return store.submit(principal, body, idempotency_key)

    @app.get("/runs/{run_id}")
    def get_run(
        run_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = Depends(identity),
    ):
        return store.get_run(
            principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id), run_id
        )

    @app.get("/runs/{run_id}/events")
    def events(
        run_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = Depends(identity),
    ):
        return store.events(
            principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id), run_id
        )

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, body: Scope, principal: Principal = Depends(identity)):
        return store.cancel(principal, body, run_id)

    @app.get("/runs/{run_id}/report")
    def report(
        run_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = Depends(identity),
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        run = store.get_run(principal, scope, run_id)
        return {
            "run_id": run_id,
            "state": run["state"],
            "project": run["project"],
            "assessment": run.get("assessment_json"),
            "execution": run.get("result_json"),
            "approvals": run["approvals"],
            "receipts": run["receipts"],
            "truth": {
                "rules": "SYNTHETIC",
                "llm": "NOT_CONNECTED",
                "issue_tracker": "LOCAL_DB_ADAPTER",
                "identity": "DEV_ONLY",
            },
        }

    @app.get("/artifacts/{artifact_id}")
    def artifact(
        artifact_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = Depends(identity),
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        row = store.cite(principal, scope, "artifact", artifact_id)
        return Response(
            row["content"], media_type="application/json", headers={"X-Content-SHA256": row["content_sha256"]}
        )

    @app.post("/approvals/{approval_id}/decision")
    def decision(approval_id: str, body: Decision, principal: Principal = Depends(identity)):
        return store.decide(
            principal, body.scope, approval_id, body.proposal_hash, body.expected_run_revision, body.decision
        )

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1"}:
        parser.error("DEV_ONLY identity requires a loopback bind")
    import uvicorn

    from .local import load_identities, load_store

    uvicorn.run(create_app(load_store(), load_identities()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
