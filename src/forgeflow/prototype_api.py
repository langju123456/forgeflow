"""Single loopback API for the local prototype and supplemental role SOPs."""

import os
from typing import Literal

from fastapi import Depends, Header

from .contracts import DomainError, Principal, Scope, StrictModel
from .domain_store import REVISION, DomainOperations
from .s2c_api import create_s2c_app
from .sre_api import create_sre_app


class DomainRequest(StrictModel):
    scope: Scope
    fixture_id: Literal["aligned", "bom-mismatch", "missing-exported"] = "aligned"


def create_prototype_app(source, identities, factory, *, test_harness=False):
    app = create_s2c_app(source, identities, factory, test_harness=test_harness)
    app.title = "ForgeFlow local prototype"
    app.router.routes = [route for route in app.router.routes
                         if getattr(route, "path", None) not in {"/health", "/runs/{run_id}/report"}]
    sre_app = create_sre_app(source, identities)
    sre_app.router.routes = [route for route in sre_app.router.routes if getattr(route, "path", None) not in {
        "/health", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}]
    app.include_router(sre_app.router)
    service = DomainOperations(source)
    service.create_schema()

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/health")
    def health():
        return {"identity": "DEV_ONLY", "revision": REVISION, "process_id": os.getpid(),
                "mode": "LOCAL_SYNTHETIC_PROTOTYPE", "model_roles": ["supervisor", "pm"],
                "deterministic_sops": ["hardware", "sre", "architect", "android"],
                "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "automatic_business_actions": False}

    @app.post("/runs/{run_id}/domain-review")
    def review(run_id: str, body: DomainRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
               principal: Principal = principal_dependency):
        return service.review(principal, body.scope, run_id, idempotency_key, body.fixture_id)

    @app.get("/runs/{run_id}/report")
    def report(run_id: str, tenant_id: str, workspace_id: str, project_id: str,
               principal: Principal = principal_dependency):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        run = source.get_run(principal, scope, run_id)
        return {"run_id": run_id, "state": run["state"], "project": run["project"],
                "assessment": run.get("assessment_json"), "execution": run.get("result_json"),
                "approvals": run["approvals"], "receipts": run["receipts"],
                "truth": {"rules": "SYNTHETIC", "core_check": "DETERMINISTIC_MCP_SANDBOX_WORKFLOW",
                          "llm": "OPTIONAL_LOCAL_LINKED_ANALYSIS_REPORTED_SEPARATELY",
                          "domain_sop": "SUPPLEMENTAL_STATIC_SOURCE_REVIEW",
                          "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "issue_tracker": "LOCAL_DB_ADAPTER"}}

    @app.get("/domain-reviews/{review_id}")
    def get(review_id: str, tenant_id: str, workspace_id: str, project_id: str,
            principal: Principal = principal_dependency):
        return service.get(principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id), review_id)

    return app


def main():
    import uvicorn

    from .local import load_identities
    from .s2c_runtime import S2CRuntimeFactory

    factory = S2CRuntimeFactory()
    uvicorn.run(create_prototype_app(factory.source, load_identities(), factory), host="127.0.0.1", port=8773)


if __name__ == "__main__":
    main()
