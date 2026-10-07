"""Unified local API with bound EVT reviews instead of hardware-only approvals."""

import os
from typing import Literal

from fastapi import Depends, Header
from pydantic import Field

from .api import Decision
from .contracts import DomainError, Principal, Scope, StrictModel, authorize
from .evt_contract import DEFAULT_POLICY
from .evt_review import REVISION, EVTReviews
from .evt_store import EVTBoundStore
from .prototype_api import create_prototype_app


class EVTRequest(StrictModel):
    scope: Scope
    domain_review_id: str
    sre_review_id: str
    policy_id: str = DEFAULT_POLICY


class AcceptanceRequest(StrictModel):
    scope: Scope
    review_hash: str


class AcceptanceDecision(StrictModel):
    scope: Scope
    proposal_hash: str
    expected_revision: int = Field(ge=0)
    decision: Literal["APPROVED", "REJECTED"]


def create_evt_app(source, identities, factory, *, test_harness=False):
    if not isinstance(source, EVTBoundStore):
        # The legacy-approval gate and its audit live in the Store; never serve a store without them.
        raise RuntimeError("EVT_BOUND_STORE_REQUIRED")
    app = create_prototype_app(source, identities, factory, test_harness=test_harness)
    app.title = "ForgeFlow bound synthetic EVT review prototype"
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) not in {
        "/health", "/approvals/{approval_id}/decision", "/runs/{run_id}/report"}]
    service = EVTReviews(source)
    service.create_schema()

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        principal = identities.get(authorization.removeprefix("Bearer ") if authorization else "")
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/health")
    def health():
        return {"identity": "DEV_ONLY", "revision": REVISION, "process_id": os.getpid(),
                "mode": "LOCAL_SYNTHETIC_PROTOTYPE", "evt_review_contract": "ENFORCED_FOR_CURRENT_HTTP_ACCEPTANCE",
                "default_policy": DEFAULT_POLICY, "hardware_only_approvals": "GRANTS_BLOCKED_REJECTION_ALLOWED",
                "model_roles": ["supervisor", "pm"], "deterministic_sops": ["hardware", "sre", "architect", "android"],
                "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "stage_action_adapter": "NOT_MIGRATED",
                "automatic_business_actions": False}

    @app.get("/runs/{run_id}/report")
    def parent_report(run_id: str, tenant_id: str, workspace_id: str, project_id: str,
                      principal: Principal = principal_dependency):
        scope = Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)
        run = source.get_run(principal, scope, run_id)
        return {"run_id": run_id, "state": run["state"], "project": run["project"],
                "assessment": run.get("assessment_json"), "assessment_scope": "SYNTHETIC_HARDWARE_ONLY",
                "execution": run.get("result_json"), "approvals": run["approvals"], "receipts": run["receipts"],
                "evt_review_endpoint": "/evt-reviews/{review_id}",
                "truth": {"rules": "SYNTHETIC", "llm": "OPTIONAL_LINKED_LOCAL_ANALYSIS",
                          "evt_review_contract": "ENFORCED_FOR_BOUND_REVIEW_ACCEPTANCE",
                          "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "stage_action_adapter": "NOT_MIGRATED"}}

    @app.post("/runs/{run_id}/evt-review")
    def create(run_id: str, body: EVTRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
               principal: Principal = principal_dependency):
        return service.create(principal, body.scope, run_id, idempotency_key, body.domain_review_id,
                              body.sre_review_id, body.policy_id)

    @app.get("/evt-reviews/{review_id}")
    def get(review_id: str, tenant_id: str, workspace_id: str, project_id: str,
            principal: Principal = principal_dependency):
        return service.get(principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id), review_id)

    @app.post("/evt-reviews/{review_id}/approval")
    def request_approval(review_id: str, body: AcceptanceRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
                         principal: Principal = principal_dependency):
        return service.request_approval(principal, body.scope, review_id, body.review_hash, idempotency_key)

    @app.post("/evt-approvals/{approval_id}/decision")
    def decide(approval_id: str, body: AcceptanceDecision, principal: Principal = principal_dependency):
        return service.decide(principal, body.scope, approval_id, body.proposal_hash, body.expected_revision, body.decision)

    @app.get("/evt-approvals/{approval_id}")
    def get_approval(approval_id: str, tenant_id: str, workspace_id: str, project_id: str,
                     principal: Principal = principal_dependency):
        return service.get_approval(principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id),
                                    approval_id)

    @app.post("/approvals/{approval_id}/decision")
    def legacy_decision(approval_id: str, body: Decision, principal: Principal = principal_dependency):
        authorize(principal, body.scope, {"reviewer"})
        # The Store rejects APPROVED (409) and audits the attempt; REJECTED stays available.
        return source.decide(principal, body.scope, approval_id, body.proposal_hash,
                             body.expected_run_revision, body.decision)

    return app


def main():
    import uvicorn

    from .local import load_identities
    from .s2c_runtime import S2CRuntimeFactory

    factory = S2CRuntimeFactory()
    uvicorn.run(create_evt_app(factory.source, load_identities(), factory), host="127.0.0.1", port=8773)


if __name__ == "__main__":
    main()
