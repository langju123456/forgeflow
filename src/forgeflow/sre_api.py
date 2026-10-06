"""Loopback SRE receipt review API, using existing DEV identities."""

import os

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse

from .contracts import DomainError, Principal, Scope
from .sre_ops import REVISION, SREOperations


def create_sre_app(source, identities):
    service = SREOperations(source)
    service.create_schema()
    app = FastAPI(title="ForgeFlow SRE recorded operational readiness")

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse(status_code=error.status, content={"code": error.code})

    def identity(authorization: str | None = Header(default=None)) -> Principal:
        token = authorization.removeprefix("Bearer ") if authorization else ""
        principal = identities.get(token)
        if principal is None:
            raise DomainError("UNAUTHENTICATED", status=401)
        return principal

    principal_dependency = Depends(identity)

    @app.get("/health")
    def health():
        return {"identity": "DEV_ONLY", "role": "sre", "revision": REVISION,
                "execution": "DETERMINISTIC_SOP", "observation": "RECORDED_RECEIPTS_NOT_LIVE_PROBE",
                "model_calls": 0, "automatic_business_actions": False, "process_id": os.getpid()}

    @app.post("/runs/{run_id}/sre-review")
    def review(run_id: str, body: Scope, idempotency_key: str = Header(alias="Idempotency-Key"),
               principal: Principal = principal_dependency):
        return service.review(principal, body, run_id, idempotency_key)

    @app.get("/sre-reviews/{review_id}")
    def get(review_id: str, tenant_id: str, workspace_id: str, project_id: str,
            principal: Principal = principal_dependency):
        return service.get(principal, Scope(tenant_id=tenant_id, workspace_id=workspace_id, project_id=project_id), review_id)

    return app


def main():
    import uvicorn

    from .local import load_identities, load_store

    uvicorn.run(create_sre_app(load_store(), load_identities()), host="127.0.0.1", port=8772)


if __name__ == "__main__":
    main()
