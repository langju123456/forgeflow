"""Constrained model selections checked against a server-derived evidence catalog.

Schema validity and semantic coverage are separate results. A valid source for a
generic rule never becomes an instance observation. Human-readable prose is a
deterministic rendering of validated claims, not additional model assertions.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .analysis_contracts import AnalysisOutput, parse_output, strict_json_object
from .contracts import DomainError, digest

QUESTION = "Review all five EVT checks and human approval"
REVISION = "s2b-grounded-selection-v1"
ClaimRef = Annotated[str, Field(pattern=r"^c_[a-f0-9]{16}$")]
Observations = Annotated[list[ClaimRef], Field(min_length=5, max_length=5)]
Concerns = Annotated[list[ClaimRef], Field(max_length=8)]


class GroundedReport(AnalysisOutput):
    status: Literal["COMPLETE", "INCOMPLETE"]
    observations: Observations
    concerns: Concerns
    recommendation: Literal["HUMAN_REVIEW_REQUIRED"]


class GroundedSummary(AnalysisOutput):
    status: Literal["COMPLETE", "INCOMPLETE"]
    report_refs: Annotated[list[Literal["report_pm"]], Field(min_length=1, max_length=1)]
    observations: Observations
    concerns: Concerns
    human_approval_required: Literal[True]


MODELS = {"report": GroundedReport, "summary": GroundedSummary}


def schema(phase: str) -> dict:
    if phase not in MODELS:
        from .analysis_contracts import compact_schema

        result = compact_schema(phase)
        if phase == "plan":
            result["properties"]["task_question"] = {"type": "string", "const": QUESTION}
        return result

    def compact(value):
        if isinstance(value, dict):
            return {k: compact(v) for k, v in value.items() if k not in {"title", "description"}}
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value

    return compact(MODELS[phase].model_json_schema())


def structural_output(phase: str, raw):
    if phase not in MODELS:
        return parse_output(phase, raw)
    try:
        return MODELS[phase].model_validate(strict_json_object(raw))
    except ValidationError as error:
        raise DomainError("INVALID_MODEL_OUTPUT", status=422) from error


def expected_status(catalog: dict) -> str:
    # An untrusted instruction is a security concern rather than a measurement
    # gap; it cannot change the authoritative technical checks.
    return "INCOMPLETE" if any(
        item["check_state"] != "SATISFIED" or item["evidence_state"] != "MEASURED"
        for item in catalog["observations"]
    ) else "COMPLETE"


def semantic_check(catalog: dict, phase: str, raw) -> dict:
    try:
        parsed = structural_output(phase, raw)
    except DomainError as error:
        return {"structural_valid": False, "semantic_valid": None, "codes": [error.code]}
    data = parsed.model_dump(mode="json")
    codes = []
    if phase == "plan":
        if data["task_question"] != QUESTION:
            codes.append("UNGROUNDED_DELEGATED_QUESTION")
    elif phase in MODELS:
        for field in ("observations", "concerns"):
            expected = {item["id"] for item in catalog[field]}
            actual = data[field]
            if len(actual) != len(set(actual)):
                codes.append("DUPLICATE_" + field.upper())
            if set(actual) != expected:
                codes.append("EVIDENCE_COVERAGE_" + field.upper())
        if data["status"] != expected_status(catalog):
            codes.append("UNSUPPORTED_COMPLETION_STATUS")
    return {"structural_valid": True, "semantic_valid": not codes, "codes": codes}


def validate_output(catalog: dict, phase: str, raw):
    result = semantic_check(catalog, phase, raw)
    if not result["structural_valid"]:
        raise DomainError("INVALID_MODEL_OUTPUT", status=422)
    if not result["semantic_valid"]:
        raise DomainError("UNGROUNDED_MODEL_OUTPUT", status=422)
    return structural_output(phase, raw)


def render(catalog: dict, output: dict) -> dict:
    """No arbitrary model prose enters this accepted report."""
    verdict = expected_status(catalog)
    return {
        "renderer": REVISION,
        "model_task": "CONSTRAINED_EVIDENCE_SELECTION",
        "catalog_hash": digest(catalog),
        "status": verdict,
        "observations": catalog["observations"],
        "concerns": catalog["concerns"],
        "human_approval_required": True,
        "summary": (
            "五项检查均有满足示例规则的测量证据；仍需人工审批。"
            if verdict == "COMPLETE"
            else "检查存在未满足、缺失、过期或冲突的证据；需人工核查，不能推进阶段。"
        ),
        "validated_selection": output,
        "limitations": "合成研发规则；不是硬件认证。模型未取得批准或外部写入权限。",
    }
