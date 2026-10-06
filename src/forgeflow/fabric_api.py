"""Unified loopback API for isolated, approved local Fabric actions."""

import os
from typing import Literal

from fastapi import Depends, Header
from pydantic import Field

from .contracts import DomainError, Principal, Scope, StrictModel
from .evt_api import create_evt_app
from .evt_contract import DEFAULT_POLICY
from .fabric_contract import FABRIC_SCOPE, REVISION
from .fabric_store import FabricOperations


class FabricRequest(StrictModel):
    scope: Scope
    fixture_id: Literal["aligned", "bom-mismatch", "missing-exported"] = "aligned"


class FabricProposal(StrictModel):
    scope: Scope
    review_hash: str
    kind: Literal["TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE", "STAGE_REVERT", "ISSUE_RETRACT", "MEMORY_REVOKE"]
    target_receipt_id: str | None = None


class FabricDecision(StrictModel):
    scope: Scope
    proposal_hash: str
    expected_revision: int = Field(ge=0)
    decision: Literal["APPROVED", "REJECTED"]


def create_fabric_app(source, identities, factory, *, test_harness=False):
    app = create_evt_app(source, identities, factory, test_harness=test_harness)
    app.title = "ForgeFlow approved local synthetic Fabric prototype"
    parent_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/runs/{run_id}/report")
    app.router.routes = [route for route in app.router.routes if getattr(route, "path", None) not in
                         {"/health", "/runs/{run_id}/report"}]
    service = FabricOperations(source)
    service.create_schema()

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    def query_scope(tenant_id: str, workspace_id: str, project_id: str) -> Scope:
        return Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id)

    principal_dependency = Depends(identity)
    scope_dependency = Depends(query_scope)

    @app.get("/runs/{run_id}/report")
    def parent_report(run_id: str, scope: Scope = scope_dependency, principal: Principal = principal_dependency):
        result = parent_route.endpoint(run_id, scope.tenant_id, scope.workspace_id, scope.project_id, principal)
        if scope == FABRIC_SCOPE:
            result["truth"].update(stage_action_adapter="ISOLATED_LOCAL_TEST_STAGE_ONLY",
                                   issue_adapter="LOCAL_DB_ONLY", memory_adapter="APPROVED_REFERENCE_ONLY",
                                   approval_provenance="TEST_DRIVER", full_evt_eligibility=False)
            result["fabric_review_endpoint"] = "/fabric/runs/" + run_id + "/review"
            result["core_approvals"] = "DISABLED_FOR_FABRIC_SCOPE"
        return result

    @app.get("/health")
    def health():
        return {"identity": "DEV_ONLY", "revision": REVISION, "process_id": os.getpid(),
                "mode": "LOCAL_SYNTHETIC_PROTOTYPE", "fabric_scope": FABRIC_SCOPE.model_dump(mode="json"),
                "fabric_action_adapter": "LOCAL_SYNTHETIC_PARAMETER_BOUND_APPROVAL_REQUIRED",
                "evt_review_contract": "ENFORCED_FOR_CURRENT_HTTP_ACCEPTANCE", "default_policy": DEFAULT_POLICY,
                "hardware_only_approvals": "GRANTS_BLOCKED_REJECTION_ALLOWED",
                "model_roles": ["supervisor", "pm"], "deterministic_sops": ["hardware", "sre", "architect", "android"],
                "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "android_device": "NOT_RUN_NO_ANDROID_DEVICE",
                "full_evt_eligibility": False, "evt_stage_transition_allowed": False,
                "stage_action_adapter": "ISOLATED_LOCAL_TEST_STAGE_ONLY", "issue_adapter": "LOCAL_DB_ONLY",
                "memory_adapter": "ISOLATED_LOCAL_APPROVAL_REQUIRED", "human_approval_required": True,
                "automatic_business_actions": False}

    @app.post("/fabric/runs/{run_id}/review")
    def create(run_id: str, body: FabricRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
               principal: Principal = principal_dependency):
        return service.create(principal, body.scope, run_id, idempotency_key, body.fixture_id)

    @app.get("/fabric/reviews/{review_id}")
    def get(review_id: str, scope: Scope = scope_dependency, principal: Principal = principal_dependency):
        return service.get(principal, scope, review_id)

    @app.post("/fabric/reviews/{review_id}/proposals")
    def propose(review_id: str, body: FabricProposal, idempotency_key: str = Header(alias="Idempotency-Key"),
                principal: Principal = principal_dependency):
        return service.propose(principal, body.scope, review_id, body.review_hash, idempotency_key,
                               body.kind, target_receipt_id=body.target_receipt_id)

    @app.get("/fabric/approvals/{approval_id}")
    def get_approval(approval_id: str, scope: Scope = scope_dependency, principal: Principal = principal_dependency):
        return service.get_approval(principal, scope, approval_id)

    @app.post("/fabric/approvals/{approval_id}/decision")
    def decide(approval_id: str, body: FabricDecision, principal: Principal = principal_dependency):
        return service.decide(principal, body.scope, approval_id, body.proposal_hash,
                              body.expected_revision, body.decision)

    @app.post("/fabric/approvals/{approval_id}/execute")
    def execute(approval_id: str, body: Scope, principal: Principal = principal_dependency):
        if principal.kind != "service":
            raise DomainError("SERVICE_EXECUTION_REQUIRED", status=403)
        return service.execute(principal, body, approval_id)

    @app.get("/fabric/memory")
    def memory(scope: Scope = scope_dependency, query: str = "", principal: Principal = principal_dependency):
        return service.memory(principal, scope, query=query)

    return app


def main():
    import uvicorn

    from .fabric_runtime import load_fabric_identities, load_fabric_store
    from .local import load_identities
    from .s2c_runtime import S2CRuntimeFactory

    source = load_fabric_store()
    factory = S2CRuntimeFactory(store=source)
    identities = load_identities() | load_fabric_identities()
    uvicorn.run(create_fabric_app(source, identities, factory), host="127.0.0.1", port=8773)


if __name__ == "__main__":
    main()
