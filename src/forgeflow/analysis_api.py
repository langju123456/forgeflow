"""Add source-authorized S2 routes to the existing local FastAPI application."""

from fastapi import Depends, Header
from pydantic import Field

from .analysis_provider import MODEL, MODEL_DIGEST
from .api import create_app
from .contracts import DomainError, Principal, Scope, StrictModel


class AnalysisRequest(StrictModel):
    scope: Scope
    run_id: str = Field(min_length=1, max_length=64)


def create_analysis_app(store, identities, workflow, *, test_harness=False):
    app = create_app(store, identities, test_harness=test_harness)
    app.title = "ForgeFlow local S1 + bounded S2 analysis"

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/analysis/health")
    def health():
        return {
            "identity": "DEV_ONLY",
            "profile": "SYNTHETIC_LOCAL_QWEN3",
            "registered_model": MODEL,
            "pinned_digest": MODEL_DIGEST,
            "provider_live_status": "CHECK_VIA_PREFLIGHT_AND_ACTUAL_CALL_EVIDENCE",
            "hardware_orchestration": "DETERMINISTIC_S1",
            "automatic_business_actions": False,
        }

    @app.post("/analyses")
    async def analyze(
        body: AnalysisRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        principal: Principal = principal_dependency,
    ):
        return await workflow.analyze(principal, body.scope, body.run_id, idempotency_key)

    @app.get("/analyses/{analysis_id}")
    def show(
        analysis_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = principal_dependency,
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        return workflow.store.get(principal, scope, analysis_id, reauthorize=workflow._reauthorize)

    @app.get("/analyses/{analysis_id}/events")
    def events(
        analysis_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = principal_dependency,
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        return workflow.store.events(principal, scope, analysis_id, reauthorize=workflow._reauthorize)

    @app.post("/analyses/{analysis_id}/cancel")
    def cancel(analysis_id: str, scope: Scope, principal: Principal = principal_dependency):
        return workflow.store.cancel(principal, scope, analysis_id)

    return app


def main():
    import uvicorn

    from .analysis_runtime import build_runtime
    from .local import load_identities

    store, workflow = build_runtime()
    # Do not offer a command-line public host override for the DEV_ONLY identity adapter.
    uvicorn.run(create_analysis_app(store, load_identities(), workflow), host="127.0.0.1", port=8769)


if __name__ == "__main__":
    main()
