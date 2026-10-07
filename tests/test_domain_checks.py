"""Offline parent receipts plus actual controlled XML, without Android tooling."""

import copy
import hashlib
import json
import shutil
from datetime import UTC, datetime

import pytest

from forgeflow.contracts import DomainError, Scope, digest
from forgeflow.domain_checks import ROLE_TOOLS, ROOT, DomainArtifacts, evaluate_domain
from forgeflow.local import SCOPE
from forgeflow.skills import SkillRegistry

ARCHIVE = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01/raw-chains/M01.json"
SKILLS = {"architect": "offline-architect-skill", "android": "offline-android-skill"}


def parent():
    data = json.loads(ARCHIVE.read_text())
    return data["parent_after"], data["parent_events"]


def evaluate(fixture="aligned", *, run=None, events=None, loader=None):
    if run is None:
        run, events = parent()
    return evaluate_domain(SCOPE, run, events, (loader or DomainArtifacts()).load(SCOPE, fixture), SKILLS)


def rebind_manifest(run, events, change):
    row = run["artifacts"][0]
    envelope = json.loads(row["content"])
    change(envelope["manifest"])
    manifest_hash = digest(envelope["manifest"])
    row["content"] = json.dumps(envelope, sort_keys=True)
    row["content_sha256"] = hashlib.sha256(row["content"].encode()).hexdigest()
    row["size"] = len(row["content"].encode())
    row["provenance"]["input_hash"] = manifest_hash
    run["snapshot_json"].update(input_hash=manifest_hash, input_manifest_hash=manifest_hash)
    run["snapshot_hash"] = digest(run["snapshot_json"])
    run["result_json"]["artifacts"] = [{"path": row["path"], "sha256": row["content_sha256"],
                                         "size": row["size"], "content": row["content"]}]
    for step in run["steps"]:
        step["output_hash"] = digest(run["result_json"])
    previous = "0" * 64
    for sequence, event in enumerate(events, 1):
        if event["kind"] == "CHECK_COMPLETED":
            event["payload"]["result_hash"] = digest(run["result_json"])
        event["sequence"], event["prev_hash"] = sequence, previous
        event["payload_hash"] = digest(event["payload"])
        event["event_hash"] = digest({"scope": SCOPE.model_dump(mode="json"), "run_id": run["id"],
            **{key: event[key] for key in ("step_id", "sequence", "kind", "actor", "payload_hash", "prev_hash",
                                          "classification", "created_at")}})
        previous = event["event_hash"]


def checks(report, role):
    return {row["id"]: row for row in report[role]["checks"]}


@pytest.mark.parametrize("fixture,architect,android,handoff", [
    ("aligned", "PASS", "PASS", "NEEDS_ANDROID_BUILD_EVIDENCE"),
    ("bom-mismatch", "FAIL", "FAIL", "DOMAIN_CONFLICT"),
    ("missing-exported", "FAIL", "FAIL", "DOMAIN_CONFLICT"),
])
def test_actual_xml_scenarios_preserve_build_and_approval_boundary(fixture, architect, android, handoff):
    report = evaluate(fixture)
    assert report["architect"]["static_status"] == architect
    assert report["android"]["static_status"] == android
    assert report["android"]["readiness"] == ("UNKNOWN" if android == "PASS" else "BLOCKED")
    assert report["android"]["build_status"] == "NOT_RUN_NO_ANDROID_TOOLCHAIN"
    assert checks(report, "android")["build_device"]["status"] == "UNKNOWN"
    assert report["supervisor_handoff"] == handoff
    assert report["human_approval_required"] is True and report["business_actions_executed_by_sop"] is False
    assert report["stage_transition"] == "NOT_EXECUTED"
    assert len(report["architect"]["checks"]) == len(report["android"]["checks"]) == 4
    if fixture == "missing-exported":
        assert checks(report, "android")["application_contract"]["reason"] == "ANDROID_12_EXPORTED_REQUIRED"


@pytest.mark.parametrize("field", ["content", "size", "provenance"])
def test_parent_artifact_binding_rejects_observed_tampering(field):
    run, events = parent()
    row = run["artifacts"][0]
    if field == "content":
        row[field] += " "
    elif field == "size":
        row[field] += 1
    else:
        row[field]["input_hash"] = "0" * 64
    report = evaluate(run=run, events=events)
    assert checks(report, "architect")["parent_artifact_binding"]["status"] == "FAIL"
    assert checks(report, "architect")["hardware_android_bom"]["status"] == "UNKNOWN"
    assert report["supervisor_handoff"] == "DOMAIN_CONFLICT"


@pytest.mark.parametrize("condition", ["missing", "expired", "conflict", "unmeasured"])
def test_hardware_bom_gaps_remain_unknown(condition):
    run, events = parent()
    def change(manifest):
        item = manifest["metrics"]["bom_revision"]
        if condition == "missing":
            del manifest["metrics"]["bom_revision"]
        elif condition == "expired":
            item["valid_until"] = "2026-10-05T00:00:00Z"
        elif condition == "conflict":
            item["conflict"] = True
        else:
            item["status"] = "CANDIDATE"
    rebind_manifest(run, events, change)
    report = evaluate(run=run, events=events)
    assert checks(report, "architect")["parent_artifact_binding"]["status"] == "PASS"
    assert checks(report, "architect")["hardware_android_bom"]["status"] == "UNKNOWN"
    assert report["architect"]["readiness"] == "UNKNOWN"
    assert report["android"]["static_status"] == "PASS"


