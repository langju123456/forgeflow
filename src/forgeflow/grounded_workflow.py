"""New grounded application revision using the sealed durable four-call engine."""

from __future__ import annotations

from .analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from .contracts import DomainError, canonical, digest
from .grounded_contracts import QUESTION, REVISION, render, schema, semantic_check

SKILLS = {role: f"{role}-grounded" for role in ROLE_TOOLS}


class GroundedWorkflow(AnalysisWorkflow):
    def __init__(self, *args, grounding_store, freeze_call=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.grounding_store = grounding_store
        self.freeze_call = freeze_call
        self._replies = {}
        self._pending_catalogs = {}

    def _activate(self):
        active = {role: self.skills.load(name, role) for role, name in SKILLS.items()}
        for role, skill in active.items():
            required = {"model.generate"} | ({"analysis.delegate:pm"} if role == "supervisor" else set())
            if not required <= set(skill.allowed_tools) or not set(skill.allowed_tools) <= ROLE_TOOLS[role]:
                raise DomainError("ANALYSIS_SKILL_DENIED", status=403)
        return active

    def _reauthorize(self, session, principal, scope, material):
        bindings = material.get("skill_bindings", [])
        if len(bindings) != 2 or {b.get("role") for b in bindings} != set(SKILLS):
            raise DomainError("ANALYSIS_SKILL_CHANGED")
        active = self._activate()
        for binding in bindings:
            role = binding["role"]
            if binding.get("name") != SKILLS[role] or binding.get("sha256") != active[role].sha256:
                raise DomainError("ANALYSIS_SKILL_CHANGED")
        if material.get("analysis_revision") != REVISION:
            raise DomainError("GROUNDING_REVISION_CHANGED")
        if material.get("output_schema_hash") != digest({phase: self._schema(phase) for phase in (
            "plan", "context", "report", "summary"
        )}):
            raise DomainError("GROUNDING_REVISION_CHANGED")
        self.context.reauthorize_in_session(session, principal, scope, material)

    def _material(self, active, *bundles):
        material = super()._material(active, *bundles)
        material["analysis_revision"] = REVISION
        material["claim_catalog_hash"] = digest(self.context.catalog(bundles[-1]))
        return material

    @staticmethod
    def _schema(phase):
        return schema(phase)

    @staticmethod
    def _validate_application_output(phase, output):
        # The context already checked strict schema plus exact semantic coverage.
        return None

    def _messages(self, phase, bundle, skill, task=None):
        catalog = self.context.catalog(bundle)
        sources = bundle.model_context["sources"]
        run_source = next(source for source in sources if source["kind"] == "hardware_check")
        material = {
            "source_ref": run_source["ref"],
            "observations_columns": ["id", "metric", "value", "check_state", "evidence_state", "source_ref"],
            "observations": [[item[k] for k in (
                "id", "metric", "value", "check_state", "evidence_state", "source_ref"
            )] for item in catalog["observations"]],
            "concerns_columns": ["id", "kind", "source_ref"],
            "concerns": [[item[k] for k in ("id", "kind", "source_ref")] for item in catalog["concerns"]],
        }
        # The adversarial document must reach the actual model as untrusted data.
        untrusted = [source for source in sources if source.get("kind") == "untrusted_document"]
        if untrusted:
            material["untrusted_documents"] = (
                [{"ref": source["ref"], "kind": source["kind"], "status": source["status"]}
                 for source in untrusted] if phase == "summary" else untrusted
            )
        if phase in {"plan", "context"}:
            material.pop("observations_columns")
            material["observations"] = [
                [item["metric"], item["check_state"], item["evidence_state"]]
                for item in catalog["observations"]
            ]
        if phase == "summary":
            material["pm_report"] = bundle.model_context["reports"][0]
        instructions = {
            "plan": f"Delegate to pm. task_question must be exactly: {QUESTION}. Cite source_ref.",
            "context": "Propose zero to two useful read-only searches; prefer top_k=1.",
            "report": (
                "Select every observation id and every concern id exactly once. COMPLETE only when all "
                "checks are SATISFIED and MEASURED, otherwise INCOMPLETE. recommendation HUMAN_REVIEW_REQUIRED."
            ),
            "summary": (
                "Preserve PM observation ids, concern ids and status. report_refs [report_pm]. "
                "human_approval_required true."
            ),
        }[phase]
        return [
            {"role": "system", "content": (
                self.context.frozen_prompt + " " + self._instructions(skill, phase) + " " + instructions
                + " The phase schema is the grounded revision of the proposed output contract. JSON only."
            )},
            {"role": "user", "content": canonical(material)},
        ]

    async def _wait_provider(self, p, scope, analysis_id, call):
        if self.freeze_call is not None:
            # Called after durable intent and before any provider dispatch.
            self.freeze_call(call)
        reply = await super()._wait_provider(p, scope, analysis_id, call)
        input_hash = digest({
            "messages": call["messages"], "schema": call["schema"]
        })
        self._replies[call["call_id"]] = {"reply": reply, "input_hash": input_hash}
        try:
            self.grounding_store.capture(
                p, scope, analysis_id, call["call_id"], reply, input_hash,
                self._pending_catalogs[analysis_id], reauthorize=self._reauthorize,
            )
        except DomainError:
            # A revoked source cannot publish raw content. Let the sealed engine
            # still record confirmed provider usage independently of publication.
            try:
                self._get(p, scope, analysis_id)
            except DomainError:
                return reply
            raise
        return reply

    async def _call(self, p, scope, analysis_id, lease, active, phase, bundle, **kwargs):
        self._pending_catalogs[analysis_id] = self.context.catalog(bundle)
        try:
            return await super()._call(p, scope, analysis_id, lease, active, phase, bundle, **kwargs)
        finally:
            # Persist the original response even when the sealed engine rejects it.
            # Reauthorization failure fails closed; it cannot publish revoked data.
            current = self._get(p, scope, analysis_id)
            for call in current["calls"]:
                saved = self._replies.get(call["id"])
                if saved is None:
                    continue
                reply = saved["reply"]
                raw = reply.get("content_utf8")
                if not isinstance(raw, str):
                    raw = reply.get("content")
                catalog = self.context.catalog(bundle)
                checked = semantic_check(catalog, phase, raw)
                provider_valid = (
                    reply.get("status") == "SUCCEEDED" and reply.get("finish_reason") == "stop"
                    and call["state"] == "VALIDATED"
                )
                if reply.get("status") != "SUCCEEDED" or reply.get("finish_reason") != "stop":
                    checked["semantic_valid"] = None
                    checked["codes"] = ["PROVIDER_OUTPUT_UNAVAILABLE"]
                elif call["state"] != "VALIDATED" and checked["semantic_valid"]:
                    checked["semantic_valid"] = False
                    checked["codes"] = ["CONTROL_PLANE_VALIDATION_REJECTED"]
                rendered = None
                if (
                    phase in {"report", "summary"} and provider_valid and checked["semantic_valid"]
                    and current["state"] != "CANCELLED" and not current.get("cancel_requested_at")
                ):
                    rendered = render(catalog, call["output_json"])
                provenance = {key: reply.get(key) for key in (
                    "transport", "actual_model_revision", "finish_reason", "status", "dispatched",
                    "completion_confirmed"
                )}
                provenance["input_hash"] = saved["input_hash"]
                self.grounding_store.append(p, scope, analysis_id, call["id"], {
                    "raw_reply": raw,
                    "structure_valid": checked["structural_valid"],
                    "semantic_valid": checked["semantic_valid"],
                    "codes": checked["codes"],
                    "claim_catalog": catalog,
                    "rendered_report": rendered,
                    "provider_provenance": provenance,
                }, reauthorize=self._reauthorize)
                del self._replies[call["id"]]
