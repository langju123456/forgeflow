"""Deterministic source reviews of scoped synthetic hardware and Android XML.

The fixed XML is a separate artifact, not an output of the recorded hardware
execution. Source conformance never constitutes Android build/device evidence.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import contracts
from .analysis_contracts import strict_json_object
from .contracts import DomainError, Scope, digest
from .local import SCOPE
from .sre_ops import evaluate_operations, source_binding

REVISION = "domain-source-review-v1"
ARTIFACT_REVISION = "domain-artifacts-v1"
ROOT = Path(__file__).resolve().parents[2]
XML_LIMIT = 64 * 1024
ANDROID = "{http://schemas.android.com/apk/res/android}"
FIXTURES = {name: name + ".xml" for name in ("aligned", "bom-mismatch", "missing-exported")}
FixtureId = Literal["aligned", "bom-mismatch", "missing-exported"]
ROLE_TOOLS = {
    "architect": frozenset({"execution.receipts.read", "execution.events.read", "domain.artifacts.read"}),
    "android": frozenset({"domain.artifacts.read"}),
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scope(scope: Scope) -> None:
    if scope != SCOPE:
        raise DomainError("NOT_FOUND", status=404)


def _artifact_binding(scope, fixture_id, contract_hash, xml_hash, sources):
    return digest({"scope": scope.model_dump(mode="json"), "fixture_id": fixture_id,
                   "contract_hash": contract_hash, "static_xml_hash": xml_hash, "sources": sources})


class DomainArtifacts:
    """Root configuration is trusted; request inputs can select only three IDs."""

    def __init__(self, fixture_root: Path | None = None):
        self.root = Path(fixture_root) if fixture_root is not None else ROOT / "fixtures/aurora-domain"

    def _read(self, filename):
        path = self.root / filename
        try:
            if path.is_symlink() or not path.is_file():
                raise DomainError("DOMAIN_ARTIFACT_UNAVAILABLE", status=404)
            with path.open("rb") as source:
                data = source.read(XML_LIMIT + 1)
        except OSError as error:
            raise DomainError("DOMAIN_ARTIFACT_UNAVAILABLE", status=404) from error
        if len(data) > XML_LIMIT:
            raise DomainError("DOMAIN_ARTIFACT_TOO_LARGE", status=422)
        return data

    def load(self, scope: Scope, fixture_id: FixtureId):
        _scope(scope)
        if not isinstance(fixture_id, str) or fixture_id not in FIXTURES:
            raise DomainError("INVALID_DOMAIN_FIXTURE", status=422)
        contract_bytes, xml_bytes = self._read("contract.json"), self._read(FIXTURES[fixture_id])
        try:
            contract = strict_json_object(contract_bytes.decode("utf-8"))
            xml = xml_bytes.decode("utf-8")
        except (UnicodeDecodeError, DomainError) as error:
            raise DomainError("INVALID_DOMAIN_ARTIFACT", status=422) from error
        if (contract.get("scope") != scope.model_dump(mode="json") or contract.get("synthetic") is not True
                or contract.get("id") != "aurora-domain-contract" or contract.get("version") != ARTIFACT_REVISION):
            raise DomainError("INVALID_DOMAIN_CONTRACT", status=422)
        contract_hash, xml_hash = _sha(contract_bytes), _sha(xml_bytes)
        sources = [{"id": contract["id"], "version": ARTIFACT_REVISION,
                    "sha256": contract_hash, "synthetic": True},
                   {"id": "aurora-domain-" + fixture_id, "version": ARTIFACT_REVISION,
                    "sha256": xml_hash, "synthetic": True}]
        return {"fixture_id": fixture_id, "contract": contract, "manifest_xml": xml, "sources": sources,
                "contract_hash": contract_hash, "static_xml_hash": xml_hash,
                "artifact_hash": _artifact_binding(scope, fixture_id, contract_hash, xml_hash, sources)}


def _parse_xml(xml):
    if (not isinstance(xml, str) or len(xml.encode()) > XML_LIMIT
            or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE)):
        raise ValueError("DTD, entity declarations or unbounded XML")
    root = ET.fromstring(xml)
    apps, sdks = root.findall("application"), root.findall("uses-sdk")
    if root.tag != "manifest" or len(apps) != 1 or len(sdks) != 1:
        raise ValueError("Exactly one application and uses-sdk required")
    app, sdk = apps[0], sdks[0]
    metadata, components, identities = {}, [], set()
    for item in app.findall("meta-data"):
        name = item.get(ANDROID + "name")
        if not name or name in metadata:
            raise ValueError("Duplicate or unnamed metadata")
        metadata[name] = item.get(ANDROID + "value")
    for item in app:
        if item.tag not in {"activity", "activity-alias", "service", "receiver", "provider"}:
            continue
        name = item.get(ANDROID + "name")
        if not name or (item.tag, name) in identities:
            raise ValueError("Duplicate or unnamed component")
        identities.add((item.tag, name))
        filters = item.findall("intent-filter")
        launcher = any(
            any(action.get(ANDROID + "name") == "android.intent.action.MAIN"
                for action in row.findall("action"))
            and any(category.get(ANDROID + "name") == "android.intent.category.LAUNCHER"
                    for category in row.findall("category")) for row in filters)
        components.append({"type": item.tag, "name": name, "exported": item.get(ANDROID + "exported"),
                           "has_filter": bool(filters), "launcher": launcher})
    def integer(name):
        value = sdk.get(ANDROID + name)
        return int(value) if value is not None and re.fullmatch(r"[0-9]+", value) else None
    return {"package": root.get("package"), "min_sdk": integer("minSdkVersion"),
            "target_sdk": integer("targetSdkVersion"), "debuggable": app.get(ANDROID + "debuggable"),
            "metadata": metadata, "components": components}


def _parent_manifest(scope, run, events):
    artifacts = [row for row in run.get("artifacts", []) if row.get("path") == "result.json"]
    if not artifacts:
        return None, "UNKNOWN", "PARENT_RESULT_ARTIFACT_MISSING"
    if len(artifacts) != 1:
        return None, "FAIL", "PARENT_RESULT_ARTIFACT_AMBIGUOUS"
    row, snapshot = artifacts[0], run.get("snapshot_json") or {}
    try:
        content = row["content"].encode()
        envelope = strict_json_object(row["content"])
        manifest = envelope["manifest"]
        provenance = row["provenance"]
        operations = evaluate_operations(scope, run, events, "domain-parent-binding")
        valid = (len(content) <= XML_LIMIT and type(row["size"]) is int and row["size"] == len(content)
                 and row["content_sha256"] == _sha(content) and row["run_id"] == run["id"]
                 and all(row[key] == value for key, value in scope.model_dump(mode="json").items())
                 and envelope.get("schema_version") == "evt-check-v1"
                 and envelope.get("source") == "SYNTHETIC_FIXTURE" and manifest.get("synthetic") is True
                 and manifest.get("version") == "v1" and isinstance(manifest.get("source_ref"), str)
                 and digest(manifest) == provenance["input_hash"] == snapshot["input_hash"]
                 == snapshot["input_manifest_hash"]
                 and all(check.status == "PASS" for check in operations.checks[:2]))
        return (manifest, "PASS", "BOUND_PARENT_ARTIFACT") if valid else (
            None, "FAIL", "PARENT_ARTIFACT_BINDING_INVALID")
    except (DomainError, KeyError, TypeError, ValueError, AttributeError):
        return None, "FAIL", "PARENT_ARTIFACT_BINDING_INVALID"


def _bom_evidence(run, manifest):
    item = (manifest.get("metrics") or {}).get("bom_revision") if manifest else None
    if not isinstance(item, dict):
        return None, "HARDWARE_BOM_MISSING_OR_UNBOUND"
    try:
        result, snapshot = run.get("result_json") or {}, run.get("snapshot_json") or {}
        now = contracts.utcnow()
        # Only a bound synthetic fixture can carry this trusted server snapshot clock.
        if manifest.get("synthetic") is True and result.get("synthetic") is True and snapshot.get("evaluation_clock"):
            now = datetime.fromisoformat(snapshot["evaluation_clock"].replace("Z", "+00:00"))
        observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
        valid = datetime.fromisoformat(item["valid_until"].replace("Z", "+00:00"))
        if now.tzinfo is None or observed.tzinfo is None or valid.tzinfo is None:
            return None, "HARDWARE_BOM_INVALID_TIME"
        if observed > now or valid <= now or valid <= observed:
            return None, "HARDWARE_BOM_EXPIRED_OR_FUTURE"
        if item.get("conflict") or item.get("status") != "MEASURED" or type(item.get("value")) is not str:
            return None, "HARDWARE_BOM_CONFLICTED_OR_UNMEASURED"
        if result.get("status") != "SUCCEEDED" or result.get("cleanup") != "REMOVED":
            return None, "HARDWARE_EXECUTION_UNCONFIRMED"
        return item["value"], "MEASURED_BOUND_HARDWARE_BOM"
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, "HARDWARE_BOM_MALFORMED"


def _status(checks):
    return "FAIL" if any(row["status"] == "FAIL" for row in checks) else (
        "UNKNOWN" if any(row["status"] == "UNKNOWN" for row in checks) else "PASS")


def evaluate_domain(scope: Scope, run: dict, events: list, bundle: dict, skill_hashes: dict):
    """Always evaluate both roles; caller cannot supply a role selection or verdict."""
    _scope(scope)
    if any(run.get(key) != value for key, value in scope.model_dump(mode="json").items()):
        raise DomainError("NOT_FOUND", status=404)
    if set(skill_hashes) != {"architect", "android"}:
        raise DomainError("DOMAIN_SKILLS_REQUIRED", status=422)
    contract = bundle["contract"]
    if contract.get("scope") != scope.model_dump(mode="json") or contract.get("synthetic") is not True:
        raise DomainError("INVALID_DOMAIN_CONTRACT", status=422)
    xml_pointer = "/domain-sources/aurora-domain-" + bundle["fixture_id"] + "/manifest"
    contract_pointer = "/domain-sources/aurora-domain-contract"
    architect, android = [], []
    def add(rows, name, status, reason, pointer):
        rows.append({"id": name, "status": status, "reason": reason, "source_pointer": pointer})
    manifest, binding, binding_reason = _parent_manifest(scope, run, events)
    add(architect, "parent_artifact_binding", binding, binding_reason, "/artifacts/result.json")
    try:
        parsed = _parse_xml(bundle["manifest_xml"])
        add(android, "manifest_parse", "PASS", "BOUNDED_XML_PARSED", xml_pointer)
    except (ET.ParseError, ValueError, TypeError):
        parsed = None
        add(android, "manifest_parse", "FAIL", "INVALID_ANDROID_XML", xml_pointer)
    expected, metadata = contract["android"], contract["metadata"]
    if parsed is None:
        for name in ("hardware_android_bom", "interface_contract", "exposure_contract"):
            add(architect, name, "UNKNOWN", "ANDROID_XML_UNAVAILABLE", xml_pointer)
        for name in ("package_sdk", "application_contract"):
            add(android, name, "UNKNOWN", "ANDROID_XML_UNAVAILABLE", xml_pointer)
    else:
        hardware_bom, bom_reason = _bom_evidence(run, manifest)
        xml_bom = parsed["metadata"].get(metadata["bom_name"])
        bom_status = "UNKNOWN" if hardware_bom is None else (
            "PASS" if hardware_bom == xml_bom == contract["hardware"]["bom_revision"] else "FAIL")
        add(architect, "hardware_android_bom", bom_status,
            bom_reason if hardware_bom is None else "BOM_ALIGNED" if bom_status == "PASS" else "BOM_CONFLICT",
            "/artifacts/result.json/manifest/metrics/bom_revision -> " + xml_pointer +
            "/application/meta-data[@android:name='" + metadata["bom_name"] + "'] -> " + contract_pointer)
        identity = (parsed["package"] == expected["package"]
                    and parsed["min_sdk"] == expected["min_sdk"]
                    and parsed["target_sdk"] == expected["target_sdk"]
                    and type(expected["min_sdk"]) is int and type(expected["target_sdk"]) is int
                    and 1 <= parsed["min_sdk"] <= parsed["target_sdk"])
        launcher = [row for row in parsed["components"]
                    if row["type"] == "activity" and row["name"] == expected["launcher_activity"]]
        primary = launcher[0] if len(launcher) == 1 else None
        protocol = parsed["metadata"].get(metadata["protocol_name"]) == metadata["protocol_version"]
        interface = identity and protocol and len(parsed["components"]) == 1 and primary and primary["launcher"]
        exported_required_missing = (parsed["target_sdk"] is not None and parsed["target_sdk"] >= 31
                                     and any(row["type"] in {"activity", "service", "receiver"}
                                             and row["has_filter"] and row["exported"] is None
                                             for row in parsed["components"]))
        exposure = (primary and primary["exported"] == str(expected["launcher_exported"]).lower()
                    and parsed["debuggable"] == str(expected["debuggable"]).lower()
                    and not exported_required_missing)
        add(architect, "interface_contract", "PASS" if interface else "FAIL",
            "INTERFACE_ALIGNED" if interface else "INTERFACE_CONFLICT", xml_pointer + " -> " + contract_pointer)
        exposure_reason = ("ANDROID_12_EXPORTED_REQUIRED" if exported_required_missing else
                           "EXPOSURE_ALIGNED" if exposure else "EXPOSURE_CONFLICT")
        add(architect, "exposure_contract", "PASS" if exposure else "FAIL", exposure_reason,
            xml_pointer + "/application/activity/@android:exported -> " + contract_pointer)
        add(android, "package_sdk", "PASS" if identity else "FAIL",
            "PACKAGE_SDK_ALIGNED" if identity else "PACKAGE_SDK_CONFLICT", xml_pointer + "/uses-sdk")
        application = interface and exposure and xml_bom == contract["hardware"]["bom_revision"]
        add(android, "application_contract", "PASS" if application else "FAIL",
            "APPLICATION_ALIGNED" if application else
            "ANDROID_12_EXPORTED_REQUIRED" if exported_required_missing else "APPLICATION_CONFLICT",
            xml_pointer + "/application -> " + contract_pointer)
    add(android, "build_device", "UNKNOWN", "NOT_RUN_NO_ANDROID_TOOLCHAIN", "/android/build-device-receipts")
    roles = {}
    for role, checks in (("architect", architect), ("android", android)):
        static_status = _status([row for row in checks if row["id"] != "build_device"])
        readiness = "BLOCKED" if static_status == "FAIL" else (
            "READY" if role == "architect" and static_status == "PASS" else "UNKNOWN")
        roles[role] = {"role": role, "authority": "DETERMINISTIC_SOP", "skill_hash": skill_hashes[role],
                       "checks": checks, "static_status": static_status, "readiness": readiness}
    roles["android"]["build_status"] = "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    return {"revision": REVISION, "scope": scope.model_dump(mode="json"), "run_id": run["id"],
            "parent_source_hash": source_binding(run, events), "artifact_hash": bundle["artifact_hash"],
            "sources": bundle["sources"], **roles,
            "supervisor_handoff": "DOMAIN_CONFLICT" if any(row["status"] == "FAIL"
                for row in architect + android) else "NEEDS_ANDROID_BUILD_EVIDENCE",
            "human_approval_required": True, "business_actions_executed_by_sop": False,
            "stage_transition": "NOT_EXECUTED"}