@pytest.fixture
def fixture_copy(tmp_path):
    root = tmp_path / "aurora-domain"
    shutil.copytree(ROOT / "fixtures/aurora-domain", root)
    return root


@pytest.mark.parametrize("condition", ["malformed", "doctype", "duplicate"])
def test_xml_parse_failures_are_evidence_failures(fixture_copy, condition):
    path = fixture_copy / "aligned.xml"
    xml = path.read_text()
    if condition == "malformed":
        xml = xml.replace("</manifest>", "")
    elif condition == "doctype":
        xml = xml.replace("<manifest ", "<!DOCTYPE manifest [<!ENTITY x 'A3'>]><manifest ")
    else:
        xml = xml.replace("<application ", '<uses-sdk android:minSdkVersion="26" android:targetSdkVersion="34" />'
                          + "<application ")
    path.write_text(xml)
    report = evaluate(loader=DomainArtifacts(fixture_copy))
    assert checks(report, "android")["manifest_parse"]["status"] == "FAIL"
    assert report["supervisor_handoff"] == "DOMAIN_CONFLICT"


def test_scope_selector_and_role_set_cannot_be_caller_grants():
    loader = DomainArtifacts()
    foreign = Scope(tenant_id="foreign", workspace_id=SCOPE.workspace_id, project_id=SCOPE.project_id)
    for scope, fixture, code in ((foreign, "aligned", "NOT_FOUND"),
                                 (SCOPE, "../aligned", "INVALID_DOMAIN_FIXTURE")):
        with pytest.raises(DomainError) as error:
            loader.load(scope, fixture)
        assert error.value.code == code
    run, events = parent()
    with pytest.raises(DomainError, match="DOMAIN_SKILLS_REQUIRED"):
        evaluate_domain(SCOPE, run, events, loader.load(SCOPE, "aligned"), {"architect": "only-one"})


def test_fixed_artifact_read_is_bounded_and_contract_scoped(fixture_copy):
    loader = DomainArtifacts(fixture_copy)
    (fixture_copy / "aligned.xml").write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(DomainError, match="DOMAIN_ARTIFACT_TOO_LARGE"):
        loader.load(SCOPE, "aligned")
    contract_path = fixture_copy / "contract.json"
    contract = json.loads(contract_path.read_text())
    contract["scope"]["project_id"] = "other-project"
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(DomainError, match="INVALID_DOMAIN_CONTRACT"):
        loader.load(SCOPE, "bom-mismatch")


@pytest.mark.parametrize("before,after", [("targetSdkVersion=\"34\"", "targetSdkVersion=\"35\""),
                                         ("android:value=\"1\"", "android:value=\"2\"")])
def test_interface_contradictions_fail_source_contract(fixture_copy, before, after):
    path = fixture_copy / "aligned.xml"
    path.write_text(path.read_text().replace(before, after))
    report = evaluate(loader=DomainArtifacts(fixture_copy))
    assert checks(report, "architect")["interface_contract"]["status"] == "FAIL"
    assert report["android"]["static_status"] == "FAIL"


def test_sources_are_stable_and_parent_receipts_remain_unchanged():
    run, events = parent()
    before = copy.deepcopy((run, events))
    loader = DomainArtifacts()
    bundle = loader.load(SCOPE, "aligned")
    report = evaluate_domain(SCOPE, run, events, bundle, SKILLS)
    assert (run, events) == before
    assert bundle == loader.load(SCOPE, "aligned")
    assert report == evaluate_domain(SCOPE, run, events, bundle, SKILLS)
    assert len(bundle["sources"]) == 2 and all(row["synthetic"] is True for row in bundle["sources"])
    assert report["sources"] == bundle["sources"]
    assert all(row["source_pointer"] for role in ("architect", "android") for row in report[role]["checks"])


def test_registered_skills_have_only_bound_source_read_capabilities():
    registry = SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS)
    for name, role in (("architect-contract", "architect"), ("android-manifest", "android")):
        skill = registry.load(name, role)
        assert skill.version == "domain-source-review-v1"
        assert set(skill.allowed_tools) == ROLE_TOOLS[role]
        assert "sandbox.execute" not in skill.allowed_tools


def test_synthetic_bound_clock_is_honored_but_non_synthetic_result_cannot_choose_clock(monkeypatch):
    run, events = parent()
    run["snapshot_json"]["evaluation_clock"] = "2026-10-05T12:00:00Z"
    rebind_manifest(run, events, lambda manifest: None)
    monkeypatch.setattr("forgeflow.contracts.utcnow", lambda: datetime(2027, 1, 1, tzinfo=UTC))
    assert checks(evaluate(run=run, events=events), "architect")["hardware_android_bom"]["status"] == "PASS"
    run["result_json"]["synthetic"] = False
    rebind_manifest(run, events, lambda manifest: None)
    report = evaluate(run=run, events=events)
    assert checks(report, "architect")["hardware_android_bom"]["status"] == "UNKNOWN"
