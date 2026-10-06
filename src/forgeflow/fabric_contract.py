"""Stable evidence for an isolated local test flow, without EVT eligibility.

The XML files are public synthetic templates. Their old project contract is
never relabelled: this module owns a separate fixed, scoped contract. Approval
and action state do not belong to the technical evidence projection.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

from .analysis_contracts import strict_json_object
from .contracts import DomainError, Scope, digest
from .domain_checks import ROLE_TOOLS as DOMAIN_TOOLS
from .domain_checks import _bom_evidence, _parent_manifest, _parse_xml
from .evt_review import technical_source
from .gate import RULE_HASH
from .policy import POLICY_HASH as EXECUTION_POLICY_HASH
from .skills import SkillRegistry
from .sre_ops import ROLE_TOOLS as SRE_TOOLS
from .sre_ops import evaluate_operations, event_chain_valid
from .templates import template_hash

ROOT = Path(__file__).resolve().parents[2]
REVISION = "fabric-stable-technical-v1"
FABRIC_SCOPE = Scope(tenant_id="synthetic-lab", workspace_id="engineering", project_id="aurora-fabric-e2e")
FIXTURES = {name: name + ".xml" for name in ("aligned", "bom-mismatch", "missing-exported")}
POLICY_ID = "synthetic-local-fabric-v1"
POLICY_VERSION = "fabric-policy-v1"
POLICY_PATH = ROOT / "fixtures/fabric/policy.json"
FILE_LIMIT = 256 * 1024


def _read(path: Path, limit: int = FILE_LIMIT) -> bytes:
    """Fixed files from trusted constructor configuration; no request paths."""
    try:
        if path.is_symlink():
            raise DomainError("FABRIC_SOURCE_UNAVAILABLE", status=404)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise DomainError("FABRIC_SOURCE_INVALID", status=422)
            data = source.read(limit + 1)
        if len(data) > limit:
            raise DomainError("FABRIC_SOURCE_INVALID", status=422)
        return data
    except OSError as error:
        raise DomainError("FABRIC_SOURCE_UNAVAILABLE", status=404) from error


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _policy(data: bytes) -> dict:
    try:
        policy = strict_json_object(data.decode("utf-8"))
        contract = policy["contract"]
        valid = (
            policy["id"] == POLICY_ID and policy["version"] == POLICY_VERSION
            and policy["scope"] == contract["scope"] == FABRIC_SCOPE.model_dump(mode="json")
            and policy["synthetic"] is True and policy["purpose"] == "LOCAL_SYNTHETIC_FLOW_VALIDATION"
            and policy["source_template_authority"] == "PUBLIC_SYNTHETIC_TEMPLATE"
            and policy["full_evt_eligibility"] is False and policy["evt_stage_transition_allowed"] is False
            and policy["human_approval_required"] is True and policy["automatic_business_actions"] is False
            and digest(policy["test_stage"]) == digest({
                "from_stage": "PROTOTYPE", "to_stage": "TEST_STAGE", "simulation": True})
            and policy["action_kinds"] == ["TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE"]
            and digest(policy["compensations"]) == digest({
                kind: {"original_kind": original, "receipt_bound": True, "current_revision_cas": True}
                for kind, original in (("STAGE_REVERT", "TEST_STAGE"), ("ISSUE_RETRACT", "ISSUE"),
                                       ("MEMORY_REVOKE", "MEMORY_ACTIVATE"))
            })
            and contract["hardware"] == {"bom_revision": "A3"}
            and digest(contract["android"]) == digest({
                "package": "io.forgeflow.aurora", "min_sdk": 26, "target_sdk": 34,
                "launcher_activity": ".MainActivity", "launcher_exported": True, "debuggable": False})
            and contract["metadata"] == {"bom_name": "aurora.bom.revision",
                                         "protocol_name": "aurora.protocol.version", "protocol_version": "1"}
        )
    except (UnicodeDecodeError, KeyError, TypeError, DomainError) as error:
        raise DomainError("INVALID_FABRIC_POLICY", status=422) from error
    if not valid:
        raise DomainError("INVALID_FABRIC_POLICY", status=422)
    return policy


_POLICY_BYTES = _read(POLICY_PATH, 64 * 1024)
POLICY = _policy(_POLICY_BYTES)
POLICY_HASH = _sha(_POLICY_BYTES)


class FabricSources:
    """Constructor roots are trusted deployment configuration, never body data."""

    def __init__(self, *, policy_root=None, xml_root=None, skill_root=None):
        self.policy_root = Path(policy_root) if policy_root is not None else ROOT / "fixtures/fabric"
        self.xml_root = Path(xml_root) if xml_root is not None else ROOT / "fixtures/aurora-domain"
        self.skill_root = Path(skill_root) if skill_root is not None else ROOT / "skills"
        for root in (self.policy_root, self.xml_root, self.skill_root):
            if not root.is_absolute() or root.is_symlink() or not root.is_dir():
                raise DomainError("INVALID_FABRIC_SOURCE_CONFIG", status=422)

    def load(self, fixture_id="aligned"):
        if not isinstance(fixture_id, str) or fixture_id not in FIXTURES:
            raise DomainError("INVALID_FABRIC_FIXTURE", status=422)
        policy_bytes = _read(self.policy_root / "policy.json", 64 * 1024)
        policy = _policy(policy_bytes)
        xml_bytes = _read(self.xml_root / FIXTURES[fixture_id], 64 * 1024)
        try:
            xml = xml_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DomainError("FABRIC_SOURCE_INVALID", status=422) from error
        hardware = SkillRegistry(self.skill_root).load("hardware-evt", "hardware")
        domains = SkillRegistry(self.skill_root, role_tools=DOMAIN_TOOLS)
        domain_skills = {role: domains.load(name, role) for role, name in (
            ("architect", "architect-contract"), ("android", "android-manifest"))}
        sre = SkillRegistry(self.skill_root, role_tools=SRE_TOOLS).load("sre-operations", "sre")
        if (set(hardware.allowed_tools) != {"knowledge.search", "memory.search", "sandbox.execute"}
                or set(sre.allowed_tools) != SRE_TOOLS["sre"]
                or any(set(skill.allowed_tools) != DOMAIN_TOOLS[role] for role, skill in domain_skills.items())):
            raise DomainError("FABRIC_SKILL_CAPABILITY_MISSING", status=403)
        implementations = {name: _sha(_read(ROOT / "src/forgeflow" / name)) for name in (
            "fabric_contract.py", "evt_review.py", "sre_ops.py", "domain_checks.py", "analysis_context.py",
            "gate.py", "policy.py", "templates.py")}
        return {"policy": policy, "policy_hash": _sha(policy_bytes), "xml": xml, "xml_hash": _sha(xml_bytes),
                "code_hash": template_hash("evt-check"), "runtime_policy_hash": EXECUTION_POLICY_HASH,
                "rule_hash": RULE_HASH, "hardware_skill_hash": hardware.sha256,
                "domain_skill_hashes": {role: skill.sha256 for role, skill in domain_skills.items()},
                "sre_skill_hash": sre.sha256, "implementation_hashes": implementations}


def load_sources(fixture_id="aligned", *, source_config=None):
    if source_config is not None and not isinstance(source_config, FabricSources):
        raise DomainError("INVALID_FABRIC_SOURCE_CONFIG", status=422)
    return (source_config or FabricSources()).load(fixture_id)


def _projection(run, events):
    completed = [event for event in events if event.get("kind") == "CHECK_COMPLETED"]
    completion = completed[-1] if completed else None
    step_id = completion.get("step_id") if completion else None
    projection = copy.deepcopy(run)
    projection.update(revision=0, state="CHECK_COMPLETED", approvals=[], receipts=[], actions=[],
                      project=FABRIC_SCOPE.model_dump(mode="json"))
    projection["steps"] = sorted([copy.deepcopy(row) for row in run.get("steps", [])
                                  if row.get("id") == step_id], key=lambda row: row["id"])
    projection["artifacts"] = sorted([copy.deepcopy(row) for row in run.get("artifacts", [])
                                      if row.get("step_id") == step_id], key=lambda row: (row["path"], row["id"]))
    for key in ("snapshot_json", "snapshot_hash", "result_json", "assessment_json"):
        projection.setdefault(key, None)
    projection.setdefault("cleanup_required", False)
    prefix = [event for event in events if completion and event["sequence"] <= completion["sequence"]]
    return projection, prefix


def _status(checks):
    return "BLOCKED" if any(row["status"] == "FAIL" for row in checks) else (
        "UNKNOWN" if any(row["status"] == "UNKNOWN" for row in checks) else "PASS")


def _static_checks(scope, run, events, sources):
    contract, xml = sources["policy"]["contract"], sources["xml"]
    architect, android = [], []

    def add(rows, name, status, reason, pointer):
        rows.append({"id": name, "status": status, "reason": reason, "source_pointer": pointer})

    manifest, binding, reason = _parent_manifest(scope, run, events)
    add(architect, "parent_artifact_binding", binding, reason, "/artifacts/result.json")
    try:
        parsed = _parse_xml(xml)
        add(android, "manifest_parse", "PASS", "BOUNDED_PUBLIC_SYNTHETIC_XML", "/templates/manifest")
    except (ET.ParseError, ValueError, TypeError):
        parsed = None
        add(android, "manifest_parse", "FAIL", "INVALID_ANDROID_XML", "/templates/manifest")
    if parsed is None:
        for name in ("hardware_android_bom", "interface_contract", "exposure_contract"):
            add(architect, name, "UNKNOWN", "ANDROID_XML_UNAVAILABLE", "/templates/manifest")
        for name in ("package_sdk", "application_contract"):
            add(android, name, "UNKNOWN", "ANDROID_XML_UNAVAILABLE", "/templates/manifest")
    else:
        expected, metadata = contract["android"], contract["metadata"]
        hardware_bom, bom_reason = _bom_evidence(run, manifest)
        xml_bom = parsed["metadata"].get(metadata["bom_name"])
        bom = "UNKNOWN" if hardware_bom is None else (
            "PASS" if hardware_bom == xml_bom == contract["hardware"]["bom_revision"] else "FAIL")
        add(architect, "hardware_android_bom", bom, bom_reason if hardware_bom is None else
            "BOM_ALIGNED" if bom == "PASS" else "BOM_CONFLICT", "/artifacts/result.json -> /templates/manifest")
        identity = (parsed["package"] == expected["package"] and parsed["min_sdk"] == expected["min_sdk"]
                    and parsed["target_sdk"] == expected["target_sdk"])
        launchers = [row for row in parsed["components"] if row["type"] == "activity"
                     and row["name"] == expected["launcher_activity"]]
        primary = launchers[0] if len(launchers) == 1 else None
        interface = bool(identity and len(parsed["components"]) == 1 and primary and primary["launcher"]
                         and parsed["metadata"].get(metadata["protocol_name"]) == metadata["protocol_version"])
        missing_exported = (parsed["target_sdk"] is not None and parsed["target_sdk"] >= 31
                            and any(row["type"] in {"activity", "service", "receiver"} and row["has_filter"]
                                    and row["exported"] is None for row in parsed["components"]))
        exposure = bool(primary and primary["exported"] == str(expected["launcher_exported"]).lower()
                        and parsed["debuggable"] == str(expected["debuggable"]).lower() and not missing_exported)
        add(architect, "interface_contract", "PASS" if interface else "FAIL",
            "INTERFACE_ALIGNED" if interface else "INTERFACE_CONFLICT", "/templates/manifest -> /policy/contract")
        add(architect, "exposure_contract", "PASS" if exposure else "FAIL",
            "ANDROID_12_EXPORTED_REQUIRED" if missing_exported else
            "EXPOSURE_ALIGNED" if exposure else "EXPOSURE_CONFLICT", "/templates/manifest/application")
        add(android, "package_sdk", "PASS" if identity else "FAIL",
            "PACKAGE_SDK_ALIGNED" if identity else "PACKAGE_SDK_CONFLICT", "/templates/manifest/uses-sdk")
        application = interface and exposure and xml_bom == contract["hardware"]["bom_revision"]
        add(android, "application_contract", "PASS" if application else "FAIL",
            "ANDROID_12_EXPORTED_REQUIRED" if missing_exported else
            "APPLICATION_ALIGNED" if application else "APPLICATION_CONFLICT", "/templates/manifest/application")
    return architect, android


def evaluate_fabric(scope, run, events, fixture_id="aligned", *, source_config=None):
    """Compose bound observations; this function executes no business actions."""
    expected_scope = FABRIC_SCOPE.model_dump(mode="json")
    if scope != FABRIC_SCOPE or any(run.get(key) != value for key, value in expected_scope.items()):
        raise DomainError("NOT_FOUND", status=404)
    if not isinstance(run.get("id"), str) or not run["id"]:
        raise DomainError("FABRIC_RUN_BINDING_INVALID", status=422)
    sources = load_sources(fixture_id, source_config=source_config)
    projection, prefix = _projection(run, events)
    snapshot = projection["snapshot_json"] or {}
    operational = evaluate_operations(scope, projection, prefix, sources["sre_skill_hash"])
    architect, android = _static_checks(scope, projection, prefix, sources)
    checks = []

    def add(name, status, reason, pointer):
        checks.append({"id": name, "status": status, "reason": reason, "source_pointer": pointer})

    core = (projection["assessment_json"] or {}).get("status")
    add("hardware", {"PASS": "PASS", "BLOCKED": "FAIL"}.get(core, "UNKNOWN"),
        "RECORDED_SYNTHETIC_HARDWARE_GATE", "/assessment_json/status")
    current = {"policy_hash": sources["runtime_policy_hash"], "rule_hash": sources["rule_hash"],
               "skill_hash": sources["hardware_skill_hash"], "code_hash": sources["code_hash"]}
    version_status = "UNKNOWN" if any(key not in snapshot for key in current) else (
        "PASS" if all(snapshot[key] == value for key, value in current.items()) else "FAIL")
    add("current_execution_sources", version_status, "CURRENT_CODE_RULE_POLICY_HARDWARE_SKILL", "/snapshot_json")
    request = run.get("request_json")
    request_status = "UNKNOWN" if request is None or not run.get("request_hash") else (
        "PASS" if isinstance(request, dict) and run["request_hash"] == digest(request)
        and request.get("scope") == expected_scope else "FAIL")
    add("request_binding", request_status, "EXACT_SCOPED_REQUEST", "/request_json")
    try:
        audit_valid = (all(all(row.get(key) == value for key, value in expected_scope.items())
                           and row.get("run_id") == run["id"] for row in events)
                       and event_chain_valid(scope, run["id"], events))
    except (KeyError, TypeError, ValueError):
        audit_valid = False
    add("current_audit_chain", "PASS" if audit_valid else "FAIL" if events else "UNKNOWN",
        "FULL_CURRENT_AUDIT_CHAIN", "/events")
    checks.extend({**row, "id": "sre." + row["id"]}
                  for row in (check.model_dump(mode="json") for check in operational.checks))
    checks.extend({**row, "id": role + "." + row["id"]}
                  for role, rows in (("architect", architect), ("android", android)) for row in rows)
    flow_gate = _status(checks)
    source_hashes = {key: value for key, value in sources.items() if key not in {"policy", "xml"}}
    binding = {"namespace": REVISION, "scope": expected_scope, "run_id": run["id"],
               "request_hash": run.get("request_hash"), "technical_source": technical_source(projection, prefix),
               "fixture_id": fixture_id, "current_sources": source_hashes}
    return {"revision": REVISION, "scope": expected_scope, "run_id": run["id"], "synthetic": True,
            "authority": "DETERMINISTIC_LOCAL_FLOW_CONTRACT", "policy_id": sources["policy"]["id"],
            "policy_hash": sources["policy_hash"], "purpose": sources["policy"]["purpose"],
            "technical_hash": digest(binding), "technical_binding": binding, "sources": source_hashes,
            "source_template_authority": "PUBLIC_SYNTHETIC_TEMPLATE", "fixture_id": fixture_id,
            "checks": checks, "flow_gate": flow_gate, "evt_gate": "BLOCKED" if flow_gate == "BLOCKED" else "UNKNOWN",
            "local_test_stage_allowed": flow_gate == "PASS", "test_stage": copy.deepcopy(sources["policy"]["test_stage"]),
            "evt_stage_transition_allowed": False, "full_evt_eligibility": False,
            "android_apk_status": "UNKNOWN", "android_device_status": "UNKNOWN",
            "android_build": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "android_device": "NOT_RUN_NO_ANDROID_DEVICE",
            "architect": {"checks": architect, "static_status": _status(architect)},
            "android": {"checks": android, "static_status": _status(android),
                        "build_status": "NOT_RUN_NO_ANDROID_TOOLCHAIN", "device_status": "NOT_RUN_NO_ANDROID_DEVICE"},
            "sre": {"operational_readiness": operational.operational_readiness,
                    "checks": [check.model_dump(mode="json") for check in operational.checks]},
            "audit_chain_valid": audit_valid, "human_approval_required": True, "automatic_business_actions": False,
            "action_kinds": list(sources["policy"]["action_kinds"]),
            "compensations": copy.deepcopy(sources["policy"]["compensations"]), "stage_transition": "NOT_EXECUTED"}
