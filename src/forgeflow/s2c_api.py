"""Separate loopback S2C DEV API on 8771, retaining prior service versions."""

import os

from fastapi import Depends, Header

from .analysis_provider import MODEL, MODEL_DIGEST
from .contracts import DomainError, Principal, Scope, authorize
from .grounded_api import GroundedRequest, create_grounded_app
from .s2c_contracts import REVISION
from .s2c_workflow import PROMPT_REVISION


def create_s2c_app(store, identities, factory, *, test_harness=False):
    app = create_grounded_app(store, identities, factory, test_harness=test_harness)
    app.title = "ForgeFlow S2C evidence selection and server-derived state"
    app.router.routes = [route for route in app.router.routes
                         if getattr(route, "path", None) not in {"/health", "/grounded/health"}]

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/s2c/health")
    @app.get("/grounded/health")
    @app.get("/health")
    def health():
        return {"identity": "DEV_ONLY", "analysis_revision": REVISION, "prompt_revision": PROMPT_REVISION,
                "registered_model": MODEL, "pinned_digest": MODEL_DIGEST, "model_task": "EVIDENCE_SELECTION_ONLY",
                "provider_live_status": "CHECK_VIA_PREFLIGHT_AND_ACTUAL_CALL_EVIDENCE",
                "status_authority": "SERVER_DERIVED_AUTHORIZED_CATALOG", "automatic_business_actions": False,
                "process_id": os.getpid()}

    @app.post("/s2c/parents", status_code=202)
    def submit(body: GroundedRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
               principal: Principal = principal_dependency):
        return factory.submit_parent(principal, body.scenario_id, idempotency_key, scope=body.scope)

    @app.post("/s2c/parents/{run_id}/check")
    async def check(run_id: str, body: Scope, principal: Principal = principal_dependency):
        authorize(principal, body, {"hardware"})
        if principal.kind != "user":
            raise DomainError("HUMAN_CHECK_REQUEST_REQUIRED", status=403)
        parent = store.get_run(principal, body, run_id)
        return await factory.check_parent(body, parent)

    return app


def main():
    import uvicorn

    from .local import load_identities
    from .s2c_runtime import S2CRuntimeFactory

    factory = S2CRuntimeFactory()
    uvicorn.run(create_s2c_app(factory.source, load_identities(), factory), host="127.0.0.1", port=8771)


if __name__ == "__main__":
    main()
