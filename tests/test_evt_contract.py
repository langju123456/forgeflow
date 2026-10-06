"""Pure composition tests: source acceptance cannot become build evidence."""

import copy
import json
import shutil

import pytest

from forgeflow.contracts import DomainError, Scope, digest
from forgeflow.evt_contract import (
    DEFAULT_POLICY,
    ROOT,
    SOURCE_POLICY,
    UNRESOLVED_POLICY,
    EVTPolicies,
    evaluate_evt,
)
from forgeflow.local import SCOPE


def inputs():
    scope = SCOPE.model_dump(mode="json")
    run = {**scope, "id": "synthetic-evt-run", "assessment_json": {"status": "PASS"}}
    domain = {"scope": scope, "run_id": run["id"],
              "architect": {"authority": "DETERMINISTIC_SOP", "static_status": "PASS"},
              "android": {"authority": "DETERMINISTIC_SOP", "static_status": "PASS",
                          "build_status": "NOT_RUN_NO_ANDROID_TOOLCHAIN"}}
    sre = {"scope": scope, "run_id": run["id"], "authority": "DETERMINISTIC_SOP", "operational_readiness": "READY"}
    return run, domain, sre


def evaluate(policy_id=DEFAULT_POLICY, *, data=None):
    run, domain, sre = inputs() if data is None else data
    return evaluate_evt(SCOPE, run, domain, sre, EVTPolicies().load(SCOPE, policy_id))


def checks(report):
    return {row["id"]: row for row in report["checks"]}


def test_default_requires_unavailable_apk_and_device_and_never_allows_actions():
    report = evaluate()
    assert report["status"] == "UNKNOWN"
    assert report["purpose"] == "EVT_READINESS_REVIEW" and len(report["checks"]) == 6
    for name in ("android_apk", "android_device"):
        assert checks(report)[name]["applicability"] == "APPLICABLE"
        assert checks(report)[name]["status"] == checks(report)[name]["observed_status"] == "UNKNOWN"
    assert report["stage_transition_allowed"] is False and report["full_evt_eligibility"] is False
    assert report["automatic_business_actions"] is False and report["human_approval_required"] is True


def test_explicit_source_review_exclusions_are_not_applicable_never_pass():
    report = evaluate(SOURCE_POLICY)
    assert report["status"] == "PASS"
    assert report["purpose"] == "SOURCE_REVIEW_ACCEPTANCE" and report["review_scope"] == "SOURCE_REVIEW_ONLY"
    for name in ("android_apk", "android_device"):
        row = checks(report)[name]
        assert row["applicability"] == row["status"] == "NOT_APPLICABLE"
        assert row["observed_status"] == "UNKNOWN" and row["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    assert report["full_evt_eligibility"] is False and report["stage_transition_allowed"] is False


def test_unresolved_applicability_remains_unknown_not_an_exclusion():
    report = evaluate(UNRESOLVED_POLICY)
    assert report["status"] == "UNKNOWN" and report["review_scope"] == "APPLICABILITY_UNRESOLVED"
    for name in ("android_apk", "android_device"):
        assert checks(report)[name]["applicability"] == checks(report)[name]["status"] == "UNKNOWN"


@pytest.mark.parametrize("failure", ["hardware", "architect", "android_source", "sre"])
def test_any_observed_failure_blocks_even_source_only_policy(failure):
    run, domain, sre = inputs()
    if failure == "hardware":
        run["assessment_json"]["status"] = "BLOCKED"
    elif failure == "architect":
        domain["architect"]["static_status"] = "FAIL"
    elif failure == "android_source":
        domain["android"]["static_status"] = "FAIL"
    else:
        sre["operational_readiness"] = "NEEDS_REVIEW"
    report = evaluate(SOURCE_POLICY, data=(run, domain, sre))
    assert report["status"] == "BLOCKED" and checks(report)[failure]["status"] == "FAIL"


def test_missing_unknown_or_unrecognized_core_evidence_cannot_pass():
    for value in (None, "UNKNOWN", "unrecognized"):
        run, domain, sre = inputs()
        run["assessment_json"]["status"] = value
        report = evaluate(SOURCE_POLICY, data=(run, domain, sre))
        assert report["status"] == checks(report)["hardware"]["status"] == "UNKNOWN"


def test_scope_and_report_binding_tampering_is_rejected():
    foreign = Scope(tenant_id="foreign", workspace_id=SCOPE.workspace_id, project_id=SCOPE.project_id)
    with pytest.raises(DomainError, match="NOT_FOUND"):
        EVTPolicies().load(foreign)
    with pytest.raises(DomainError, match="INVALID_EVT_POLICY"):
        EVTPolicies().load(SCOPE, "../source-review")
    for field in ("scope", "run_id", "authority"):
        run, domain, sre = inputs()
        sre[field] = {} if field == "scope" else "forged"
        with pytest.raises(DomainError, match="EVT_REPORT_BINDING_INVALID"):
            evaluate(SOURCE_POLICY, data=(run, domain, sre))


def test_entire_exact_registry_is_bound_and_policy_flags_cannot_be_overridden(tmp_path):
    root = tmp_path / "policies"
    shutil.copytree(ROOT / "fixtures/aurora-evt-review", root)
    loader = EVTPolicies(root)
    first = loader.load(SCOPE, SOURCE_POLICY)
    path = root / "policies.json"
    registry = json.loads(path.read_text())
    registry["policies"][0]["limitations"] += " Changed unrelated policy text."
    path.write_text(json.dumps(registry))
    changed = loader.load(SCOPE, SOURCE_POLICY)
    assert changed["policy_hash"] != first["policy_hash"] and changed["registry_hash"] != first["registry_hash"]
    forged = copy.deepcopy(first)
    forged["stage_transition_allowed"] = True
    with pytest.raises(DomainError, match="INVALID_EVT_POLICY"):
        evaluate_evt(SCOPE, *inputs(), forged)
    forged = copy.deepcopy(first)
    forged["policy_hash"] = "0" * 64
    with pytest.raises(DomainError, match="EVT_POLICY_BINDING_INVALID"):
        evaluate_evt(SCOPE, *inputs(), forged)


def test_injected_successful_build_claims_are_not_a_consumed_evidence_capability():
    run, domain, sre = inputs()
    domain["android"].update(build_status="SUCCEEDED", apk_status="PASS", device_status="PASS")
    report = evaluate(data=(run, domain, sre))
    assert report["status"] == "UNKNOWN"
    for name in ("android_apk", "android_device"):
        assert checks(report)[name]["status"] == "UNKNOWN"
        assert checks(report)[name]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"


def test_pure_composition_is_stable_and_preserves_input_reports():
    data = inputs()
    before = copy.deepcopy(data)
    first = evaluate(SOURCE_POLICY, data=data)
    assert data == before and first == evaluate(SOURCE_POLICY, data=data)
    assert digest(first) == digest(evaluate(SOURCE_POLICY, data=data))
