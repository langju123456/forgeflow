"""Deterministic Supervisor -> role Skill -> context -> MCP -> evidence -> approval.

This first local chain has no model calls. A process boundary is used for MCP;
human approval is a separate API action, never a Supervisor capability.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import contracts
from .contracts import DomainError, ExecutionResult, ExecutionSpec, Principal, Scope, digest
from .gate import RULE_HASH, evaluate
from .policy import POLICY_HASH, execution_policy

ROOT = Path(__file__).resolve().parents[2]


def verify_checker_artifact(result: ExecutionResult, manifest: dict) -> bool:
    """Explicit checks remain enforced under python -O; output is not authority."""
    try:
        artifact = next(a for a in result.artifacts if a.path == "result.json")
        payload = json.loads(artifact.content)
        return (
            hashlib.sha256(artifact.content.encode()).hexdigest() == artifact.sha256
            and len(artifact.content.encode()) == artifact.size
            and payload["schema_version"] == "evt-check-v1"
            and digest(payload["manifest"]) == digest(manifest)
            and payload["runtime"] == {"uid": 10001, "gid": 10001, "seccomp": "2"}
        )
    except (StopIteration, KeyError, ValueError, TypeError):
        return False


class Workflow:
    def __init__(self, store, sandbox, scanner, bridge, knowledge, memory, skills):
        self.store, self.sandbox, self.scanner, self.bridge = store, sandbox, scanner, bridge
        self.knowledge, self.memory, self.skills = knowledge, memory, skills

    def service(self, scope: Scope) -> Principal:
        return Principal(
            subject_id="forgeflow-local-worker",
            kind="service",
            roles=frozenset({"worker", "memory_writer", "hardware"}),
            scopes=(scope,),
        )

    def role(self, scope: Scope) -> Principal:
        return Principal(
            subject_id="aurora-hardware-role", kind="agent", roles=frozenset({"hardware"}), scopes=(scope,)
        )

    async def check(self, scope: Scope, run_id: str) -> dict:
        service, role = self.service(scope), self.role(scope)
        run = self.store.get_run(service, scope, run_id)
        if run.get("assessment_json") is not None:
            return self.consolidate(scope, run_id)
        claim = self.store.claim(service, scope, run_id, "local-worker", ttl=90)
        token = claim.get("token", claim.get("lease_token"))
        epoch = claim.get("epoch", claim.get("fencing_epoch"))
        try:
            request = run.get("request", run.get("request_json"))
            manifest = json.loads((ROOT / "fixtures" / (request["fixture_id"] + ".json")).read_text())
            skill = self.skills.load("hardware-evt", "hardware")
            # ACL selection precedes scoring inside KnowledgeService.
            hits = self.knowledge.search(role, scope, "Aurora EVT", top_k=5)
            prior = self.memory.search(role, scope, "Aurora EVT", top_k=5)
            template = "timeout" if request["fixture_id"] == "aurora-timeout" else "evt-check"
            source_hash = self.sandbox.template_hash(template)
            if "sandbox.execute" not in skill.allowed_tools:
                raise DomainError("SKILL_TOOL_DENIED", "Activated skill has no execution grant", 403)
            identity = {
                "scope": scope.model_dump(mode="json"),
                "run_id": run_id,
                "step_id": claim["step_id"],
                "attempt": claim["attempt"],
            }
            scanner_handle = self.scanner.handle(scope, run_id, claim["step_id"], claim["attempt"])
            self.store.set_scanner_handle(service, scope, run_id, token, epoch, scanner_handle)
            lease_deadline = datetime.fromisoformat(
                claim["lease_expires_at"].replace("Z", "+00:00")
            ).timestamp()
        except Exception as error:
            failed = {
                "status": "DENIED" if isinstance(error, DomainError) else "FAILED",
                "cleanup": "NOT_STARTED",
                "reason": getattr(error, "code", type(error).__name__),
            }
            if self.store.get_run(service, scope, run_id).get("cancel_requested_at"):
                return self.store.acknowledge_cancel(service, scope, run_id, token, epoch, "NOT_STARTED")
            return self.store.fail_check(service, scope, run_id, token, epoch, failed)
        try:
            scan = await asyncio.to_thread(
                self.scanner.scan,
                template,
                handle=scanner_handle,
                identity=identity,
                lease_deadline=lease_deadline,
            )
        except Exception as error:
            scan = {
                "status": "ERROR",
                "source_hash": source_hash,
                "cleanup": "FAILED",
                "reason": "Scanner controller raised " + type(error).__name__,
            }
        snapshot = {
            "input_manifest_hash": digest(manifest),
            "input_hash": digest(manifest),
            "policy_hash": POLICY_HASH,
            "skill_hash": skill.sha256,
            "rule_hash": RULE_HASH,
            "code_hash": source_hash,
            "fixture_version": request.get("fixture_version", "v1"),
            "documents": [
                {"id": h.id, "version": h.version, "source_hash": h.source_hash}
                for h in hits
                if h.status == "CURRENT"
            ],
            "valid_until": "2026-11-05T00:00:00Z",
        }
        transcript = {"status": "NOT_STARTED"}
        handle = ""
        try:
            spec = ExecutionSpec(
                scope=scope,
                run_id=run_id,
                step_id=claim["step_id"],
                attempt=claim["attempt"],
                template_id=template,
                input_manifest=manifest,
                input_hash=digest(manifest),
                code_hash=source_hash,
                image=self.sandbox.resolve_image(),
                timeout_seconds=2 if template == "timeout" else 20,
            )
            execution_policy(
                role,
                spec,
                scan.get("source_hash", scan.get("code_hash", "")),
                scan.get("status", "UNAVAILABLE"),
            )
            # Recovery must know the exact container identity before startup.
            handle = self.sandbox.handle(spec)
            self.store.set_sandbox_handle(service, scope, run_id, token, epoch, handle)
            result, transcript = await self.bridge.execute(
                role,
                spec,
                scan_receipt=scan,
                lease_deadline=lease_deadline,
                cancel_requested=lambda: bool(
                    self.store.get_run(service, scope, run_id).get("cancel_requested_at")
                ),
            )
        except Exception as error:
            cleanup = (
                "NOT_STARTED"
                if not handle
                else ("REMOVED" if await asyncio.to_thread(self.sandbox.cleanup, handle) else "FAILED")
            )
            result = ExecutionResult(
                handle=handle,
                status="UNAVAILABLE",
                cleanup=cleanup,
                reason=f"{type(error).__name__}: {str(error)[:600]}",
            )
        result_dict = result.model_dump(mode="json")
        if scan.get("cleanup") == "FAILED":
            result_dict["cleanup"] = "FAILED"
        if self.store.get_run(service, scope, run_id).get("cancel_requested_at"):
            return self.store.acknowledge_cancel(service, scope, run_id, token, epoch, result_dict["cleanup"])
        # The independent control-plane verifier checks exact artifact bytes and input provenance.
        if result.status == "SUCCEEDED" and not verify_checker_artifact(result, manifest):
            result_dict.update(status="FAILED", reason="Independent artifact verification failed")
        # No model/program self-report can override these independently held rules/input facts.
        assessment = evaluate(manifest, result_dict, now=contracts.utcnow())
        report = {
            **result_dict,
            "scan": scan,
            "mcp": transcript,
            "orchestration": "DETERMINISTIC_NO_LLM",
            "skill": skill.model_dump(mode="json"),
            "knowledge_count": len(hits),
            "prior_memory_count": len(prior),
            "synthetic": True,
            "supervisor_plan": {
                "planner": "DETERMINISTIC",
                "role": "hardware",
                "skill": skill.name,
                "tools": list(skill.allowed_tools),
                "maximum_check_seconds": 20,
                "reason": "EVT fixture requires a scoped hardware manifest checker",
            },
        }
        self.store.complete_check(service, scope, run_id, token, epoch, report, assessment, snapshot)
        return self.consolidate(scope, run_id)

    def consolidate(self, scope: Scope, run_id: str) -> dict:
        """A crash after CHECK_COMPLETED can safely retry only the candidate write."""
        service = self.service(scope)
        run = self.store.get_run(service, scope, run_id)
        assessment = run.get("assessment_json")
        if assessment is None:
            raise DomainError("NO_ASSESSMENT")
        events = self.store.events(service, scope, run_id)
        completed = [event for event in events if event["kind"] == "CHECK_COMPLETED"]
        if completed:
            event_id = completed[-1]["id"]
            self.memory.propose(
                service,
                scope,
                run_id,
                "Aurora EVT readiness",
                f"Synthetic EVT assessment {assessment['status']}; check {run['result_json']['status']}; "
                "stage transition awaits a separate human decision.",
                [event_id],
            )
        return self.store.get_run(service, scope, run_id)

    def dispatch(self, scope: Scope, run_id: str, action_id: str) -> dict:
        service = self.service(scope)
        run = self.store.get_run(service, scope, run_id)
        snapshot = run.get("snapshot", run.get("snapshot_json", {}))
        request = run.get("request", run.get("request_json", {}))
        template = "timeout" if request.get("fixture_id") == "aurora-timeout" else "evt-check"
        if any(
            snapshot.get(key) != value
            for key, value in {
                "policy_hash": POLICY_HASH,
                "rule_hash": RULE_HASH,
                "skill_hash": self.skills.load("hardware-evt", "hardware").sha256,
                "code_hash": self.sandbox.template_hash(template),
                "input_hash": digest(
                    json.loads((ROOT / "fixtures" / (request["fixture_id"] + ".json")).read_text())
                ),
            }.items()
        ):
            raise DomainError("PROPOSAL_CHANGED", "Current policy, rule, skill or code differs from approval")
        if any(
            action["id"] == action_id and action["status"] == "SUCCEEDED" for action in run.get("actions", [])
        ):
            # Receipt replay has no effect and requires fresh object authorization above.
            return self.store.dispatch_local(
                service, scope, run_id, action_id, "local-dispatch", lease_token="", epoch=-1
            )
        claim = self.store.claim(service, scope, run_id, "local-dispatch", ttl=30)
        result = self.store.dispatch_local(
            service,
            scope,
            run_id,
            action_id,
            "local-dispatch",
            lease_token=claim.get("token", claim.get("lease_token")),
            epoch=claim.get("epoch", claim.get("fencing_epoch")),
        )
        if result.get("state") != "COMPLETED":
            self.store.release_dispatch(service, scope, run_id, claim["token"], claim["epoch"])
        return result

    def recover(self, scope: Scope, run_id: str) -> dict:
        service = self.service(scope)
        view = self.store.get_run(service, scope, run_id)
        if view.get("cleanup_required") and not any(step.get("lease_token") for step in view["steps"]):
            step = next(step for step in view["steps"] if step["step_key"] == view["current_step"])
            recovery = {**step, "step_id": step["id"]}
        else:
            recovery = self.store.recover(service, scope, run_id)
        handle = recovery.get("sandbox_handle", recovery.get("handle"))
        cleanup = (
            "REMOVED"
            if handle and self.sandbox.cleanup(handle)
            else ("NOT_STARTED" if not handle else "FAILED")
        )
        scan_handle = recovery.get("scanner_handle")
        if scan_handle:
            identity = {
                "scope": scope.model_dump(mode="json"),
                "run_id": run_id,
                "step_id": recovery["step_id"],
                "attempt": recovery["attempt"],
            }
            scan_cleanup = self.scanner.cleanup(scan_handle, identity=identity)
            if scan_cleanup.get("cleanup") != "REMOVED":
                cleanup = "FAILED"
        return self.store.acknowledge_recovery(service, scope, run_id, cleanup)
