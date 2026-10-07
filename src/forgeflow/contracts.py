"""Frozen, scope-bearing module contracts. No authority is accepted from model output."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(StrictModel):
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    workspace_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")

    def key(self) -> tuple[str, str, str]:
        return self.tenant_id, self.workspace_id, self.project_id


class Principal(StrictModel):
    subject_id: str
    kind: Literal["user", "agent", "service"]
    roles: frozenset[str]
    scopes: tuple[Scope, ...]
    clearance: int = Field(default=0, ge=0, le=3)

    def permits(self, scope: Scope, roles: set[str] | None = None, classification: int = 0) -> bool:
        return (
            scope in self.scopes
            and classification <= self.clearance
            and (roles is None or bool(self.roles & roles))
        )


class DomainError(Exception):
    def __init__(self, code: str, message: str = "", status: int = 409):
        self.code, self.message, self.status = code, message or code, status
        super().__init__(self.message)


def authorize(
    principal: Principal, scope: Scope, roles: set[str] | None = None, classification: int = 0
) -> None:
    if not principal.permits(scope, roles, classification):
        raise DomainError("NOT_FOUND", "Object unavailable", 404)


class RunRequest(StrictModel):
    scope: Scope
    stage: Literal["EVT"] = "EVT"
    fixture_id: Literal[
        "aurora-pass", "aurora-blocked", "aurora-unknown", "aurora-timeout",
        "aurora-pass-v2", "aurora-blocked-v2", "aurora-unknown-v2", "aurora-timeout-v2",
    ]
    fixture_version: Literal["v1"] = "v1"
    objective: str = Field(default="Assess Project Aurora EVT readiness", min_length=1, max_length=500)


class ExecutionSpec(StrictModel):
    scope: Scope
    run_id: str
    step_id: str
    attempt: int = Field(ge=1)
    template_id: Literal["evt-check", "timeout", "network-probe", "output-flood"]
    input_manifest: dict
    input_hash: str
    code_hash: str
    image: str
    timeout_seconds: float = Field(default=20, gt=0, le=30)
    profile: Literal["checker-v1"] = "checker-v1"


class SandboxArtifact(StrictModel):
    path: str
    sha256: str
    size: int
    content: str


class ExecutionResult(StrictModel):
    handle: str
    status: Literal["SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "OUTPUT_LIMIT", "UNAVAILABLE"]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    cleanup: Literal["REMOVED", "FAILED", "NOT_STARTED"]
    artifacts: tuple[SandboxArtifact, ...] = ()
    inspect: dict = Field(default_factory=dict)
    reason: str = ""


class ContextHit(StrictModel):
    id: str
    version: str
    content: str
    source_hash: str
    classification: int
    status: Literal["CURRENT", "EXPIRED", "CONFLICT", "CANDIDATE", "CONFIRMED"]


class SkillSnapshot(StrictModel):
    name: str
    role: str
    version: str
    content: str
    sha256: str
    allowed_tools: tuple[str, ...]
