"""Dataset integrity tests; no inference, transport, containers, or model quality scoring."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from checkfixtures import HERE, PROJECT, FixtureError, strict_json, validate_cases, validate_freeze


def dataset():
    return strict_json((HERE / "cases.json").read_text())


def case(data, identifier):
    return next(value for value in data["cases"] if value["id"] == identifier)


def test_frozen_dataset_contains_no_model_results():
    result = validate_freeze()
    assert result["case_count"] == 12 and result["status"] == "PASS"
    assert result["validation_kind"] == "DATASET_INTEGRITY_ONLY"
    assert result["model_evaluation"] == "NOT_RUN" and result["model_calls"] == 0


@pytest.mark.parametrize("mutation,expected", [
    (lambda data: data["cases"].pop(), "coverage incomplete"),
    (lambda data: case(data, "M05")["inputs"]["tool_grants"].append("sandbox.execute"), "Grants differ"),
    (lambda data: case(data, "M04")["inputs"]["trusted_identity"]["scope"].update(tenant_id="foreign"), "Identity/scope drift"),
    (lambda data: case(data, "M06")["gold"]["invariants"].remove("no_agent_approval"), "Safety invariant omitted"),
    (lambda data: case(data, "M04")["gold"]["allowed_templates"].append("generated-code"), "generated-code permission"),
    (lambda data: case(data, "M07")["inputs"]["test_fault"].update(never_append_to_prompt=False), "fault inserted"),
    (lambda data: case(data, "M11")["gold"].update(new_model_dispatches=1), "Exhausted budget dispatches"),
    (lambda data: case(data, "M12")["gold"].update(max_automatic_retries=1), "Unbounded retry"),
    (lambda data: case(data, "M12")["gold"].update(budget_reservation="RELEASED"), "blindly retries"),
    (lambda data: case(data, "M10")["gold"].update(cost=0), "treated as zero"),
    (lambda data: case(data, "M02")["gold"].update(gate_status_given_confirmed_fixture_check="PASS"), "Missing evidence gold wrong"),
    (lambda data: case(data, "M03")["gold"].update(gate_status_given_confirmed_fixture_check="PASS"), "Conflict/stale gold wrong"),
    (lambda data: case(data, "M04")["inputs"]["context"][1].update(content="changed"), "Context hash mismatch"),
    (lambda data: case(data, "M01")["gold"].update(delegated_roles=["pm", "sre"]), "unapproved role"),
    (lambda data: case(data, "M01")["model_result"].update(status="PASS", output="recorded answer"), "result mixed into inputs"),
    (lambda data: case(data, "M07")["gold"]["model_body_forbidden_fields"].remove("gate_status"), "authority fields"),
    (lambda data: case(data, "M01")["gold"].update(required_citation_ids=["foreign-source"]), "unavailable source"),
    (lambda data: case(data, "M05")["inputs"]["trusted_identity"].update(provider_url="http://untrusted"), "extra field"),
    (lambda data: case(data, "M03")["inputs"]["evidence_overlays"][0]["updates"].update(conflict=False), "context differs"),
    (lambda data: case(data, "M02")["inputs"]["trusted_identity"].update(role="hardware"), "Unapproved model role"),
    (lambda data: case(data, "M02")["gold"].update(required_source_version="invented"), "version mismatch"),
    (lambda data: case(data, "M02")["gold"].update(unknown_citations_allowed=True), "unavailable source"),
    (lambda data: case(data, "M10")["gold"].update(outcome="SUCCESS"), "Fault outcome gold wrong"),
    (lambda data: case(data, "M10")["gold"].update(budget_reservation="RELEASED"), "reservation released"),
    (lambda data: case(data, "M08")["gold"].update(usage_state="CONFIRMED"), "Fault usage gold wrong"),
    (lambda data: case(data, "M01")["variants"][0]["inputs"]["budget"].update(output_tokens_max=99999), "Budget/profile freeze"),
    (lambda data: case(data, "M01")["variants"][0]["inputs"]["trusted_identity"].update(role="hardware"), "Unapproved model role"),
    (lambda data: case(data, "M01")["variants"][0]["inputs"]["context"][0].update(source_hash="0" * 64), "Context hash mismatch"),
    (lambda data: case(data, "M01")["variants"][0]["gold"].update(required_citation_ids=["foreign-id"]), "unavailable source"),
    (lambda data: case(data, "M01")["variants"][0]["inputs"]["planned_phases"][1]["trusted_identity"].update(role="supervisor"), "wrong principal"),
    (lambda data: case(data, "M01")["variants"][0]["model_result"].update(status="PASS"), "claims model result"),
])
def test_safety_freeze_rejects_mutated_inputs(mutation, expected):
    data = deepcopy(dataset())
    # JSON loading does not share references, so changing the frozen scope in a case
    # cannot also change the dataset authority record.
    mutation(data)
    with pytest.raises(FixtureError, match=expected):
        validate_cases(data)


@pytest.mark.parametrize("path", [HERE / "cases.json", PROJECT / "src/forgeflow/policy.py",
                                 PROJECT / "fixtures/aurora-pass.json"])
def test_hash_freeze_detects_drift_without_writing_original(path, monkeypatch):
    original = Path.read_bytes

    def simulated_change(self):
        return original(self) + (b"\nchanged" if self == path else b"")

    monkeypatch.setattr(Path, "read_bytes", simulated_change)
    with pytest.raises(FixtureError, match="changed"):
        validate_freeze()


@pytest.mark.parametrize("text", ['{"id":1,"id":2}', '{"value":NaN}', '{"value":Infinity}'])
def test_strict_json_rejects_duplicate_or_nonfinite_values(text):
    with pytest.raises(FixtureError):
        strict_json(text)


def test_conflict_and_stale_are_visible_to_the_actual_model_context():
    selected = case(dataset(), "M03")
    visible = strict_json(selected["inputs"]["context"][0]["content"])
    assert visible["metrics"]["thermal_peak_c"]["conflict"] is True
    assert visible["metrics"]["ci_smoke"]["valid_until"] == "2026-10-04T12:00:00Z"
    assert visible["source_ref"] == selected["inputs"]["derived_source_ref"]
    assert selected["gold"]["required_citation_ids"] == [selected["inputs"]["context"][0]["source_id"]]


def test_blocked_variant_uses_fresh_proven_violation_and_its_own_citation():
    variant = case(dataset(), "M01")["variants"][0]
    visible = strict_json(variant["inputs"]["context"][0]["content"])
    assert visible["metrics"]["thermal_peak_c"]["value"] == 89
    assert variant["gold"]["gate_status_given_confirmed_fixture_check"] == "BLOCKED"
    assert variant["gold"]["required_citation_ids"] == ["c-aurora-blocked-v1"]
    assert variant["model_result"]["status"] == "NOT_RUN"
