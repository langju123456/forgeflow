"""Acceptance cases for semantic coverage, independent of structural validity."""

import copy

import pytest

from forgeflow.grounded_contracts import QUESTION, render, schema, semantic_check, validate_output


def catalog():
    return {
        "observations": [
            {"id": f"c_{n:016x}", "source_ref": "src_" + "a" * 32, "metric": metric,
             "value": value, "check_state": "SATISFIED", "evidence_state": "MEASURED",
             "authority": "HARDWARE_CHECK"}
            for n, (metric, value) in enumerate((
                ("boot_cycles", 100), ("boot_failures", 0), ("bom_revision", "A3"),
                ("ci_smoke", True), ("thermal_peak_c", 68)
            ))
        ],
        "concerns": [],
    }


def report(data):
    return {"status": "COMPLETE", "observations": [x["id"] for x in data["observations"]],
            "concerns": [], "recommendation": "HUMAN_REVIEW_REQUIRED"}


def test_pass_cannot_invent_unknown_from_generic_sop():
    data = catalog()
    output = report(data)
    output["concerns"] = ["c_" + "f" * 16]
    result = semantic_check(data, "report", output)
    assert result == {"structural_valid": True, "semantic_valid": False,
                      "codes": ["EVIDENCE_COVERAGE_CONCERNS"]}


@pytest.mark.parametrize("state", ["MISSING", "EXPIRED", "CONFLICT"])
def test_unknown_never_complete(state):
    data = catalog()
    data["observations"][3].update(check_state="UNKNOWN", evidence_state=state, value=None)
    output = report(data)
    assert semantic_check(data, "report", output)["codes"] == ["UNSUPPORTED_COMPLETION_STATUS"]
    output["status"] = "INCOMPLETE"
    assert validate_output(data, "report", output).status == "INCOMPLETE"
    assert "不能推进" in render(data, output)["summary"]


def test_duplicate_and_missing_measurement_rejected_separately_from_schema():
    data = catalog()
    output = report(data)
    output["observations"][-1] = output["observations"][0]
    assert semantic_check(data, "report", output)["codes"] == [
        "DUPLICATE_OBSERVATIONS", "EVIDENCE_COVERAGE_OBSERVATIONS"
    ]


def test_model_free_text_and_authority_fields_are_structurally_invalid():
    data = catalog()
    for key in ("summary", "approval_decision", "gate_status", "scope"):
        output = {**report(data), key: "PASS"}
        result = semantic_check(data, "report", output)
        assert result["structural_valid"] is False
        assert result["semantic_valid"] is None


def test_summary_cannot_drop_concern_or_report_reference():
    data = catalog()
    data["concerns"].append({"id": "c_" + "e" * 16, "source_ref": "src_" + "b" * 32,
                             "kind": "PROMPT_INJECTION"})
    output = {"status": "COMPLETE", "report_refs": ["report_pm"],
              "observations": report(data)["observations"], "concerns": [],
              "human_approval_required": True}
    assert semantic_check(data, "summary", output)["semantic_valid"] is False
    output["concerns"] = [data["concerns"][0]["id"]]
    assert semantic_check(data, "summary", output)["semantic_valid"] is True
    output["report_refs"] = ["another_report"]
    assert semantic_check(data, "summary", output)["structural_valid"] is False


def test_renderer_adds_no_model_prose_and_preserves_exact_source_values():
    data = catalog()
    before = copy.deepcopy(data)
    output = report(data)
    rendered = render(data, output)
    assert rendered["observations"] == before["observations"]
    assert rendered["concerns"] == []
    assert rendered["human_approval_required"] is True
    assert rendered["model_task"] == "CONSTRAINED_EVIDENCE_SELECTION"
    assert data == before


def test_plan_question_is_exact_and_fully_schema_bound():
    assert schema("plan")["properties"]["task_question"]["const"] == QUESTION
    output = {"assignment": "pm", "task_question": "Unknown evidence status",
              "source_refs": ["src_" + "a" * 32]}
    assert semantic_check(catalog(), "plan", output)["codes"] == ["UNGROUNDED_DELEGATED_QUESTION"]
