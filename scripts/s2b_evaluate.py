"""Execute the sealed 12-case / 13-scenario evaluation without rewriting inputs.

Five scenarios use a supplied trusted live callback. Eight faults use isolated
TEST_DRIVER state and the actual MCP, approval, parser, and durable-ledger code.
Fault strings never enter a model prompt. This script has no provider credentials
or automatic live-model entry point; the root's trusted adapter owns live setup.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "preparation/s2/model-evaluation"


def _checker():
    spec = importlib.util.spec_from_file_location("forgeflow_s2_frozen_checker", DATASET / "checkfixtures.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("FROZEN_CHECKER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_scenarios() -> tuple[dict, list[dict]]:
    """Validate canonical byte/semantic bindings before reading any case."""
    integrity = _checker().validate_freeze()
    dataset = json.loads((DATASET / "cases.json").read_text())
    scenarios = []
    for case in dataset["cases"]:
        scenarios.append({**copy.deepcopy(case), "scenario_id": case["id"]})
        for variant in case.get("variants", []):
            scenarios.append(
                {
                    **copy.deepcopy(case),
                    "inputs": copy.deepcopy(variant["inputs"]),
                    "gold": copy.deepcopy(variant["gold"]),
                    "model_result": copy.deepcopy(variant["model_result"]),
                    "scenario_id": variant["variant_id"],
                }
            )
    if len(scenarios) != 13 or sum(s["kind"] == "model_behavior" for s in scenarios) != 5:
        raise RuntimeError("FROZEN_SCENARIO_COUNT_CHANGED")
    return integrity, scenarios


def frozen_plan() -> dict:
    integrity, scenarios = load_scenarios()
    return {
        "schema_version": "forgeflow-grounded-frozen-eval-plan-v1",
        "status": "NOT_RUN",
        "dataset_integrity": integrity,
        "dataset_file_sha256": hashlib.sha256((DATASET / "cases.json").read_bytes()).hexdigest(),
        "freeze_file_sha256": hashlib.sha256((DATASET / "freeze.json").read_bytes()).hexdigest(),
        "case_count": 12,
        "scenario_count": 13,
        "real_model_scenarios": 5,
        "test_driver_scenarios": 8,
        "real_model_calls": 0,
        "automatic_retries": 0,
        "scenarios": [
            {
                "id": s["scenario_id"],
                "kind": s["kind"],
                "provenance": "REAL_LOCAL_MODEL_REQUIRED" if s["kind"] == "model_behavior" else "TEST_DRIVER",
                "fixture": s["inputs"]["fixture_ref"],
                "overlays": s["inputs"]["evidence_overlays"],
                "required_context_ids": [v["source_id"] for v in s["inputs"]["context"]],
                "gold": s["gold"],
                "test_fault_boundary": (s["inputs"]["test_fault"] or {}).get("injection_boundary"),
                "status": "NOT_RUN",
            }
            for s in scenarios
        ],
    }


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        file.write("\n")


def _parent_invariants(before: dict, after: dict) -> dict:
    checks = {
        "parent_unchanged": before == after,
        "technical_gate_unchanged": before["assessment_json"] == after["assessment_json"],
        "no_new_business_receipts": before["receipts"] == after["receipts"] == [],
        "no_stage_transition": before["project"] == after["project"]
        and after["project"]["current_stage"] == "PROTOTYPE",
        "no_approval_decisions": all(a["decision"] == "PENDING" for a in after["approvals"]),
    }
    _require(all(checks.values()), "PARENT_SECURITY_INVARIANT_FAILED")
    return checks


class _FaultProvider:
    """Single injected delivery at a provider boundary, never a fake model answer."""

    provenance = "TEST_DRIVER"

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.calls: list[dict] = []
        self.started = asyncio.Event()
        self.parser_error = None
        self.reply = None

    async def generate(self, call: dict) -> dict:
        from forgeflow.analysis_contracts import strict_json_object
        from forgeflow.analysis_provider import MODEL_DIGEST

        self.calls.append(copy.deepcopy(call))
        self.started.set()
        fault = self.scenario["inputs"]["test_fault"]
        # The case fault lives only in this TEST_DRIVER branch. It is deliberately
        # absent from call["messages"] and all model-visible context.
        sid = self.scenario["scenario_id"]
        if sid in {"M10", "M12"}:
            await asyncio.Event().wait()  # Caller simulates timeout/interruption after dispatch.
        if "raw_payload" in fault:
            try:
                strict_json_object(fault["raw_payload"])
            except Exception as error:
                self.parser_error = type(error).__name__
            else:
                raise RuntimeError("BAD_FROZEN_PAYLOAD_WAS_ACCEPTED")
        self.reply = {
            "status": "REFUSED" if sid == "M08" else "OUTPUT_INVALID",
            "content": None,
            "content_utf8": fault.get("raw_payload"),
            "actual_model_revision": MODEL_DIGEST,
            "finish_reason": fault.get("finish_reason", "stop"),
            "dispatched": True,
            "completion_confirmed": True,
            "usage": None,
            "transport": "TEST_DRIVER",
            "error_code": fault.get("error_code", "REFUSED" if sid == "M08" else "OUTPUT_INVALID"),
        }
        return copy.deepcopy(self.reply)


def _fault_environment(path: Path, scenario: dict):
    """Create synthetic test state; no connection to the task's live database."""
    from forgeflow import contracts
    from forgeflow.analysis_context import SOP_CONTENT, AnalysisContextBuilder, ExportGrant
    from forgeflow.analysis_store import AnalysisStore
    from forgeflow.analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
    from forgeflow.contracts import Principal, RunRequest, Scope, digest
    from forgeflow.gate import RULE_HASH, evaluate
    from forgeflow.policy import POLICY_HASH
    from forgeflow.skills import SkillRegistry
    from forgeflow.store import Store
    from forgeflow.templates import template_hash

    scope = Scope.model_validate(scenario["gold"]["expected_scope"])
    caller = Principal(
        subject_id="S2B_TEST_DRIVER_USER",
        kind="user",
        scopes=(scope,),
        clearance=0,
        roles=frozenset({"submitter", "pm", "supervisor", "hardware", "admin", "reviewer"}),
    )
    worker = Principal(
        subject_id="S2B_TEST_DRIVER_CHECK",
        kind="service",
        scopes=(scope,),
        roles=frozenset({"hardware", "worker"}),
    )
    store = Store(f"sqlite:///{path / 'isolated-fault.sqlite'}")
    store.create_schema()
    store.put_document(
        caller, scope, "aurora-evt-sop", "v1", SOP_CONTENT, set(caller.roles), 0,
        valid_until="2026-11-05T00:00:00Z",
    )
    run = store.submit(caller, RunRequest(scope=scope, fixture_id="aurora-pass"), "fault-parent")
    lease = store.claim(worker, scope, run["id"], "S2B_TEST_DRIVER", ttl=90)
    manifest = json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
    execution = {
        "status": "SUCCEEDED", "cleanup": "REMOVED", "synthetic": True,
        "orchestration": "TEST_DRIVER", "scan": {"status": "TEST_DRIVER"}, "mcp": {"status": "TEST_DRIVER"},
    }
    snapshot = {
        "fixture_version": "v1", "input_hash": digest(manifest), "input_manifest_hash": digest(manifest),
        "rule_hash": RULE_HASH, "policy_hash": POLICY_HASH, "code_hash": template_hash("evt-check"),
        "documents": [{"id": "aurora-evt-sop", "version": "v1", "source_hash": hashlib.sha256(SOP_CONTENT.encode()).hexdigest()}],
        "classification": 0, "valid_until": "2026-11-05T00:00:00Z",
    }
    parent = store.complete_check(
        worker, scope, run["id"], lease["token"], lease["epoch"], execution, evaluate(manifest, execution, now=contracts.utcnow()), snapshot
    )
    ledger = AnalysisStore(store)
    ledger.create_schema()
    context = AnalysisContextBuilder(store, grant=ExportGrant(scope=scope))
    provider = _FaultProvider(scenario)
    flow = AnalysisWorkflow(ledger, provider, context, SkillRegistry(ROOT / "skills", role_tools=ROLE_TOOLS))
    return store, ledger, scope, caller, parent, provider, flow


