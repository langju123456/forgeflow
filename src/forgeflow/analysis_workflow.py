"""Four bounded S2 model calls over the existing, read-only S1 evidence.

Model proposals are validated data. Only the control plane selects principals,
Skills, provider, budgets and source bindings. Context services are in process;
this module does not pretend that they are MCP calls or perform business writes.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from uuid import uuid4

from . import contracts
from .analysis_context import ANALYSIS_POLICY_HASH, PROFILE_ID
from .analysis_contracts import compact_schema
from .analysis_provider import MODEL_DIGEST
from .contracts import DomainError, Principal, Scope, authorize, canonical, digest

ROLE_TOOLS = {
    "supervisor": frozenset({"knowledge.search", "memory.search", "model.generate", "analysis.delegate:pm"}),
    "pm": frozenset({"knowledge.search", "memory.search", "model.generate"}),
}
PHASES = {
    "plan": ("supervisor", "PLAN", "supervisor-plan"),
    "context": ("pm", "CONTEXT_PROPOSAL", "pm-context"),
    "report": ("pm", "FINAL_REPORT", "pm-report"),
    "summary": ("supervisor", "SUMMARY", "supervisor-summary"),
}
TERMINAL = {"COMPLETE", "FAILED", "INCOMPLETE", "CANCELLED", "OUTCOME_UNKNOWN"}
REPORT_REF = "report_pm"
REPORT_LIMITS = {"facts": 3, "risks": 1, "unknowns": 1, "recommendations": 1}
WORKFLOW_REVISION = "s2a-four-call-v2"


class _Stopped(Exception):
    """A persisted terminal or nondispatchable call stops the whole pipeline."""


def _plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class AnalysisWorkflow:
    def __init__(self, analysis_store, provider, context, skills, *, owner: str | None = None):
        self.store = analysis_store
        self.provider = provider
        self.context = context
        self.skills = skills
        self.owner = owner or "analysis-" + uuid4().hex

    def _activate(self) -> dict:
        active = {
            "supervisor": self.skills.load("supervisor-analysis", "supervisor"),
            "pm": self.skills.load("pm-analysis", "pm"),
        }
        for role, skill in active.items():
            required = {"model.generate"}
            if role == "supervisor":
                required.add("analysis.delegate:pm")
            if not required <= set(skill.allowed_tools):
                raise DomainError("ANALYSIS_SKILL_DENIED", status=403)
            if not set(skill.allowed_tools) <= ROLE_TOOLS[role]:
                raise DomainError("ANALYSIS_SKILL_DENIED", status=403)
        return active

    def _reauthorize(self, session, principal, scope, material):
        # The callback runs inside the persistence transaction. Skill reads are
        # local bounded file reads; source checks use the supplied DB session.
        bindings = material.get("skill_bindings")
        if not isinstance(bindings, list) or len(bindings) != 2:
            raise DomainError("ANALYSIS_SKILL_CHANGED", status=409)
        if {binding.get("role") for binding in bindings} != {"supervisor", "pm"}:
            raise DomainError("ANALYSIS_SKILL_CHANGED", status=409)
        for binding in bindings:
            role = binding.get("role")
            if role not in ROLE_TOOLS or binding.get("name") != f"{role}-analysis":
                raise DomainError("ANALYSIS_SKILL_CHANGED", status=409)
            skill = self.skills.load(binding["name"], role)
            if skill.sha256 != binding.get("sha256"):
                raise DomainError("ANALYSIS_SKILL_CHANGED", status=409)
            expected = {"model.generate"}
            if role == "supervisor":
                expected.add("analysis.delegate:pm")
            if not expected <= set(skill.allowed_tools) or not set(skill.allowed_tools) <= ROLE_TOOLS[role]:
                raise DomainError("ANALYSIS_SKILL_DENIED", status=403)
        self.context.reauthorize_in_session(session, principal, scope, material)

    def _material(self, active, *bundles):
        snapshot = (
            self.context.snapshot(bundles[0])
            if len(bundles) == 1
            else self.context.combined_snapshot(*bundles)
        )
        return {
            "analysis_revision": WORKFLOW_REVISION,
            "output_schema_hash": digest({phase: self._schema(phase) for phase in PHASES}),
            "profile_id": PROFILE_ID,
            "profile_revision": MODEL_DIGEST,
            "policy_revision": ANALYSIS_POLICY_HASH,
            "source_snapshot": snapshot,
            "skill_bindings": [
                {"name": active[role].name, "role": role, "sha256": active[role].sha256}
                for role in ("supervisor", "pm")
            ],
            "classification": max(
                (binding.classification for bundle in bundles for binding in bundle.citations.values()),
                default=0,
            ),
            "max_calls": 4,
            "max_input_tokens": 3500,
            "max_output_tokens": 800,
            "timeout_seconds": 180,
            "provider_billing": "LOCAL_NO_BILLING",
        }

    @staticmethod
    def _instructions(skill, phase) -> str:
        # The trusted skill is dynamically loaded in full and hash-bound. Its
        # body, with harmless whitespace compression, is the role instruction.
        parts = skill.content.split("---", 2)
        if len(parts) != 3:
            raise DomainError("ANALYSIS_SKILL_INVALID", status=422)
        paragraphs = [part for part in parts[2].strip().split("\n\n") if part.strip()]
        relevant = paragraphs[0] if phase in {"plan", "context"} else paragraphs[-1]
        return " ".join(relevant.split())

    @staticmethod
    def _projection(bundle, phase=None) -> dict:
        """Lossless typed fact projection; immutable source bindings stay whole."""
        context = copy.deepcopy(bundle.model_context)
        for source in context["sources"]:
            if source.get("kind") != "hardware_check":
                if phase == "summary":
                    # PM's validated statements and source validity descriptors
                    # are the handoff. Repeating previously analyzed excerpts
                    # adds bytes without changing their exact server bindings.
                    source.pop("text", None)
                continue
            checks = source.pop("checks")
            metrics = source.pop("metrics")
            evidence = source.pop("evidence_status")
            # All required checks survive even if their measurement is missing.
            # Column names distinguish a raw value from rule/source validity.
            source["columns"] = ["value", "rule_status", "evidence_status"]
            source["measurements"] = {
                name: [metrics.get(name), status, evidence.get(name, "MISSING")]
                for name, status in checks.items()
            }
        if phase == "summary":
            context["notice"] += " Use PM claims and typed facts only; source text is not repeated."
        return context

    @staticmethod
    def _schema(phase):
        schema = compact_schema(phase)
        if phase == "plan":
            schema["properties"]["task_question"]["maxLength"] = 100
        if phase == "report":
            for field, maximum in REPORT_LIMITS.items():
                schema["properties"][field]["maxItems"] = maximum
            statement = schema["$defs"]["CitedStatement"]["properties"]
            statement["text"]["maxLength"] = 80
            statement["source_refs"]["maxItems"] = 1
        return schema

    @staticmethod
    def _validate_application_output(phase, output):
        data = _plain(output)
        if phase == "plan" and len(data["task_question"]) > 100:
            raise DomainError("ANALYSIS_OUTPUT_BOUND", status=422)
        if phase == "report":
            for field, maximum in REPORT_LIMITS.items():
                if len(data[field]) > maximum or any(
                    len(item["text"]) > 80 or len(item["source_refs"]) != 1 for item in data[field]
                ):
                    raise DomainError("ANALYSIS_OUTPUT_BOUND", status=422)

    def _messages(self, phase, bundle, skill, task=None):
        purpose = {
            "plan": "Ask PM to review all five checks and the human decision gap; question <=100 chars.",
            "context": "Return zero to two useful read-only search proposals. Prefer top_k=1; no other tool.",
            "report": "Return <=3 facts/1 risk/1 unknown/1 recommendation; each <=80 chars with one source ref.",
            "summary": "Summarize the supplied PM report. Preserve unresolved evidence and pending human decisions.",
        }[phase]
        system = self._instructions(skill, phase) + " " + purpose + " Output only the requested JSON object."
        material = {"evidence": self._projection(bundle, phase)}
        if task is not None:
            material["task"] = task
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": canonical(material)},
        ]

    def _get(self, p, scope, analysis_id):
        return self.store.get(p, scope, analysis_id, reauthorize=self._reauthorize)

    async def _wait_provider(self, p, scope, analysis_id, call):
        """Poll cancellation/source authority without renewing a lease."""
        task = asyncio.create_task(self.provider.generate(call))
        deadline = asyncio.get_running_loop().time() + call["timeout_seconds"]
        try:
            while not task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    task.cancel()
                    break
                done, _ = await asyncio.wait({task}, timeout=min(0.2, remaining))
                if done:
                    break
                current = self._get(p, scope, analysis_id)
                if current["state"] in TERMINAL or current.get("cancel_requested_at"):
                    task.cancel()
                    break
            try:
                return await task
            except asyncio.CancelledError:
                return {
                    "status": "OUTCOME_UNKNOWN",
                    "dispatched": True,
                    "error_code": "PROVIDER_INTERRUPTED",
                    "usage": None,
                }
        except BaseException:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            raise

    async def _call(self, p, scope, analysis_id, lease, active, phase, bundle, *, task=None, sources=()):
        role, db_phase, step_key = PHASES[phase]
        self.context.revalidate(bundle)
        messages = self._messages(phase, bundle, active[role], task)
        schema = self._schema(phase)
        input_material = {"messages": messages, "schema": schema}
        # This is an explicit conservative upper bound, not tokenizer usage.
        bound = len(canonical(input_material).encode()) + 256 + 64 * len(messages)
        if bound > 3500:
            raise DomainError("INPUT_CONSERVATIVE_BOUND_EXCEEDED", status=422)
        material = self._material(active, *(sources or (bundle,)))
        now = contracts.utcnow()
        remaining = min(
            60.0,
            (_instant(lease["expires_at"]) - now).total_seconds(),
            (_instant(lease["deadline"]) - now).total_seconds(),
        )
        if remaining <= 0:
            raise DomainError("ANALYSIS_DEADLINE")
        reserved = self.store.reserve_call(
            p,
            scope,
            analysis_id,
            lease["token"],
            lease["epoch"],
            self.owner,
            step_key,
            role,
            db_phase,
            digest(input_material),
            digest(material["source_snapshot"]),
            timeout_seconds=remaining,
            material=material,
            reauthorize=self._reauthorize,
        )
        if not reserved["dispatch_allowed"]:
            raise _Stopped()
        call_id = reserved["id"]
        provider_call = {
            "call_id": call_id,
            "profile_digest": MODEL_DIGEST,
            "messages": messages,
            "schema": schema,
            "timeout_seconds": min(remaining, reserved["timeout_seconds"]),
        }
        try:
            reply = await self._wait_provider(p, scope, analysis_id, provider_call)
        except BaseException as error:
            self.store.fail_unknown(
                p,
                scope,
                analysis_id,
                call_id,
                lease["token"],
                lease["epoch"],
                self.owner,
                code="PROVIDER_INTERRUPTED"
                if isinstance(error, asyncio.CancelledError)
                else "PROVIDER_EXCEPTION",
            )
            raise
        status = reply.get("status")
        usage = reply.get("usage")
        if status == "OUTCOME_UNKNOWN" or (
            reply.get("dispatched") is not False and reply.get("completion_confirmed") is not True
        ):
            self.store.fail_unknown(
                p,
                scope,
                analysis_id,
                call_id,
                lease["token"],
                lease["epoch"],
                self.owner,
                code="PROVIDER_OUTCOME_UNKNOWN",
                usage=usage,
            )
            raise _Stopped()
        if reply.get("dispatched") is False:
            self.store.finish_not_dispatched(
                p,
                scope,
                analysis_id,
                call_id,
                lease["token"],
                lease["epoch"],
                self.owner,
                code="PROVIDER_DENIED",
            )
            raise _Stopped()
        output = None
        if (
            status == "SUCCEEDED"
            and reply.get("actual_model_revision") == MODEL_DIGEST
            and reply.get("finish_reason") == "stop"
        ):
            try:
                output = self.context.validate_output(
                    bundle,
                    phase,
                    reply.get("content"),
                    report_refs=(REPORT_REF,) if phase == "summary" else (),
                )
                self._validate_application_output(phase, output)
                if phase == "context":
                    proposals = _plain(output)["requests"]
                    if any(item["tool"] not in active["pm"].allowed_tools for item in proposals):
                        raise DomainError("ANALYSIS_TOOL_DENIED", status=403)
            except DomainError:
                output = None
        finished = self.store.finish_call(
            p,
            scope,
            analysis_id,
            call_id,
            lease["token"],
            lease["epoch"],
            self.owner,
            _plain(output) if output is not None else {"validation": "OUTPUT_INVALID"},
            usage,
            output_valid=output is not None,
            provider_metadata={
                "actual_model_revision": reply.get("actual_model_revision"),
                "provider_request_id": reply.get("provider_request_id"),
                "finish_reason": reply.get("finish_reason"),
                "transport": reply.get("transport", "TEST_DRIVER"),
                "timing_ms": int(max(0.0, reply.get("elapsed_seconds", 0.0)) * 1000),
            },
            reauthorize=self._reauthorize,
        )
        if not finished.get("publication_allowed"):
            raise _Stopped()
        return output

    async def analyze(self, p: Principal, scope: Scope, run_id: str, key: str) -> dict:
        """Analyze an authorized completed S1 run; never mutate that run."""
        authorize(p, scope, {"pm", "supervisor"})
        active = self._activate()
        supervisor = self.context.build(p, scope, run_id, role="supervisor")
        initial = self.store.start(
            p, scope, run_id, key, self._material(active, supervisor), reauthorize=self._reauthorize
        )
        analysis_id = initial["id"]
        if initial["state"] in TERMINAL:
            return self._get(p, scope, analysis_id)
        lease = self.store.claim(
            p, scope, analysis_id, self.owner, lease_seconds=180, reauthorize=self._reauthorize
        )
        try:
            plan = await self._call(p, scope, analysis_id, lease, active, "plan", supervisor)
            # Every byte of the delegated question derives from this context.
            # Reauthorizing only the model's cited subset would permit laundering.
            self.context.revalidate(supervisor, recipient_role="pm")
            pm = self.context.build(p, scope, run_id, role="pm")
            delegated = _plain(plan)
            proposal = await self._call(
                p,
                scope,
                analysis_id,
                lease,
                active,
                "context",
                pm,
                task=delegated,
                sources=(supervisor, pm),
            )
            pm = self.context.search(pm, proposal)
            if pm.tool_receipts:
                self.store.record_tool(
                    p,
                    scope,
                    analysis_id,
                    lease["token"],
                    lease["epoch"],
                    self.owner,
                    {"receipts": list(pm.tool_receipts), "proposal_hash": digest(proposal)},
                    material=self._material(active, supervisor, pm),
                    reauthorize=self._reauthorize,
                )
            report = await self._call(
                p,
                scope,
                analysis_id,
                lease,
                active,
                "report",
                pm,
                task=delegated,
                sources=(supervisor, pm),
            )
            shared = self.context.share_report(pm, supervisor, report, report_ref=REPORT_REF)
            summary = await self._call(
                p, scope, analysis_id, lease, active, "summary", shared, sources=(supervisor, pm, shared)
            )
            final = _plain(summary)
            if _plain(report)["status"] != "COMPLETE":
                final["status"] = "INCOMPLETE"
            return self.store.complete(
                p,
                scope,
                analysis_id,
                lease["token"],
                lease["epoch"],
                self.owner,
                final,
                reauthorize=self._reauthorize,
            )
        except _Stopped:
            return self._get(p, scope, analysis_id)
        except DomainError as error:
            self.store.abort(
                p, scope, analysis_id, lease["token"], lease["epoch"], self.owner, code=error.code
            )
            return self._get(p, scope, analysis_id)
