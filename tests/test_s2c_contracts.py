"""S2C selection-only outputs and independent server evidence state."""

import copy

import pytest
from test_grounded_contracts import catalog

from forgeflow.contracts import DomainError, digest
from forgeflow.s2c_contracts import (
    STATUS_AUTHORITY,
    derive_status,
    internal_projection,
    project,
    render,
    schema_for_catalog,
    semantic_check,
    validate_output,
)


def selection(data, phase="report"):
    result = {"observations": [item["id"] for item in data["observations"]],
              "concerns": [item["id"] for item in data["concerns"]]}
    if phase == "report":
        result["recommendation"] = "HUMAN_REVIEW_REQUIRED"
    else:
        result.update(report_refs=["report_pm"], human_approval_required=True)
    return result


@pytest.mark.parametrize("phase", ["report", "summary"])
@pytest.mark.parametrize("field", ["status", "gate_status", "approval_decision", "stage_transition", "scope", "role", "code", "summary"])
def test_model_cannot_supply_state_authority_or_free_claims(phase, field):
    data = catalog()
    raw = {**selection(data, phase), field: "COMPLETE"}
    result = semantic_check(data, phase, raw)
    assert result["structural_valid"] is False and result["semantic_valid"] is None
    assert field not in schema_for_catalog(phase, data)["properties"]


def test_security_concern_and_pending_human_review_do_not_redefine_measurements():
    data = catalog()
    data["concerns"] = [{"id": "c_" + "f" * 16, "source_ref": "src_" + "b" * 32,
                         "kind": "UNTRUSTED_INSTRUCTION"}]
    raw = selection(data)
    assert validate_output(data, "report", raw).model_dump(mode="json") == raw
    rendered = render(data, raw)
    assert rendered["status"] == "COMPLETE"
    assert rendered["status_authority"] == STATUS_AUTHORITY
    assert rendered["model_task"] == "EVIDENCE_SELECTION_ONLY"
    assert rendered["human_approval_required"] is True
    assert rendered["concerns"] == data["concerns"]
    assert "status" not in rendered["validated_selection"]


@pytest.mark.parametrize("check,evidence", [("UNKNOWN", "MISSING"), ("UNKNOWN", "EXPIRED"),
                                            ("UNKNOWN", "CONFLICT"), ("UNKNOWN", "UNCONFIRMED"),
                                            ("VIOLATED", "MEASURED")])
def test_unresolved_or_failed_evidence_has_server_incomplete_state(check, evidence):
    data = catalog()
    data["observations"][3].update(check_state=check, evidence_state=evidence)
    raw = selection(data)
    assert semantic_check(data, "report", raw) == {"structural_valid": True, "semantic_valid": True, "codes": []}
    assert derive_status(data) == "INCOMPLETE"
    assert render(data, raw)["server_projection"]["status"] == "INCOMPLETE"
    assert "status" not in raw


@pytest.mark.parametrize("change", [
    lambda data: data.update(observations=[]),
    lambda data: data["observations"].pop(),
    lambda data: data["observations"][4].update(metric="ci_smoke"),
    lambda data: data["observations"][4].update(metric="not_an_evt_rule"),
    lambda data: data["observations"][4].update(check_state="PASS"),
    lambda data: data["observations"][4].update(value=None),
    lambda data: data["observations"][4].update(id=data["observations"][0]["id"]),
])
def test_invalid_or_incomplete_catalog_never_derives_complete(change):
    data = catalog()
    change(data)
    with pytest.raises(DomainError) as error:
        derive_status(data)
    assert error.value.code == "INVALID_S2C_CATALOG"


def test_coverage_and_duplicates_are_distinct_from_schema_validity():
    data = catalog()
    raw = selection(data)
    raw["observations"][4] = raw["observations"][0]
    assert semantic_check(data, "report", raw) == {
        "structural_valid": True, "semantic_valid": False,
        "codes": ["DUPLICATE_OBSERVATIONS", "EVIDENCE_COVERAGE_OBSERVATIONS"],
    }
    raw = selection(data)
    raw["concerns"] = ["c_" + "f" * 16]
    assert semantic_check(data, "report", raw)["codes"] == ["EVIDENCE_COVERAGE_CONCERNS"]


@pytest.mark.parametrize("phase", ["report", "summary"])
def test_internal_projection_keeps_provider_selection_original(phase):
    data = catalog()
    raw = selection(data, phase)
    before = copy.deepcopy(raw)
    projected = project(phase, raw, data).model_dump(mode="json")
    assert projected == {**before, "status": "COMPLETE"}
    assert internal_projection(data, phase, raw).model_dump(mode="json") == projected
    assert raw == before and "status" not in raw
    with pytest.raises(DomainError):
        validate_output(data, phase, projected)


def test_renderer_preserves_source_values_and_raw_selection_without_aliases():
    data = catalog()
    raw = selection(data)
    before_catalog, before_raw = copy.deepcopy(data), copy.deepcopy(raw)
    rendered = render(data, raw)
    assert rendered["catalog_hash"] == digest(data)
    assert rendered["validated_selection"] == before_raw
    rendered["observations"][0]["value"] = 999
    rendered["validated_selection"]["observations"].clear()
    assert data == before_catalog and raw == before_raw


def test_schema_enum_is_permission_bound_and_empty_concerns_have_no_invalid_enum():
    data = catalog()
    for phase in ("report", "summary"):
        schema = schema_for_catalog(phase, data)
        assert schema["additionalProperties"] is False
        assert schema["properties"]["observations"]["items"]["enum"] == [item["id"] for item in data["observations"]]
        assert schema["properties"]["concerns"]["maxItems"] == 0
        assert "enum" not in schema["properties"]["concerns"]["items"]
