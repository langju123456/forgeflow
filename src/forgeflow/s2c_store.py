"""S2C persistence specializes semantic checks without rewriting S2B history.

Existing tables, scoped foreign keys, raw journal, authorization, immutable
records, provider facts and fencing remain the sealed implementation. Only the
new revision's selection validator and final server projection are specialized.
"""

from __future__ import annotations

from sqlalchemy import select

from .analysis_contracts import strict_json_object
from .analysis_store import PHASE_ROLES, AnalysisCall, AnalysisStore
from .contracts import DomainError, authorize, canonical, digest
from .grounded_store import FIELDS, PHASES, GroundedStore, GroundingRecord, RawProviderReply
from .s2c_contracts import REVISION, project, render, semantic_check, server_projection
from .store import filters

STEP_KEYS = {
    "PLAN": "supervisor-plan", "CONTEXT_PROPOSAL": "pm-context",
    "FINAL_REPORT": "pm-report", "SUMMARY": "supervisor-summary",
}


class S2CStore(GroundedStore):
    def _authorized(self, session, principal, scope, analysis_id, reauthorize):
        row = super()._authorized(session, principal, scope, analysis_id, reauthorize)
        if row.material_json.get("analysis_revision") != REVISION:
            raise DomainError("S2C_REVISION_REQUIRED")
        for call in session.scalars(select(AnalysisCall).where(
            *filters(AnalysisCall, scope), AnalysisCall.analysis_id == analysis_id,
        )):
            if call.material_json.get("analysis_revision") != REVISION:
                raise DomainError("S2C_REVISION_REQUIRED")
        return row

    @staticmethod
    def _call_record(call, record):
        """Same physical guards as S2B; statusless semantic/render specialization."""
        if call.material_json.get("analysis_revision") != REVISION:
            raise DomainError("S2C_REVISION_REQUIRED")
        if call.state == "DISPATCHING":
            raise DomainError("GROUNDING_CALL_UNFINISHED")
        catalog = record["claim_catalog"]
        if (
            call.material_json.get("claim_catalog_hash") != digest(catalog)
            or call.material_json.get("schema_catalog") != catalog
        ):
            raise DomainError("GROUNDING_CATALOG_MISMATCH")
        provenance = record["provider_provenance"]
        if provenance.get("input_hash", call.input_hash) != call.input_hash:
            raise DomainError("GROUNDING_INPUT_MISMATCH")
        metadata = call.provider_metadata_json or {}
        for name in ("transport", "actual_model_revision", "finish_reason"):
            if name in metadata and provenance.get(name) != metadata[name]:
                raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        if call.state == "OUTCOME_UNKNOWN" and provenance.get("completion_confirmed") is True:
            raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        if call.state == "NOT_DISPATCHED" and provenance.get("dispatched") is not False:
            raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        verdict = semantic_check(catalog, PHASES[call.phase], record["raw_reply"])
        if verdict["structural_valid"] != record["structure_valid"]:
            raise DomainError("GROUNDING_VERDICT_MISMATCH")
        if record["semantic_valid"] is True:
            if call.state != "VALIDATED" or not record["structure_valid"]:
                raise DomainError("GROUNDING_PUBLICATION_REJECTED")
            if (
                provenance.get("status") != "SUCCEEDED"
                or provenance.get("finish_reason") != "stop"
                or provenance.get("dispatched") is not True
                or provenance.get("completion_confirmed") is not True
                or not {"transport", "actual_model_revision", "finish_reason"} <= set(metadata)
            ):
                raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
            if strict_json_object(record["raw_reply"]) != call.output_json:
                raise DomainError("GROUNDING_OUTPUT_MISMATCH")
            if verdict["semantic_valid"] is not True:
                raise DomainError("GROUNDING_VERDICT_MISMATCH")
        if record["rendered_report"] is not None:
            if record["semantic_valid"] is not True or call.phase not in {"FINAL_REPORT", "SUMMARY"}:
                raise DomainError("GROUNDING_PUBLICATION_REJECTED")
            if record["rendered_report"] != render(catalog, call.output_json):
                raise DomainError("GROUNDING_RENDER_MISMATCH")


