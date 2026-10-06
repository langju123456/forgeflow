"""Evidence selection with server-owned technical state over the durable engine.

Provider replies are never rewritten. Calls retain only the model's selection;
an internal typed projection lets the existing orchestration share evidence.
The new analysis store separately persists the deterministic state derivation.
"""

from __future__ import annotations

import copy
from contextvars import ContextVar

from .analysis_workflow import ROLE_TOOLS, AnalysisWorkflow
from .contracts import DomainError, canonical, digest
from .grounded_workflow import GroundedWorkflow
from .s2c_contracts import REVISION, project, render, schema_for_catalog, semantic_check

PROMPT_REVISION = "s2c-evidence-selection-v3"
SKILLS = {role: f"{role}-evidence" for role in ROLE_TOOLS}
PHASES = ("plan", "context", "report", "summary")
CALL_PHASES = {"PLAN": "plan", "CONTEXT_PROPOSAL": "context", "FINAL_REPORT": "report", "SUMMARY": "summary"}
_CATALOG: ContextVar[tuple[int, dict] | None] = ContextVar("forgeflow_s2c_catalog", default=None)


class S2CWorkflow(GroundedWorkflow):
    def _activate(self):
        active = {role: self.skills.load(name, role) for role, name in SKILLS.items()}
        for role, skill in active.items():
            required = {"model.generate"} | ({"analysis.delegate:pm"} if role == "supervisor" else set())
            if not required <= set(skill.allowed_tools) or not set(skill.allowed_tools) <= ROLE_TOOLS[role]:
                raise DomainError("ANALYSIS_SKILL_DENIED", status=403)
        return active

    def _schema(self, phase):
        active = _CATALOG.get()
        if active is None or active[0] != id(self):
            raise DomainError("GROUNDING_SCHEMA_CONTEXT_REQUIRED")
        return schema_for_catalog(phase, active[1])

    def _material(self, active, *bundles):
        if not bundles:
            raise DomainError("GROUNDING_CONTEXT_REQUIRED")
        catalog = self.context.catalog(bundles[-1])
        token = _CATALOG.set((id(self), copy.deepcopy(catalog)))
        try:
            material = AnalysisWorkflow._material(self, active, *bundles)
        finally:
            _CATALOG.reset(token)
        material.update(
            analysis_revision=REVISION,
            prompt_revision=PROMPT_REVISION,
            schema_catalog=copy.deepcopy(catalog),
            claim_catalog_hash=digest(catalog),
            status_authority="SERVER_DERIVED_AUTHORIZED_CATALOG",
            output_schema_hash=digest({phase: schema_for_catalog(phase, catalog) for phase in PHASES}),
        )
        return material

    def _reauthorize(self, session, principal, scope, material):
        if (
            material.get("analysis_revision") != REVISION
            or material.get("prompt_revision") != PROMPT_REVISION
            or material.get("status_authority") != "SERVER_DERIVED_AUTHORIZED_CATALOG"
        ):
            raise DomainError("GROUNDING_REVISION_CHANGED")
        bindings = material.get("skill_bindings", [])
        if (
            not isinstance(bindings, list) or len(bindings) != 2
            or {binding.get("role") for binding in bindings} != set(SKILLS)
        ):
            raise DomainError("ANALYSIS_SKILL_CHANGED")
        active = self._activate()
        for binding in bindings:
            role = binding["role"]
            if binding.get("name") != SKILLS[role] or binding.get("sha256") != active[role].sha256:
                raise DomainError("ANALYSIS_SKILL_CHANGED")
        catalog = material.get("schema_catalog")
        if type(catalog) is not dict or set(catalog) != {"observations", "concerns"}:
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        if digest(catalog) != material.get("claim_catalog_hash"):
            raise DomainError("GROUNDING_CATALOG_CHANGED")
        try:
            expected = digest({phase: schema_for_catalog(phase, catalog) for phase in PHASES})
        except (KeyError, TypeError) as error:
            raise DomainError("GROUNDING_CATALOG_CHANGED") from error
        if material.get("output_schema_hash") != expected:
            raise DomainError("GROUNDING_REVISION_CHANGED")
        self.context.reauthorize_in_session(session, principal, scope, material)

    def _messages(self, phase, bundle, skill, task=None):
        catalog = self.context.catalog(bundle)
        sources = bundle.model_context["sources"]
        hardware = next(source for source in sources if source["kind"] == "hardware_check")
        material = {
            "source_ref": hardware["ref"],
            "observations_columns": ["id", "metric", "value", "check_state", "evidence_state"],
            "observations": [[item[key] for key in (
                "id", "metric", "value", "check_state", "evidence_state"
            )] for item in catalog["observations"]],
            "concerns_columns": ["id", "kind"],
            "concerns": [[item[key] for key in ("id", "kind")] for item in catalog["concerns"]],
        }
        untrusted = [source for source in sources if source.get("kind") == "untrusted_document"]
        if untrusted:
            material["untrusted_documents"] = (
                [{"ref": source["ref"], "kind": source["kind"], "status": source["status"]}
                 for source in untrusted] if phase == "summary" else untrusted
            )
        if phase in {"plan", "context"}:
            material.pop("observations_columns")
            material["observations"] = [[item["metric"], item["check_state"], item["evidence_state"]]
                                         for item in catalog["observations"]]
        if phase == "summary":
            report = copy.deepcopy(bundle.model_context["reports"][0])
            report.pop("status", None)
            material["pm_selection"] = report
        instructions = {
            "plan": "Delegate to pm. task_question must be exactly: Review all five EVT checks and human approval. Cite source_ref.",
            "context": "Propose zero to two useful read-only searches; prefer top_k=1.",
            "report": "Select all observation ids and all concern ids exactly once. recommendation HUMAN_REVIEW_REQUIRED.",
            "summary": "Preserve PM observation ids and concern ids. report_refs [report_pm]. human_approval_required true.",
        }[phase]
        return [
            {"role": "system", "content": (
                self.context.frozen_prompt + " " + self._instructions(skill, phase) + " " + instructions
                + " Select evidence only; the server derives technical status. Do not emit status or grant actions. JSON only."
            )},
            {"role": "user", "content": canonical(material)},
        ]

    async def _call(self, p, scope, analysis_id, lease, active, phase, bundle, **kwargs):
        catalog = self.context.catalog(bundle)
        self._pending_catalogs[analysis_id] = catalog
        token = _CATALOG.set((id(self), copy.deepcopy(catalog)))
        try:
            # The sealed engine persists the exact validated selection, before
            # we construct the server-only projection used by orchestration.
            output = await AnalysisWorkflow._call(self, p, scope, analysis_id, lease, active, phase, bundle, **kwargs)
            if phase in {"report", "summary"}:
                selection = output.model_dump(mode="json") if hasattr(output, "model_dump") else output
                return project(phase, selection, catalog)
            return output
        finally:
            _CATALOG.reset(token)
            current = self._get(p, scope, analysis_id)
            for call in current["calls"]:
                saved = self._replies.get(call["id"])
                if saved is None:
                    continue
                call_phase = CALL_PHASES[call["phase"]]
                reply = saved["reply"]
                raw = reply.get("content_utf8")
                if not isinstance(raw, str):
                    raw = reply.get("content")
                checked = semantic_check(catalog, call_phase, raw)
                provider_valid = (reply.get("status") == "SUCCEEDED"
                                  and reply.get("finish_reason") == "stop" and call["state"] == "VALIDATED")
                if reply.get("status") != "SUCCEEDED" or reply.get("finish_reason") != "stop":
                    checked["semantic_valid"] = None
                    checked["codes"] = ["PROVIDER_OUTPUT_UNAVAILABLE"]
                elif call["state"] != "VALIDATED" and checked["semantic_valid"]:
                    checked["semantic_valid"] = False
                    checked["codes"] = ["CONTROL_PLANE_VALIDATION_REJECTED"]
                rendered = None
                if (call_phase in {"report", "summary"} and provider_valid and checked["semantic_valid"]
                    and current["state"] != "CANCELLED" and not current.get("cancel_requested_at")):
                    rendered = render(catalog, call["output_json"])
                provenance = {key: reply.get(key) for key in (
                    "transport", "actual_model_revision", "finish_reason", "status", "dispatched", "completion_confirmed"
                )}
                provenance["input_hash"] = saved["input_hash"]
                self.grounding_store.append(p, scope, analysis_id, call["id"], {
                    "raw_reply": raw, "structure_valid": checked["structural_valid"],
                    "semantic_valid": checked["semantic_valid"], "codes": checked["codes"],
                    "claim_catalog": catalog, "rendered_report": rendered, "provider_provenance": provenance,
                }, reauthorize=self._reauthorize)
                del self._replies[call["id"]]

    def input_bound(self, phase, bundle, skill, task=None):
        material = {"messages": self._messages(phase, bundle, skill, task),
                    "schema": schema_for_catalog(phase, self.context.catalog(bundle))}
        return len(canonical(material).encode()) + 256 + 64 * len(material["messages"])
