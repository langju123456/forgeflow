"""Loopback DEV API for constrained grounded analysis; no model authority inputs."""

import os
from typing import Literal

from fastapi import Depends, Header

from .analysis_provider import MODEL, MODEL_DIGEST
from .api import create_app
from .contracts import DomainError, Principal, Scope, StrictModel
from .grounded_contracts import REVISION
from .grounded_runtime import GroundedRuntimeFactory

Scenario = Literal["M01", "M01-blocked", "M02", "M03", "M04"]


class GroundedRequest(StrictModel):
    # A selector, not an authority grant. The generated identity must authorize
    # the complete parent run and every source before any model dispatch.
    scope: Scope
    scenario_id: Scenario = "M01"


def create_grounded_app(store, identities, factory, *, test_harness=False):
    app = create_app(store, identities, test_harness=test_harness)
    app.title = "ForgeFlow local S1 + constrained grounded analysis"

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/grounded/health")
    def health():
        return {
            "identity": "DEV_ONLY",
            "analysis_revision": REVISION,
            "registered_model": MODEL,
            "pinned_digest": MODEL_DIGEST,
            "provider_live_status": "CHECK_VIA_PREFLIGHT_AND_ACTUAL_CALL_EVIDENCE",
            "model_task": "CONSTRAINED_EVIDENCE_SELECTION",
            "accepted_prose": "DETERMINISTIC_SERVER_RENDERER",
            "automatic_business_actions": False,
            "external_actions": "LOCAL_FIXTURE_ONLY",
            "process_id": os.getpid(),
        }

    @app.post("/runs/{run_id}/grounded-analysis")
    async def analyze(
        run_id: str,
        body: GroundedRequest,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        principal: Principal = principal_dependency,
    ):
        return await factory(body.scenario_id).analyze(principal, body.scope, run_id, idempotency_key)

    @app.get("/grounded-analyses/{analysis_id}")
    def show(
        analysis_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = principal_dependency,
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        return factory.persistence.get(principal, scope, analysis_id, reauthorize=factory.reauthorize)

    @app.get("/grounded-analyses/{analysis_id}/records")
    def records(
        analysis_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = principal_dependency,
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        return factory.grounding.list(principal, scope, analysis_id, reauthorize=factory.reauthorize)

    @app.get("/grounded-analyses/{analysis_id}/events")
    def events(
        analysis_id: str,
        tenant_id: str,
        workspace_id: str,
        project_id: str,
        principal: Principal = principal_dependency,
    ):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        return factory.persistence.events(principal, scope, analysis_id, reauthorize=factory.reauthorize)

    @app.post("/grounded-analyses/{analysis_id}/cancel")
    def cancel(analysis_id: str, scope: Scope, principal: Principal = principal_dependency):
        factory.persistence.get(principal, scope, analysis_id, reauthorize=factory.reauthorize)
        return factory.persistence.cancel(principal, scope, analysis_id)

    return app


def main():
    import uvicorn

    from .local import load_identities

    factory = GroundedRuntimeFactory()
    uvicorn.run(
        create_grounded_app(factory.source, load_identities(), factory), host="127.0.0.1", port=8770
    )


if __name__ == "__main__":
    main()
