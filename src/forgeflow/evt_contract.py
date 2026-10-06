"""Scoped synthetic EVT composition, with no build or action authority."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .analysis_contracts import strict_json_object
from .contracts import DomainError, Scope, digest
from .local import SCOPE

ROOT = Path(__file__).resolve().parents[2]
REVISION = "synthetic-evt-composition-v1"
POLICY_REVISION = "synthetic-evt-policy-v1"
DEFAULT_POLICY = "synthetic-evt-apk-device-v1"
SOURCE_POLICY = "synthetic-source-review-v1"
UNRESOLVED_POLICY = "synthetic-applicability-unresolved-v1"
CHECK_IDS = ("hardware", "architect", "android_source", "android_apk", "android_device", "sre")
BUILD_STATUS = "NOT_RUN_NO_ANDROID_TOOLCHAIN"
POLICY_SHAPES = {
    DEFAULT_POLICY: ("EVT_READINESS_REVIEW", "FULL_EVT_EVIDENCE", "APPLICABLE"),
    SOURCE_POLICY: ("SOURCE_REVIEW_ACCEPTANCE", "SOURCE_REVIEW_ONLY", "NOT_APPLICABLE"),
    UNRESOLVED_POLICY: ("EVT_READINESS_REVIEW", "APPLICABILITY_UNRESOLVED", "UNKNOWN"),
}


def _scope(scope):
    if scope != SCOPE:
        raise DomainError("NOT_FOUND", status=404)


def _validate_policy(scope, policy):
    try:
        purpose, review_scope, build_applicability = POLICY_SHAPES[policy["id"]]
        expected = {name: build_applicability if name in {"android_apk", "android_device"}
                    else "APPLICABLE" for name in CHECK_IDS}
        valid = (policy["scope"] == scope.model_dump(mode="json") and policy["synthetic"] is True
                 and policy["version"] == POLICY_REVISION and policy["purpose"] == purpose
                 and policy["review_scope"] == review_scope and policy["stage_transition_allowed"] is False
                 and policy["applicability"] == expected)
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise DomainError("INVALID_EVT_POLICY", status=422)


def _policy_hash(policy, registry_hash):
    material = {key: value for key, value in policy.items() if key not in {"policy_hash", "registry_hash"}}
    return digest({"registry_hash": registry_hash, "selected_policy": material})


class EVTPolicies:
    """Only fixed policy IDs are accepted; filesystem configuration is trusted."""

    def __init__(self, fixture_root: Path | None = None):
        self.root = Path(fixture_root) if fixture_root is not None else ROOT / "fixtures/aurora-evt-review"

    def load(self, scope: Scope, policy_id: str = DEFAULT_POLICY):
        _scope(scope)
        if not isinstance(policy_id, str) or policy_id not in POLICY_SHAPES:
            raise DomainError("INVALID_EVT_POLICY", status=422)
        path = self.root / "policies.json"
        try:
            if path.is_symlink() or not path.is_file():
                raise DomainError("EVT_POLICY_UNAVAILABLE", status=404)
            with path.open("rb") as source:
                data = source.read(64 * 1024 + 1)
            if len(data) > 64 * 1024:
                raise DomainError("EVT_POLICY_TOO_LARGE", status=422)
            registry = strict_json_object(data.decode("utf-8"))
        except OSError as error:
            raise DomainError("EVT_POLICY_UNAVAILABLE", status=404) from error
        except (UnicodeDecodeError, DomainError) as error:
            if isinstance(error, DomainError) and error.code.startswith("EVT_POLICY_"):
                raise
            raise DomainError("INVALID_EVT_POLICY_REGISTRY", status=422) from error
        policies = registry.get("policies")
        if (registry.get("version") != POLICY_REVISION or registry.get("synthetic") is not True
                or registry.get("scope") != scope.model_dump(mode="json") or not isinstance(policies, list)
                or len(policies) != len(POLICY_SHAPES) or any(not isinstance(item, dict) for item in policies)
                or {item.get("id") for item in policies} != set(POLICY_SHAPES)):
            raise DomainError("INVALID_EVT_POLICY_REGISTRY", status=422)
        for policy in policies:
            _validate_policy(scope, policy)
        selected = next(policy for policy in policies if policy["id"] == policy_id)
        registry_hash = hashlib.sha256(data).hexdigest()
        return {**selected, "registry_hash": registry_hash, "policy_hash": _policy_hash(selected, registry_hash)}


def _reports_bound(scope, run, domain, sre):
    expected_scope, run_id = scope.model_dump(mode="json"), run.get("id")
    try:
        valid = (isinstance(run_id, str) and bool(run_id)
                 and all(run[key] == value for key, value in expected_scope.items())
                 and domain["scope"] == sre["scope"] == expected_scope
                 and domain["run_id"] == sre["run_id"] == run_id
                 and domain["architect"]["authority"] == domain["android"]["authority"]
                 == sre["authority"] == "DETERMINISTIC_SOP")
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise DomainError("EVT_REPORT_BINDING_INVALID", status=422)


def _observed(value, mapping):
    # Missing and unfamiliar statuses cannot increase readiness.
    return mapping.get(value, "UNKNOWN") if isinstance(value, str) else "UNKNOWN"


def evaluate_evt(scope: Scope, run: dict, domain_report: dict, sre_report: dict, policy: dict):
    """Compose six fixed rows; no caller build/device claims are consumed."""
    _scope(scope)
    _reports_bound(scope, run, domain_report, sre_report)
    _validate_policy(scope, policy)
    registry_hash = policy.get("registry_hash")
    if (not isinstance(registry_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", registry_hash)
            or policy.get("policy_hash") != _policy_hash(policy, registry_hash)):
        raise DomainError("EVT_POLICY_BINDING_INVALID", status=422)
    evidence = {
        "hardware": (_observed((run.get("assessment_json") or {}).get("status"),
                               {"PASS": "PASS", "BLOCKED": "FAIL", "UNKNOWN": "UNKNOWN"}),
                     "/parent/assessment_json/status"),
        "architect": (_observed(domain_report["architect"].get("static_status"),
                                {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}),
                      "/domain_report/architect/static_status"),
        "android_source": (_observed(domain_report["android"].get("static_status"),
                                     {"PASS": "PASS", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}),
                           "/domain_report/android/static_status"),
        "android_apk": ("UNKNOWN", "/domain_report/android/build_status"),
        "android_device": ("UNKNOWN", "/domain_report/android/build_status"),
        "sre": (_observed(sre_report.get("operational_readiness"),
                          {"READY": "PASS", "NEEDS_REVIEW": "FAIL", "UNKNOWN": "UNKNOWN"}),
                "/sre_report/operational_readiness"),
    }
    checks = []
    for name in CHECK_IDS:
        observed, pointer = evidence[name]
        applicability = policy["applicability"][name]
        status = observed if applicability == "APPLICABLE" else (
            "NOT_APPLICABLE" if applicability == "NOT_APPLICABLE" else "UNKNOWN")
        reason = ("EXCLUDED_BY_EXPLICIT_SYNTHETIC_SOURCE_REVIEW_POLICY" if status == "NOT_APPLICABLE" else
                  "APPLICABILITY_UNRESOLVED" if applicability == "UNKNOWN" else
                  BUILD_STATUS if name in {"android_apk", "android_device"} else
                  "RECORDED_CHECK_SATISFIED" if observed == "PASS" else
                  "RECORDED_CHECK_FAILED" if observed == "FAIL" else "REQUIRED_EVIDENCE_UNKNOWN")
        row = {"id": name, "applicability": applicability, "status": status, "reason": reason,
               "source_pointer": pointer, "observed_status": observed}
        if name in {"android_apk", "android_device"}:
            row["build_status"] = BUILD_STATUS
        checks.append(row)
    status = "BLOCKED" if any(row["status"] == "FAIL" for row in checks) else (
        "UNKNOWN" if any(row["status"] == "UNKNOWN" for row in checks) else "PASS")
    return {"revision": REVISION, "scope": scope.model_dump(mode="json"), "run_id": run["id"],
            "synthetic": True, "authority": "DETERMINISTIC_CONTRACT", "model_authority": "DETERMINISTIC_CONTRACT",
            "policy_id": policy["id"], "policy_hash": policy["policy_hash"], "registry_hash": registry_hash,
            "purpose": policy["purpose"], "review_scope": policy["review_scope"],
            "checks": checks, "status": status, "stage_transition_allowed": False,
            "full_evt_eligibility": False, "human_approval_required": True,
            "automatic_business_actions": False, "stage_transition": "NOT_EXECUTED"}
