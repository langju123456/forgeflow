"""S2 model proposals contain data, never control-plane authority."""

from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contracts import DomainError


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("task_question", "query", "text", "summary", check_fields=False)
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Nonblank text required")
        return value


Reference = Annotated[str, Field(pattern=r"^src_[a-f0-9]{32}$")]
SourceRefs = Annotated[list[Reference], Field(min_length=1, max_length=5)]
Text = Annotated[str, Field(min_length=1, max_length=500)]


class RolePlan(AnalysisOutput):
    assignment: Literal["pm"]
    task_question: Annotated[str, Field(min_length=1, max_length=100)]
    source_refs: SourceRefs


class SearchRequest(AnalysisOutput):
    tool: Literal["knowledge.search", "memory.search"]
    query: Annotated[str, Field(min_length=1, max_length=200)]
    top_k: Annotated[int, Field(ge=1, le=5)]


class ContextProposal(AnalysisOutput):
    requests: Annotated[list[SearchRequest], Field(max_length=2)]


class CitedStatement(AnalysisOutput):
    text: Annotated[str, Field(min_length=1, max_length=80)]
    source_refs: Annotated[list[Reference], Field(min_length=1, max_length=1)]


Facts = Annotated[list[CitedStatement], Field(max_length=3)]
SingleStatement = Annotated[list[CitedStatement], Field(max_length=1)]


class RoleReport(AnalysisOutput):
    status: Literal["COMPLETE", "INCOMPLETE"]
    facts: Facts
    risks: SingleStatement
    unknowns: SingleStatement
    recommendations: SingleStatement


class SupervisorSummary(AnalysisOutput):
    status: Literal["COMPLETE", "INCOMPLETE"]
    summary: Text
    source_refs: SourceRefs
    report_refs: Annotated[list[str], Field(min_length=1, max_length=1)]


OUTPUT_MODELS = {
    "plan": RolePlan,
    "context": ContextProposal,
    "report": RoleReport,
    "summary": SupervisorSummary,
}


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError("Non-finite JSON number")


def _json_value(value: object, depth: int = 0) -> None:
    if depth > 30:
        raise ValueError("JSON nesting limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item, depth + 1)
        return
    raise ValueError("Not a finite JSON value")


def strict_json_object(raw: str | dict) -> dict:
    """Reject markdown, duplicate keys, NaN/Infinity and non-object output."""
    try:
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > 65536:
                raise ValueError("JSON output size limit")
            data = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        elif type(raw) is dict:
            data = raw
        else:
            raise ValueError("JSON object required")
        if type(data) is not dict:
            raise ValueError("JSON object required")
        _json_value(data)
        if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 65536:
            raise ValueError("JSON output size limit")
        return data
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise DomainError(
            "INVALID_MODEL_OUTPUT", "Model output is not a bounded strict JSON object", 422
        ) from error


def parse_output(phase: str, raw: str | dict) -> AnalysisOutput:
    model = OUTPUT_MODELS.get(phase)
    if model is None:
        raise DomainError("INVALID_ANALYSIS_PHASE", status=422)
    try:
        return model.model_validate(strict_json_object(raw))
    except ValidationError as error:
        # Do not copy an untrusted output into an error/log/prompt repair loop.
        raise DomainError("INVALID_MODEL_OUTPUT", "Model output violates the phase schema", 422) from error


def compact_schema(phase: str) -> dict:
    """Keep every validation constraint while removing presentation metadata."""
    model = OUTPUT_MODELS.get(phase)
    if model is None:
        raise DomainError("INVALID_ANALYSIS_PHASE", status=422)

    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items() if key not in {"title", "description"}}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return compact(model.model_json_schema())
