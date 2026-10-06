"""S2C model selections and explicitly server-derived evidence state.

Only the trusted context builder derives and authorizes the catalog. The model
selects its existing claim identities; it never supplies technical completion,
approval or execution state. Internal legacy projections are compatibility data,
not provider output, and must never replace the raw journal or call.output_json.
"""

from __future__ import annotations

import copy
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .analysis_contracts import AnalysisOutput, parse_output, strict_json_object
from .contracts import DomainError, digest
from .gate import RULES
from .grounded_contracts import QUESTION, GroundedReport, GroundedSummary
from .grounded_contracts import schema as legacy_schema

REVISION = "s2c-server-derived-state-v1"
STATUS_AUTHORITY = "SERVER_DERIVED_AUTHORIZED_CATALOG"
ClaimRef = Annotated[str, Field(pattern=r"^c_[a-f0-9]{16}$")]
SourceRef = Annotated[str, Field(pattern=r"^src_[a-f0-9]{32}$")]
Observations = Annotated[list[ClaimRef], Field(min_length=5, max_length=5)]
Concerns = Annotated[list[ClaimRef], Field(max_length=8)]


class SelectionReport(AnalysisOutput):
    observations: Observations
    concerns: Concerns
    recommendation: Literal["HUMAN_REVIEW_REQUIRED"]


class SelectionSummary(AnalysisOutput):
    observations: Observations
    concerns: Concerns
    report_refs: Annotated[list[Literal["report_pm"]], Field(min_length=1, max_length=1)]
    human_approval_required: Literal[True]


class CatalogObservation(AnalysisOutput):
    id: ClaimRef
    source_ref: SourceRef
    metric: Annotated[str, Field(min_length=1, max_length=64)]
    value: int | float | str | bool | None
    check_state: Literal["SATISFIED", "VIOLATED", "UNKNOWN"]
    evidence_state: Literal["MEASURED", "MISSING", "EXPIRED", "CONFLICT", "UNCONFIRMED"]
    authority: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]


class CatalogConcern(AnalysisOutput):
    id: ClaimRef
    source_ref: SourceRef
    kind: Annotated[str, Field(min_length=1, max_length=96)]


class Catalog(AnalysisOutput):
    observations: Annotated[list[CatalogObservation], Field(min_length=5, max_length=5)]
    concerns: Annotated[list[CatalogConcern], Field(max_length=8)]


MODELS = {"report": SelectionReport, "summary": SelectionSummary}


def checked_catalog(catalog: dict) -> dict:
    """Shape sanity, not source authorization or a substitute for derivation."""
    try:
        data = Catalog.model_validate(strict_json_object(catalog)).model_dump(mode="json")
    except (DomainError, ValidationError) as error:
        raise DomainError("INVALID_S2C_CATALOG", status=422) from error
    metrics = [item["metric"] for item in data["observations"]]
    identities = [item["id"] for field in ("observations", "concerns") for item in data[field]]
    if set(metrics) != set(RULES["required"]) or len(identities) != len(set(identities)):
        raise DomainError("INVALID_S2C_CATALOG", status=422)
    if any(item["check_state"] == "SATISFIED" and item["value"] is None for item in data["observations"]):
        raise DomainError("INVALID_S2C_CATALOG", status=422)
    return data


def derive_status(catalog: dict) -> str:
    catalog = checked_catalog(catalog)
    return "COMPLETE" if all(
        item["check_state"] == "SATISFIED" and item["evidence_state"] == "MEASURED"
        for item in catalog["observations"]
    ) else "INCOMPLETE"


def server_projection(catalog: dict) -> dict:
    return {"status": derive_status(catalog), "status_authority": STATUS_AUTHORITY, "catalog_hash": digest(catalog)}


def schema(phase: str) -> dict:
    if phase not in MODELS:
        return legacy_schema(phase)

    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items() if key not in {"title", "description"}}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return compact(MODELS[phase].model_json_schema())


def schema_for_catalog(phase: str, catalog: dict) -> dict:
    catalog = checked_catalog(catalog)
    result = schema(phase)
    if phase in MODELS:
        for field in ("observations", "concerns"):
            identities = [item["id"] for item in catalog[field]]
            definition = result["properties"][field]
            if identities:
                definition["items"]["enum"] = identities
            else:
                definition["maxItems"] = 0
    return result


def structural_output(phase: str, raw):
    if phase not in MODELS:
        return parse_output(phase, raw)
    try:
        return MODELS[phase].model_validate(strict_json_object(raw))
    except ValidationError as error:
        raise DomainError("INVALID_MODEL_OUTPUT", status=422) from error


def semantic_check(catalog: dict, phase: str, raw) -> dict:
    try:
        catalog = checked_catalog(catalog)
        parsed = structural_output(phase, raw)
    except DomainError as error:
        return {"structural_valid": False, "semantic_valid": None, "codes": [error.code]}
    data = parsed.model_dump(mode="json")
    codes = []
    if phase == "plan" and data["task_question"] != QUESTION:
        codes.append("UNGROUNDED_DELEGATED_QUESTION")
    elif phase in MODELS:
        for field in ("observations", "concerns"):
            expected = {item["id"] for item in catalog[field]}
            if len(data[field]) != len(set(data[field])):
                codes.append("DUPLICATE_" + field.upper())
            if set(data[field]) != expected:
                codes.append("EVIDENCE_COVERAGE_" + field.upper())
    return {"structural_valid": True, "semantic_valid": not codes, "codes": codes}


def validate_output(catalog: dict, phase: str, raw):
    verdict = semantic_check(catalog, phase, raw)
    if not verdict["structural_valid"]:
        raise DomainError("INVALID_MODEL_OUTPUT", status=422)
    if verdict["semantic_valid"] is not True:
        raise DomainError("UNGROUNDED_MODEL_OUTPUT", status=422)
    return structural_output(phase, raw)


def project(phase: str, selection, catalog: dict):
    """Legacy engine compatibility only; never persisted as provider output."""
    raw = selection.model_dump(mode="json") if hasattr(selection, "model_dump") else selection
    parsed = validate_output(catalog, phase, raw)
    if phase not in MODELS:
        return parsed
    model = GroundedReport if phase == "report" else GroundedSummary
    return model.model_validate({**parsed.model_dump(mode="json"), "status": derive_status(catalog)})


def internal_projection(catalog: dict, phase: str, raw):
    return project(phase, raw, catalog)


def render(catalog: dict, output: dict) -> dict:
    """Render a validated statusless selection using only trusted catalog facts."""
    phase = "report" if "recommendation" in output else "summary"
    parsed = validate_output(catalog, phase, output)
    verdict = server_projection(catalog)
    return {
        "renderer": REVISION,
        "model_task": "EVIDENCE_SELECTION_ONLY",
        **verdict,
        "server_projection": verdict,
        "observations": copy.deepcopy(catalog["observations"]),
        "concerns": copy.deepcopy(catalog["concerns"]),
        "human_approval_required": True,
        "summary": (
            "服务器依据已授权来源判定：五项合成检查证据满足规则；仍需人工审批。"
            if verdict["status"] == "COMPLETE" else
            "服务器依据已授权来源判定：证据存在未满足、缺失、过期或冲突；不能推进阶段。"
        ),
        "validated_selection": parsed.model_dump(mode="json"),
        "limitations": "状态由服务器推导，模型仅选择已有证据。合成规则不是硬件认证；人工审批及执行单独记录。",
    }