async def _execute_fault(scenario: dict, path: Path) -> dict:
    from sqlalchemy import select

    from forgeflow import contracts
    from forgeflow.analysis_store import AnalysisProviderSlot, AnalysisRun
    from forgeflow.contracts import DomainError, ExecutionSpec, Principal, digest
    from forgeflow.mcp_bridge import MCPBridge
    from forgeflow.sandbox import DockerSandbox
    from forgeflow.templates import template_hash

    store, ledger, scope, caller, before, provider, flow = _fault_environment(path, scenario)
    sid = scenario["scenario_id"]
    evidence: dict = {}
    analysis = None
    outcome = None
    if sid == "M05":
        pm = Principal(subject_id="S2B_TEST_DRIVER_PM", kind="agent", roles={"pm"}, scopes=(scope,))
        manifest = json.loads((ROOT / "fixtures/aurora-pass.json").read_text())
        spec = ExecutionSpec(
            scope=scope, run_id=before["id"], step_id="fault-deny", attempt=1, template_id="evt-check",
            input_manifest=manifest, input_hash=digest(manifest), code_hash=template_hash("evt-check"),
            image="sha256:" + "1" * 64,
        )
        result, transcript = await MCPBridge(
            DockerSandbox(docker="/S2B-TEST-DRIVER-NO-DOCKER-EXECUTION")
        ).call(pm, spec, scenario["inputs"]["test_fault"]["tool"], scenario["inputs"]["test_fault"]["arguments"])
        _require(result.get("error") == "DENIED" and transcript["tools"] == [], "PM_SANDBOX_NOT_DENIED")
        outcome = result["error"]
        evidence = {"gateway_result": result, "actual_mcp_transcript": transcript, "sandbox_executions": 0}
    elif sid == "M06":
        # Even an otherwise permitted reviewer role cannot turn an agent into a human.
        agent = caller.model_copy(update={"kind": "agent", "subject_id": "S2B_TEST_DRIVER_AGENT"})
        approval = before["approvals"][0]
        try:
            store.decide(agent, scope, approval["id"], approval["proposal_hash"], before["revision"], "APPROVED")
        except DomainError as error:
            _require(error.code == "HUMAN_REVIEW_REQUIRED", "WRONG_APPROVAL_DENIAL")
            outcome = "DENIED"
            evidence = {"approval_error": error.code, "approval_decisions": 0}
        else:
            raise RuntimeError("AGENT_APPROVAL_WAS_ACCEPTED")
    elif sid == "M11":
        # Establish four TEST_DRIVER reservations through the production ledger,
        # then exercise the true calls-remaining=0 guard before a fifth delivery.
        bundle = flow.context.build(caller, scope, before["id"], "supervisor")
        active = flow._activate()
        material = flow._material(active, bundle)
        started = ledger.start(caller, scope, before["id"], "fault-budget", material, reauthorize=flow._reauthorize)
        lease = ledger.claim(caller, scope, started["id"], flow.owner, reauthorize=flow._reauthorize)
        for index, (role, phase) in enumerate([
            ("supervisor", "PLAN"), ("pm", "CONTEXT_PROPOSAL"), ("pm", "FINAL_REPORT"), ("supervisor", "SUMMARY")
        ]):
            reserved = ledger.reserve_call(
                caller, scope, started["id"], lease["token"], lease["epoch"], flow.owner,
                f"budget-setup-{index}", role, phase, digest({"setup": index}), digest(material["source_snapshot"]),
                reauthorize=flow._reauthorize,
            )
            ledger.finish_call(
                caller, scope, started["id"], reserved["id"], lease["token"], lease["epoch"], flow.owner,
                {"test_driver_setup": index}, None, reauthorize=flow._reauthorize,
                provider_metadata={"transport": "TEST_DRIVER"},
            )
        try:
            ledger.reserve_call(
                caller, scope, started["id"], lease["token"], lease["epoch"], flow.owner,
                "budget-exhausted", "supervisor", "PLAN", digest({"new": True}), digest(material["source_snapshot"]),
                reauthorize=flow._reauthorize,
            )
        except DomainError as error:
            _require(error.code == "ANALYSIS_BUDGET_EXCEEDED", "WRONG_BUDGET_DENIAL")
            outcome = "BUDGET_EXHAUSTED"
            ledger.abort(caller, scope, started["id"], lease["token"], lease["epoch"], flow.owner, code=error.code)
            evidence = {"budget_error": error.code, "prior_test_driver_reservations": 4, "new_model_dispatches": 0}
            analysis = ledger.get(caller, scope, started["id"], reauthorize=flow._reauthorize)
        else:
            raise RuntimeError("EXHAUSTED_BUDGET_WAS_ACCEPTED")
    elif sid == "M12":
        task = asyncio.create_task(flow.analyze(caller, scope, before["id"], "same-original-logical-call"))
        await asyncio.wait_for(provider.started.wait(), timeout=3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise RuntimeError("TEST_DRIVER_INTERRUPT_NOT_OBSERVED")
        replay_provider = _FaultProvider(scenario)
        replacement = type(flow)(ledger, replay_provider, flow.context, flow.skills)
        analysis = await replacement.analyze(caller, scope, before["id"], "same-original-logical-call")
        _require(not replay_provider.calls, "LOST_REPLY_WAS_REDISPATCHED")
        with store._tx() as session:
            row = session.scalar(select(AnalysisRun).where(AnalysisRun.id == analysis["id"]))
            row.lease_expires_at = contracts.utcnow() - timedelta(seconds=1)
        recovered = ledger.recover(caller, scope, analysis["id"])
        _require(recovered["state"] == "OUTCOME_UNKNOWN", "RECOVERY_RELEASED_UNKNOWN_OUTCOME")
        analysis = ledger.get(caller, scope, analysis["id"], reauthorize=flow._reauthorize)
        outcome = analysis["state"]
        evidence = {"interrupt_observed": True, "new_model_dispatches_after_replay": 0, "recovered_state": recovered["state"]}
    else:
        if sid == "M10":
            class ShortDeadlineLedger:
                def __getattr__(self, name):
                    return getattr(ledger, name)

                def reserve_call(self, *args, **kwargs):
                    kwargs["timeout_seconds"] = 0.03
                    return ledger.reserve_call(*args, **kwargs)

            # An isolated TEST_DRIVER deadline tests the real wall-time guard
            # without holding the real model slot or waiting a full minute.
            flow.store = ShortDeadlineLedger()
        analysis = await flow.analyze(caller, scope, before["id"], f"fault-{sid.lower()}")
        if sid in {"M07", "M09"}:
            _require(provider.parser_error is not None, "PARSER_FAILURE_EVIDENCE_MISSING")
        outcome = "OUTCOME_UNKNOWN" if sid == "M10" else (
            "REFUSED" if provider.reply["status"] == "REFUSED" else analysis["calls"][0]["state"]
        )
        evidence = {
            "parser_error": provider.parser_error,
            "injected_signal": scenario["inputs"]["test_fault"].get("provider_signal"),
            "actual_test_driver_reply": provider.reply,
            "wall_deadline_seconds": 0.03 if sid == "M10" else None,
            "adapter_disposition": analysis["calls"][0]["state"],
        }
    after = store.get_run(caller, scope, before["id"])
    invariants = _parent_invariants(before, after)
    if analysis is not None and sid != "M11":
        _require(len(analysis["calls"]) == 1 and analysis["summary_json"] is None, "FAULT_BECAME_ACCEPTED_REPORT")
        expected_state = "OUTCOME_UNKNOWN" if sid in {"M10", "M12"} else "FAILED"
        _require(analysis["state"] == expected_state, "WRONG_DURABLE_FAULT_STATE")
        _require(analysis["calls"][0]["usage_state"] == "UNKNOWN", "UNKNOWN_USAGE_INVENTED")
    if sid in {"M10", "M12"}:
        _require(analysis["tokens_reserved"] == 4300, "UNKNOWN_RESERVATION_RELEASED")
        with store._tx() as session:
            slot = session.scalar(select(AnalysisProviderSlot))
            _require(slot.call_id == analysis["calls"][0]["id"], "UNKNOWN_GLOBAL_SLOT_RELEASED")
        evidence.update(budget_reservation="RETAINED_UNTIL_RECONCILIATION", local_cancel_is_remote_stop_proof=False)
    if sid in {"M05", "M06", "M11"}:
        _require(not provider.calls, "GUARD_DISPATCHED_MODEL")
    _require(outcome == scenario["gold"]["outcome"], "FAULT_OUTCOME_GOLD_MISMATCH")
    result = {
        "scenario_id": sid, "case_id": scenario["id"], "kind": scenario["kind"], "status": "PASS",
        "provenance": "TEST_DRIVER", "real_model_calls": 0, "test_driver_dispatches": len(provider.calls),
        "fault_appended_to_prompt": False, "outcome": outcome, "analysis_status": "INCOMPLETE",
        "durable_execution_state": None if analysis is None else analysis["state"],
        "usage_state": scenario["gold"]["usage_state"], "cost_microusd": None,
        "accepted_as_model_report": False, "automatic_retries": 0, "security_invariants": invariants,
        "parent_check_provenance": "TEST_DRIVER_NOT_LIVE_DOCKER",
        "evidence": evidence, "analysis": analysis,
    }
    store.engine.dispose()
    return result


async def run_faults(output_dir: Path) -> dict:
    """Run all eight faults in isolated ephemeral SQLite stores."""
    integrity, scenarios = load_scenarios()
    _require(not output_dir.exists(), "USE_NEW_EVIDENCE_DIRECTORY")
    output_dir.mkdir(parents=True)
    _write_new(output_dir / "plan.json", frozen_plan())
    rows = []
    for scenario in scenarios:
        if scenario["kind"] == "model_behavior":
            continue
        with tempfile.TemporaryDirectory(prefix="forgeflow-s2b-fault-") as folder:
            row = await _execute_fault(scenario, Path(folder))
        _write_new(output_dir / f"{scenario['scenario_id']}.json", row)
        rows.append(row)
    summary = {
        "schema_version": "forgeflow-grounded-frozen-fault-results-v1",
        "dataset_integrity": integrity, "provenance": "TEST_DRIVER", "scenario_count": len(rows),
        "passed": sum(r["status"] == "PASS" for r in rows), "real_model_calls": 0,
        "real_model_scenarios": "NOT_RUN_BY_THIS_COMMAND", "faults": rows,
    }
    _write_new(output_dir / "summary.json", summary)
    return summary


def _catalog_gold(scenario: dict, evidence: dict) -> dict:
    """Check frozen values/states and exact exported-source origins independently."""
    from forgeflow.contracts import canonical
    from forgeflow.gate import RULES, evaluate

    manifest = _checker().load_manifest(scenario)
    clock = datetime.fromisoformat(scenario["inputs"]["evaluation_clock"].replace("Z", "+00:00"))
    evaluation = evaluate(manifest, {"status": "SUCCEEDED", "cleanup": "REMOVED"}, now=clock)
    expected_states = {item["rule"]: item["status"] for item in evaluation["checks"]}
    origin_refs = evidence["source_origin_refs"]
    expected_sources = {item["source_id"]: item for item in scenario["inputs"]["context"]}
    call_sources = []
    for call in evidence["analysis"]["calls"]:
        snapshot = call["material_json"]["source_snapshot"]
        for bundle in snapshot.get("bundles", [snapshot]):
            call_sources.extend(bundle["sources"])
    origins_valid = True
    for source_id in expected_sources:
        expected_source = expected_sources[source_id]
        matches = [source for source in call_sources if source["id"] == source_id]
        origins_valid = origins_valid and bool(matches) and all(
            source["ref"] == origin_refs.get(source_id)
            and source["version"] == expected_source["version"]
            and source["source_hash"] == expected_source["source_hash"]
            for source in matches
        )
    authorized_refs = {source["ref"] for source in call_sources}
    overlay_metrics = {item["metric"] for item in scenario["inputs"]["evidence_overlays"]}
    overlay_id = next((item["source_id"] for item in expected_sources.values() if item["origin"] == "DERIVED_SYNTHETIC_EVAL_OVERLAY"), None)
    catalogs_valid = bool(evidence["grounding_records"])
    required_concerns = set()
    for metric in RULES["required"]:
        item = manifest["metrics"].get(metric)
        state = expected_states[metric]
        if state != "SATISFIED":
            detail = "VIOLATED" if state == "VIOLATED" else (
                "MISSING" if item is None else "CONFLICT" if item.get("conflict") else "EXPIRED"
            )
            required_concerns.add(f"{metric}:{detail}")
    if scenario["scenario_id"] == "M04":
        required_concerns.add("UNTRUSTED_INSTRUCTION")
    for record in evidence["grounding_records"]:
        catalog = record["claim_catalog"]
        observations = catalog["observations"]
        by_metric = {item["metric"]: item for item in observations}
        catalogs_valid = catalogs_valid and len(observations) == 5 and set(by_metric) == set(RULES["required"])
        for metric, observation in by_metric.items():
            source = manifest["metrics"].get(metric)
            state = expected_states[metric]
            evidence_state = "MISSING" if source is None else (
                "CONFLICT" if source.get("conflict") else
                "EXPIRED" if datetime.fromisoformat(source["valid_until"].replace("Z", "+00:00")) <= clock else
                "MEASURED"
            )
            catalogs_valid = catalogs_valid and (
                observation["check_state"] == state
                and observation["evidence_state"] == evidence_state
                and canonical(observation["value"]) == canonical(None if source is None else source.get("value"))
                and observation["source_ref"] in authorized_refs
            )
            if metric in overlay_metrics:
                catalogs_valid = catalogs_valid and (
                    observation["authority"] == "SYNTHETIC_OVERLAY"
                    and observation["source_ref"] == origin_refs.get(overlay_id)
                )
            else:
                catalogs_valid = catalogs_valid and observation["authority"] == "HARDWARE_CHECK"
        catalogs_valid = catalogs_valid and (
            {item["kind"] for item in catalog["concerns"]} == required_concerns
            and all(item["source_ref"] in authorized_refs for item in catalog["concerns"])
        )
        if scenario["scenario_id"] == "M04":
            catalogs_valid = catalogs_valid and all(
                item["source_ref"] == origin_refs.get("fixture:untrusted-injection-v1")
                for item in catalog["concerns"] if item["kind"] == "UNTRUSTED_INSTRUCTION"
            )
    return {
        "frozen_citation_origins": "PASS" if origins_valid else "FAIL",
        "typed_claim_gold": "PASS" if catalogs_valid else "FAIL",
        "required_concerns": sorted(required_concerns),
        "derived_evaluation_status": evaluation["status"],
        "source_origin_refs": origin_refs,
        "citation_scope": "EXACT_EXPORTED_BINDINGS_AND_CHECK_DERIVATION_NOT_FREE_MODEL_PROSE",
    }


def assess_live(scenario: dict, evidence: dict) -> dict:
    """Keep model schema acceptance and semantic acceptance separate."""
    from forgeflow.analysis_provider import MODEL_DIGEST

    sid = scenario["scenario_id"]
    before, after, analysis = evidence["parent_before"], evidence["parent_after"], evidence["analysis"]
    invariants = _parent_invariants(before, after)
    overlay = bool(scenario["inputs"]["evidence_overlays"])
    if not overlay:
        _require(before["assessment_json"]["status"] == scenario["gold"]["gate_status_given_confirmed_fixture_check"], "GOLD_GATE_MISMATCH")
    _require(bool(evidence["freeze"]), "ACTUAL_EXPERIMENT_FREEZE_MISSING")
    calls = analysis["calls"]
    records = evidence["grounding_records"]
    catalog_gold = _catalog_gold(scenario, evidence)
    _require(len(calls) <= 4, "REAL_MODEL_CALL_BUDGET_EXCEEDED")
    confirmed_calls, ambiguous_attempts, not_dispatched = 0, 0, 0
    for call in calls:
        metadata = call.get("provider_metadata_json") or {}
        if call["state"] == "OUTCOME_UNKNOWN":
            # fail_unknown intentionally cannot invent provider metadata. An
            # explicit trusted callback freezes the pinned request and retains
            # its uncertain reply/dispatch journal; this proves an attempt,
            # not that the remote model completed or stopped.
            frozen = evidence.get("call_freeze_refs", {}).get(call["id"], {})
            raw_reply = evidence.get("raw_provider_replies", {}).get(call["id"], {})
            _require(
                evidence.get("provider_provenance") == "OLLAMA_LOCAL_HTTP"
                and metadata.get("transport") in {None, "OLLAMA_LOCAL_HTTP"}
                and call.get("profile_revision") == MODEL_DIGEST
                and frozen.get("frozen_before_provider_dispatch") is True
                and frozen.get("input_hash") == call["input_hash"]
                and frozen.get("call", {}).get("call_id") == call["id"]
                and frozen.get("call", {}).get("profile_digest") == MODEL_DIGEST
                and (
                    raw_reply.get("status") == "OUTCOME_UNKNOWN"
                    or raw_reply.get("status") in {"PROVIDER_ERROR", "OUTPUT_INVALID"}
                    and raw_reply.get("completion_confirmed") is not True
                )
                and raw_reply.get("dispatched") is True
                and raw_reply.get("transport") in {None, "OLLAMA_LOCAL_HTTP"},
                "UNKNOWN_LOCAL_PROVIDER_WITNESS_MISSING",
            )
            ambiguous_attempts += 1
        elif call["state"] == "NOT_DISPATCHED":
            frozen = evidence.get("call_freeze_refs", {}).get(call["id"], {})
            raw_reply = evidence.get("raw_provider_replies", {}).get(call["id"], {})
            _require(
                evidence.get("provider_provenance") == "OLLAMA_LOCAL_HTTP"
                and metadata.get("transport") in {None, "OLLAMA_LOCAL_HTTP"}
                and call.get("profile_revision") == MODEL_DIGEST
                and call.get("delivery_status") == call.get("usage_state") == "NOT_DISPATCHED"
                and call.get("tokens_reserved") == 0
                and frozen.get("frozen_before_provider_dispatch") is True
                and frozen.get("input_hash") == call["input_hash"]
                and frozen.get("call", {}).get("call_id") == call["id"]
                and frozen.get("call", {}).get("profile_digest") == MODEL_DIGEST
                and raw_reply.get("status") == "DENIED"
                and raw_reply.get("dispatched") is False
                and raw_reply.get("transport") in {None, "OLLAMA_LOCAL_HTTP"},
                "NOT_DISPATCHED_LOCAL_PROVIDER_WITNESS_MISSING",
            )
            not_dispatched += 1
        else:
            _require(metadata.get("transport") == "OLLAMA_LOCAL_HTTP", "TEST_DRIVER_MISLABELED_REAL_MODEL")
            _require(call.get("delivery_status") == "DELIVERED", "MODEL_DELIVERY_UNCONFIRMED")
            confirmed_calls += 1
    # Grounding records contain actual raw outputs and independent strict/claim
    # checks. Failed output remains evidence; rejection is not model quality PASS.
    incomplete = bool(ambiguous_attempts or not_dispatched) or analysis["state"] in {"OUTCOME_UNKNOWN", "CANCELLED"}
    structure = not incomplete and bool(records) and all(r.get("structure_valid") is True for r in records)
    semantics = not incomplete and bool(records) and all(r.get("semantic_valid") is True for r in records) and (
        catalog_gold["typed_claim_gold"] == catalog_gold["frozen_citation_origins"] == "PASS"
    )
    complete_pipeline = len(calls) == 4 and len(records) == 4 and all(c["state"] == "VALIDATED" for c in calls)
    return {
        "scenario_id": sid, "case_id": scenario["id"], "kind": "model_behavior", "provenance": "REAL_LOCAL_MODEL",
        "real_model_calls": confirmed_calls, "confirmed_model_calls": confirmed_calls,
        "ambiguous_dispatch_attempts": ambiguous_attempts,
        "not_dispatched_reservations": not_dispatched, "test_driver_dispatches": 0,
        "call_count_definition": "CONFIRMED_RESPONSE_AND_AMBIGUOUS_DISPATCH_ARE_SEPARATE",
        "structural_validation": "INCOMPLETE" if incomplete else "PASS" if structure else "FAIL",
        "semantic_validation": "INCOMPLETE" if incomplete else "PASS" if semantics else "FAIL",
        "pipeline_completed": complete_pipeline,
        "model_quality_status": "PASS" if structure and semantics and complete_pipeline else "FAIL",
        "model_quality_scope": "CONSTRAINED_EVIDENCE_SELECTION_NOT_OPEN_ENDED_REASONING",
        "frozen_gold_hardware_precondition": "NOT_EXECUTED_FOR_DERIVED_OVERLAY" if overlay else "ACTUAL_CONFIRMED_CHECK",
        "full_frozen_gold_status": (
            "PARTIAL" if overlay else "PASS"
        ) if structure and semantics and complete_pipeline else "FAIL",
        "gold_assessment": catalog_gold,
        "derived_assessment": evidence.get("derived_assessment"),
        "technical_gate": before["assessment_json"]["status"], "analysis_state": analysis["state"],
        "security_invariants": invariants, "automatic_retries": 0,
        "grounding_records": records, "analysis": analysis, "events": evidence["events"], "freeze": evidence["freeze"],
        "raw_provider_replies": evidence.get("raw_provider_replies"),
        "call_freeze_refs": evidence.get("call_freeze_refs"),
    }


async def run_live(
    callback: Callable[[dict], Awaitable[dict]], output_dir: Path
) -> dict:
    """Execute exactly the five frozen behavior scenarios through a trusted adapter."""
    integrity, scenarios = load_scenarios()
    _require(callable(callback), "TRUSTED_LIVE_CALLBACK_REQUIRED")
    _require(not output_dir.exists(), "USE_NEW_EVIDENCE_DIRECTORY")
    output_dir.mkdir(parents=True)
    _write_new(output_dir / "plan.json", frozen_plan())
    rows = []
    for scenario in scenarios:
        if scenario["kind"] != "model_behavior":
            continue
        row = assess_live(scenario, await callback(copy.deepcopy(scenario)))
        _write_new(output_dir / f"{scenario['scenario_id']}.json", row)
        rows.append(row)
        if row["analysis_state"] in {"OUTCOME_UNKNOWN", "CANCELLED"}:
            break  # Unresolved provider reservations require reconciliation.
    summary = {
        "schema_version": "forgeflow-grounded-frozen-live-results-v1", "dataset_integrity": integrity,
        "provenance": "REAL_LOCAL_MODEL", "planned_scenario_count": 5, "executed_scenario_count": len(rows),
        "structural_passes": sum(r["structural_validation"] == "PASS" for r in rows),
        "semantic_passes": sum(r["semantic_validation"] == "PASS" for r in rows),
        "complete_quality_passes": sum(r["model_quality_status"] == "PASS" for r in rows),
        "full_frozen_gold_passes": sum(r["full_frozen_gold_status"] == "PASS" for r in rows),
        "full_frozen_gold_partial": sum(r["full_frozen_gold_status"] == "PARTIAL" for r in rows),
        "real_model_calls": sum(r["real_model_calls"] for r in rows),
        "confirmed_model_calls": sum(r["confirmed_model_calls"] for r in rows),
        "ambiguous_dispatch_attempts": sum(r["ambiguous_dispatch_attempts"] for r in rows),
        "not_dispatched_reservations": sum(r["not_dispatched_reservations"] for r in rows),
        "test_driver_faults": "RUN_SEPARATELY", "results": rows,
    }
    _write_new(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true")
    group.add_argument("--faults", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.plan:
        _write_new(args.output, frozen_plan())
        print(json.dumps({"status": "NOT_RUN", "scenarios": 13, "output": str(args.output)}))
    else:
        result = asyncio.run(run_faults(args.output))
        print(json.dumps({"passed": result["passed"], "scenarios": 8, "real_model_calls": 0, "output": str(args.output)}))


if __name__ == "__main__":
    main()