class S2CAnalysisStore(AnalysisStore):
    @staticmethod
    def _material(material):
        result = AnalysisStore._material(material)
        if result.get("analysis_revision") != REVISION:
            raise DomainError("S2C_REVISION_REQUIRED")
        return result

    def complete(self, p, scope, analysis_id, token, epoch, owner, summary, *, reauthorize):
        """Finish from reauthorized catalogs and actual replies, never model status."""
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if type(summary) is not dict or len(canonical(summary).encode()) > 128 * 1024:
            raise DomainError("ANALYSIS_OUTPUT_LIMIT", status=422)
        ledger = S2CStore(self)
        with self.store._tx() as session:
            row, parent = self._load(session, p, scope, analysis_id)
            self._writer(p, row)
            self._lease(session, row, parent, token, epoch, owner)
            self._authority(session, p, scope, row, parent, reauthorize)
            if row.material_json.get("analysis_revision") != REVISION:
                raise DomainError("S2C_REVISION_REQUIRED")
            calls = session.scalars(select(AnalysisCall).where(
                *filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id,
            ).with_for_update()).all()
            if (
                len(calls) != 4 or {call.phase for call in calls} != set(PHASE_ROLES)
                or any(call.state != "VALIDATED" or call.role != PHASE_ROLES[call.phase]
                       or call.step_key != STEP_KEYS[call.phase] for call in calls)
            ):
                raise DomainError("ANALYSIS_INCOMPLETE")
            by_phase, catalogs = {}, {}
            for call in calls:
                self._hook(reauthorize, session, p, scope, call.material_json)
                if call.output_hash != digest(call.output_json):
                    raise DomainError("GROUNDING_OUTPUT_MISMATCH")
                graded = session.scalar(select(GroundingRecord).where(
                    *filters(GroundingRecord, scope), GroundingRecord.analysis_id == row.id,
                    GroundingRecord.call_id == call.id,
                ))
                raw = session.scalar(select(RawProviderReply).where(
                    *filters(RawProviderReply, scope), RawProviderReply.analysis_id == row.id,
                    RawProviderReply.call_id == call.id,
                ))
                if graded is None or raw is None:
                    raise DomainError("S2C_GROUNDING_EVIDENCE_REQUIRED")
                data = ledger._view(scope, graded, session)
                record = {name: data[name] for name in FIELDS}
                ledger._raw_view(scope, raw)
                ledger._journal_match(raw, record)
                ledger._call_record(call, record)
                if record["structure_valid"] is not True or record["semantic_valid"] is not True:
                    raise DomainError("S2C_GROUNDING_EVIDENCE_REQUIRED")
                raw_usage = self._usage(raw.provider_reply.get("usage"))
                if (
                    raw.input_hash != call.input_hash or call.usage_state != "CONFIRMED"
                    or raw_usage["state"] != "CONFIRMED" or raw_usage != call.usage_json
                    or raw_usage["input_tokens"] > call.input_token_limit
                    or raw_usage["output_tokens"] > call.output_token_limit
                ):
                    raise DomainError("S2C_PROVIDER_EVIDENCE_REQUIRED")
                if call.phase in {"FINAL_REPORT", "SUMMARY"} and record["rendered_report"] is None:
                    raise DomainError("S2C_GROUNDING_EVIDENCE_REQUIRED")
                by_phase[call.phase] = call
                catalogs[call.phase] = record["claim_catalog"]
            report_catalog = catalogs["FINAL_REPORT"]
            if digest(report_catalog) != digest(catalogs["SUMMARY"]):
                raise DomainError("S2C_HANDOFF_CATALOG_MISMATCH")
            actual_summary = by_phase["SUMMARY"].output_json
            expected = project("summary", actual_summary, report_catalog).model_dump(mode="json")
            if summary != expected:
                raise DomainError("S2C_PROJECTION_MISMATCH")
            self._lease(session, row, parent, token, epoch, owner)
            projection = server_projection(report_catalog)
            persisted = {
                "selection": actual_summary,
                "server_projection": projection,
                "rendered_report": render(report_catalog, actual_summary),
            }
            row.state = projection["status"]
            row.summary_json, row.summary_hash = persisted, digest(persisted)
            self._event(session, p, scope, row, "ANALYSIS_FINISHED", {
                "state": row.state, "summary_hash": row.summary_hash,
                "status_authority": projection["status_authority"], "catalog_hash": projection["catalog_hash"],
            })
            row.lease_token = row.lease_owner = row.lease_expires_at = None
            return self._view(session, scope, row)
