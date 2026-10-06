"""Immutable, scoped grounding evidence beside the sealed analysis ledger.

The workflow derives the catalog under live source authorization and binds its
hash in the call material before dispatch. This ledger does not derive source
truth or grant authority. It keeps exact provider output separate from accepted,
deterministically rendered reports. Hashes detect inconsistent stored values;
they are not a signature or protection against a privileged database writer.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import JSON, Boolean, DateTime, ForeignKeyConstraint, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from .analysis_contracts import strict_json_object
from .analysis_store import AnalysisCall, AnalysisEvent, AnalysisStore, Reauthorize
from .contracts import DomainError, Principal, Scope, canonical, digest
from .grounded_contracts import render, semantic_check
from .store import SCOPE, Base, Run, Scoped, Store, filters, fk, uid, values, view

FIELDS = {
    "raw_reply", "structure_valid", "semantic_valid", "codes", "claim_catalog",
    "rendered_report", "provider_provenance",
}
PROVENANCE = {
    "transport", "actual_model_revision", "finish_reason", "status", "dispatched",
    "completion_confirmed", "input_hash",
}
REPLY_FIELDS = {
    "status", "content", "content_utf8", "model", "digest", "actual_model_revision", "finish_reason",
    "usage", "elapsed_seconds", "error_code", "dispatched", "completion_confirmed", "input_count_method",
    "input_token_bound", "transport", "provider_request_id",
}
PHASES = {"PLAN": "plan", "CONTEXT_PROPOSAL": "context", "FINAL_REPORT": "report", "SUMMARY": "summary"}
HEX = re.compile(r"^[0-9a-f]{64}$")
CODE = re.compile(r"^[A-Z0-9_]{1,64}$")


class GroundingRecord(Scoped, Base):
    __tablename__ = "grounding_records"
    __table_args__ = (
        fk("analysis_runs", local="analysis_id"),
        ForeignKeyConstraint(
            SCOPE + ["call_id", "analysis_id"],
            [f"analysis_calls.{v}" for v in SCOPE + ["id", "analysis_id"]],
        ),
        UniqueConstraint(*SCOPE, "analysis_id", "call_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36))
    call_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    structure_valid: Mapped[bool] = mapped_column(Boolean)
    semantic_valid: Mapped[bool | None] = mapped_column(Boolean)
    codes: Mapped[list] = mapped_column(JSON)
    raw_reply: Mapped[dict | str | None] = mapped_column(JSON)
    raw_reply_hash: Mapped[str] = mapped_column(String(64))
    claim_catalog: Mapped[dict] = mapped_column(JSON)
    claim_catalog_hash: Mapped[str] = mapped_column(String(64))
    rendered_report: Mapped[dict | None] = mapped_column(JSON)
    provider_provenance: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))


class RawProviderReply(Scoped, Base):
    """Provider facts committed before any finishing or grading transaction.

    A late response is historical diagnostic evidence. Capturing it never
    completes, retries, releases a provider slot or approves an analysis.
    """

    __tablename__ = "raw_provider_replies"
    __table_args__ = (
        fk("analysis_runs", local="analysis_id"),
        ForeignKeyConstraint(
            SCOPE + ["call_id", "analysis_id"],
            [f"analysis_calls.{v}" for v in SCOPE + ["id", "analysis_id"]],
        ),
        UniqueConstraint(*SCOPE, "analysis_id", "call_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36))
    call_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    input_hash: Mapped[str] = mapped_column(String(64))
    claim_catalog: Mapped[dict] = mapped_column(JSON)
    claim_catalog_hash: Mapped[str] = mapped_column(String(64))
    provider_reply: Mapped[dict] = mapped_column(JSON)
    provider_reply_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))


def _bounded_json(value, maximum: int):
    """Detach input aliases and reject non-finite/deep/non-JSON values."""
    def check(item, depth=0):
        if depth > 30:
            raise ValueError("JSON nesting limit")
        if item is None or type(item) in (str, bool, int, float):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ValueError("JSON value required")

    try:
        check(value)
        encoded = canonical(value).encode("utf-8")
        if len(encoded) > maximum:
            raise ValueError("JSON size limit")
        return json.loads(encoded)
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise DomainError("INVALID_GROUNDING_RECORD", status=422) from error


class GroundedStore:
    def __init__(self, analysis_store: AnalysisStore | Store):
        self.analysis = analysis_store if isinstance(analysis_store, AnalysisStore) else AnalysisStore(analysis_store)
        self.store = self.analysis.store
        self.engine = self.store.engine

    def create_schema(self):
        # Existing parent tables must exist. This adds only our new table.
        Base.metadata.create_all(self.engine, tables=[GroundingRecord.__table__, RawProviderReply.__table__])

    @staticmethod
    def _record(record: dict) -> dict:
        if type(record) is not dict or set(record) != FIELDS:
            raise DomainError("INVALID_GROUNDING_RECORD", status=422)
        result = _bounded_json(record, 512 * 1024)
        if (
            type(result["structure_valid"]) is not bool
            or (result["semantic_valid"] is not None and type(result["semantic_valid"]) is not bool)
            or (not result["structure_valid"] and result["semantic_valid"] is not None)
            or type(result["codes"]) is not list
            or len(result["codes"]) > 32
            or any(type(code) is not str or not CODE.fullmatch(code) for code in result["codes"])
            or type(result["claim_catalog"]) is not dict
            or type(result["raw_reply"]) not in (dict, str, type(None))
            or type(result["rendered_report"]) not in (dict, type(None))
            or type(result["provider_provenance"]) is not dict
            or not set(result["provider_provenance"]) <= PROVENANCE
        ):
            raise DomainError("INVALID_GROUNDING_RECORD", status=422)
        _bounded_json(result["raw_reply"], 128 * 1024)
        _bounded_json(result["claim_catalog"], 128 * 1024)
        _bounded_json(result["rendered_report"], 64 * 1024)
        provenance = result["provider_provenance"]
        for name in ("dispatched", "completion_confirmed"):
            if name in provenance and provenance[name] is not None and type(provenance[name]) is not bool:
                raise DomainError("INVALID_GROUNDING_PROVENANCE", status=422)
        for name in ("input_hash", "actual_model_revision"):
            if name in provenance and provenance[name] is not None and (
                type(provenance[name]) is not str or not HEX.fullmatch(provenance[name])
            ):
                raise DomainError("INVALID_GROUNDING_PROVENANCE", status=422)
        for name in ("transport", "finish_reason", "status"):
            if name in provenance and provenance[name] is not None and (
                type(provenance[name]) is not str or not 1 <= len(provenance[name]) <= 128
            ):
                raise DomainError("INVALID_GROUNDING_PROVENANCE", status=422)
        return result

    def _authorized(self, session, principal, scope, analysis_id, reauthorize):
        row, parent = self.analysis._load(session, principal, scope, analysis_id)
        self.analysis._authority(session, principal, scope, row, parent, reauthorize)
        for call in session.scalars(
            select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
        ):
            self.analysis._hook(reauthorize, session, principal, scope, call.material_json)
        for event in session.scalars(
            select(AnalysisEvent).where(*filters(AnalysisEvent, scope), AnalysisEvent.analysis_id == row.id)
        ):
            if event.material_json:
                self.analysis._hook(reauthorize, session, principal, scope, event.material_json)
        return row

    @staticmethod
    def _binding(scope, analysis_id, call_id, record):
        return {"scope": values(scope), "analysis_id": analysis_id, "call_id": call_id, "record": record}

    @staticmethod
    def _call_record(call, record):
        if call.state == "DISPATCHING":
            raise DomainError("GROUNDING_CALL_UNFINISHED")
        catalog_hash = digest(record["claim_catalog"])
        if call.material_json.get("claim_catalog_hash") != catalog_hash:
            raise DomainError("GROUNDING_CATALOG_MISMATCH")
        provenance = record["provider_provenance"]
        if provenance.get("input_hash", call.input_hash) != call.input_hash:
            raise DomainError("GROUNDING_INPUT_MISMATCH")
        # These facts were already committed by finish_call. A new side record
        # cannot relabel a fault driver as a real local model invocation.
        metadata = call.provider_metadata_json or {}
        for name in ("transport", "actual_model_revision", "finish_reason"):
            if name in metadata and provenance.get(name) != metadata[name]:
                raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        if call.state == "OUTCOME_UNKNOWN" and provenance.get("completion_confirmed") is True:
            raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        if call.state == "NOT_DISPATCHED" and provenance.get("dispatched") is not False:
            raise DomainError("GROUNDING_PROVENANCE_MISMATCH")
        verdict = semantic_check(record["claim_catalog"], PHASES[call.phase], record["raw_reply"])
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
            output = strict_json_object(record["raw_reply"])
            if output != call.output_json:
                raise DomainError("GROUNDING_OUTPUT_MISMATCH")
            if not verdict["structural_valid"] or verdict["semantic_valid"] is not True:
                raise DomainError("GROUNDING_VERDICT_MISMATCH")
        if record["rendered_report"] is not None:
            if record["semantic_valid"] is not True or call.phase not in {"FINAL_REPORT", "SUMMARY"}:
                raise DomainError("GROUNDING_PUBLICATION_REJECTED")
            if record["rendered_report"] != render(record["claim_catalog"], call.output_json):
                raise DomainError("GROUNDING_RENDER_MISMATCH")

    @staticmethod
    def _journal_binding(scope, analysis_id, call_id, reply, input_hash, catalog):
        return {
            "scope": values(scope), "analysis_id": analysis_id, "call_id": call_id,
            "provider_reply": reply, "input_hash": input_hash, "claim_catalog": catalog,
        }

    def _raw_view(self, scope, row):
        data = view(row)
        if (
            row.claim_catalog_hash != digest(row.claim_catalog)
            or row.provider_reply_hash != digest(row.provider_reply)
            or row.payload_hash != digest(self._journal_binding(
                scope, row.analysis_id, row.call_id, row.provider_reply, row.input_hash, row.claim_catalog,
            ))
        ):
            raise DomainError("RAW_PROVIDER_REPLY_TAMPERED")
        return data

    @staticmethod
    def _journal_match(journal, record):
        reply = journal.provider_reply
        original = reply.get("content_utf8")
        if not isinstance(original, str):
            original = reply.get("content")
        if record["raw_reply"] != original or digest(record["claim_catalog"]) != journal.claim_catalog_hash:
            raise DomainError("GROUNDING_RAW_JOURNAL_MISMATCH")
        for key, value in record["provider_provenance"].items():
            expected = journal.input_hash if key == "input_hash" else reply.get(key)
            if value != expected:
                raise DomainError("GROUNDING_RAW_JOURNAL_MISMATCH")

    def _view(self, scope, row, session):
        data = view(row)
        record = {name: data[name] for name in FIELDS}
        if (
            data["claim_catalog_hash"] != digest(record["claim_catalog"])
            or data["raw_reply_hash"] != digest(record["raw_reply"])
            or data["payload_hash"] != digest(self._binding(scope, row.analysis_id, row.call_id, record))
        ):
            raise DomainError("GROUNDING_RECORD_TAMPERED")
        journal = session.scalar(select(RawProviderReply).where(
            *filters(RawProviderReply, scope), RawProviderReply.analysis_id == row.analysis_id,
            RawProviderReply.call_id == row.call_id,
        ))
        data["raw_journal_hash"] = None
        if journal is not None:
            self._raw_view(scope, journal)
            self._journal_match(journal, record)
            data["raw_journal_hash"] = journal.payload_hash
        return data

    def capture(self, principal, scope, analysis_id, call_id, reply, input_hash, catalog, *, reauthorize):
        """Commit exact adapter response before finish_call; no grades or effects."""
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        if (
            type(reply) is not dict or not set(reply) <= REPLY_FIELDS
            or type(catalog) is not dict or type(input_hash) is not str or not HEX.fullmatch(input_hash)
            or type(reply.get("status")) is not str
            or reply.get("status") not in {
                "SUCCEEDED", "OUTPUT_INVALID", "DENIED", "PROVIDER_ERROR", "OUTCOME_UNKNOWN",
            }
        ):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        reply = _bounded_json(reply, 256 * 1024)
        catalog = _bounded_json(catalog, 128 * 1024)
        for name in REPLY_FIELDS - {"content", "content_utf8", "usage", "elapsed_seconds", "dispatched",
                                    "completion_confirmed", "input_token_bound"}:
            if name in reply and reply[name] is not None and (
                type(reply[name]) is not str or not 1 <= len(reply[name]) <= 128
            ):
                raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        for name in ("dispatched", "completion_confirmed"):
            if name in reply and reply[name] is not None and type(reply[name]) is not bool:
                raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        if "content_utf8" in reply and type(reply["content_utf8"]) not in (str, type(None)):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        if "content" in reply and type(reply["content"]) not in (dict, str, type(None)):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        if "input_token_bound" in reply and reply["input_token_bound"] is not None and (
            type(reply["input_token_bound"]) is not int or reply["input_token_bound"] < 0
        ):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        if "elapsed_seconds" in reply and (
            type(reply["elapsed_seconds"]) not in (int, float) or not 0 <= reply["elapsed_seconds"] <= 180
        ):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        usage = reply.get("usage")
        if usage is not None and (
            type(usage) is not dict or not set(usage) <= {"state", "input_tokens", "output_tokens", "cost_microusd"}
        ):
            raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        if usage is not None:
            if "state" in usage and (
                type(usage["state"]) is not str or usage["state"] not in {"CONFIRMED", "UNKNOWN", "NOT_DISPATCHED"}
            ):
                raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
            for name in ("input_tokens", "output_tokens", "cost_microusd"):
                if name in usage and usage[name] is not None and (
                    type(usage[name]) is not int or usage[name] < 0
                ):
                    raise DomainError("INVALID_RAW_PROVIDER_REPLY", status=422)
        for name in ("content", "content_utf8"):
            if name in reply:
                _bounded_json(reply[name], 128 * 1024)
        with self.store._tx() as session:
            run = self._authorized(session, principal, scope, analysis_id, reauthorize)
            self.analysis._writer(principal, run)
            if principal.kind == "agent":
                raise DomainError("GROUNDING_AGENT_WRITE_DENIED", status=403)
            call = self.store._get(session, AnalysisCall, scope, call_id, lock=True)
            if call.analysis_id != analysis_id:
                raise DomainError("NOT_FOUND", "Object unavailable", 404)
            if call.input_hash != input_hash or call.material_json.get("claim_catalog_hash") != digest(catalog):
                raise DomainError("RAW_PROVIDER_REPLY_BINDING_MISMATCH")
            graded = session.scalar(select(GroundingRecord).where(
                *filters(GroundingRecord, scope), GroundingRecord.analysis_id == analysis_id,
                GroundingRecord.call_id == call_id,
            ))
            if graded is not None:
                self._view(scope, graded, session)
                self._journal_match(
                    RawProviderReply(provider_reply=reply, input_hash=input_hash, claim_catalog_hash=digest(catalog)),
                    {name: getattr(graded, name) for name in FIELDS},
                )
            payload_hash = digest(self._journal_binding(scope, analysis_id, call_id, reply, input_hash, catalog))
            identifier = uid()
            self.store._insert_ignore(session, RawProviderReply, {
                **values(scope), "id": identifier, "analysis_id": analysis_id, "call_id": call_id,
                "created_by": principal.subject_id, "created_at": self.store._now(session),
                "provider_reply": reply, "provider_reply_hash": digest(reply), "input_hash": input_hash,
                "claim_catalog": catalog, "claim_catalog_hash": digest(catalog), "payload_hash": payload_hash,
            }, SCOPE + ["analysis_id", "call_id"])
            row = session.scalar(select(RawProviderReply).where(
                *filters(RawProviderReply, scope), RawProviderReply.analysis_id == analysis_id,
                RawProviderReply.call_id == call_id,
            ).with_for_update().execution_options(populate_existing=True))
            if row.payload_hash != payload_hash:
                raise DomainError("RAW_PROVIDER_REPLY_CONFLICT")
            return {**self._raw_view(scope, row), "replayed": row.id != identifier}

    def raw(self, principal, scope, analysis_id, call_id, *, reauthorize):
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        with self.store._tx() as session:
            self._authorized(session, principal, scope, analysis_id, reauthorize)
            row = session.scalar(select(RawProviderReply).where(
                *filters(RawProviderReply, scope), RawProviderReply.analysis_id == analysis_id,
                RawProviderReply.call_id == call_id,
            ))
            if row is None:
                raise DomainError("NOT_FOUND", "Object unavailable", 404)
            return self._raw_view(scope, row)

    def raw_list(self, principal, scope, analysis_id, *, reauthorize):
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        with self.store._tx() as session:
            self._authorized(session, principal, scope, analysis_id, reauthorize)
            rows = session.scalars(select(RawProviderReply).where(
                *filters(RawProviderReply, scope), RawProviderReply.analysis_id == analysis_id,
            ).order_by(RawProviderReply.created_at, RawProviderReply.id)).all()
            return [self._raw_view(scope, row) for row in rows]

    def append(
        self, principal: Principal, scope: Scope, analysis_id: str, call_id: str,
        record: dict, *, reauthorize: Reauthorize,
    ) -> dict:
        # Repeated in the publication transaction so a preflight is no ACL cache.
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        record = self._record(record)
        with self.store._tx() as session:
            run = self._authorized(session, principal, scope, analysis_id, reauthorize)
            self.analysis._writer(principal, run)
            if principal.kind == "agent":
                raise DomainError("GROUNDING_AGENT_WRITE_DENIED", status=403)
            call = self.store._get(session, AnalysisCall, scope, call_id, lock=True)
            if call.analysis_id != analysis_id:
                raise DomainError("NOT_FOUND", "Object unavailable", 404)
            payload_hash = digest(self._binding(scope, analysis_id, call_id, record))
            existing = session.scalar(select(GroundingRecord).where(
                *filters(GroundingRecord, scope), GroundingRecord.analysis_id == analysis_id,
                GroundingRecord.call_id == call_id,
            ).with_for_update())
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise DomainError("GROUNDING_CONFLICT")
                # Returning an earlier immutable artifact is not a fresh render
                # publication. Current source authorization still ran above.
                return {**self._view(scope, existing, session), "replayed": True}
            parent = self.store._get(session, Run, scope, run.run_id)
            if record["rendered_report"] is not None and (
                run.cancel_requested_at or parent.cancel_requested_at or run.state == "CANCELLED"
            ):
                raise DomainError("GROUNDING_PUBLICATION_REJECTED")
            self._call_record(call, record)
            identifier = uid()
            self.store._insert_ignore(
                session, GroundingRecord,
                {
                    **values(scope), "id": identifier, "analysis_id": analysis_id, "call_id": call_id,
                    "created_by": principal.subject_id, "created_at": self.store._now(session),
                    **record, "raw_reply_hash": digest(record["raw_reply"]),
                    "claim_catalog_hash": digest(record["claim_catalog"]), "payload_hash": payload_hash,
                },
                SCOPE + ["analysis_id", "call_id"],
            )
            row = session.scalar(
                select(GroundingRecord).where(
                    *filters(GroundingRecord, scope), GroundingRecord.analysis_id == analysis_id,
                    GroundingRecord.call_id == call_id,
                ).with_for_update().execution_options(populate_existing=True)
            )
            if row.payload_hash != payload_hash:
                raise DomainError("GROUNDING_CONFLICT")
            return {**self._view(scope, row, session), "replayed": row.id != identifier}

    def get(self, principal, scope, analysis_id, call_id, *, reauthorize):
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        with self.store._tx() as session:
            self._authorized(session, principal, scope, analysis_id, reauthorize)
            row = session.scalar(select(GroundingRecord).where(
                *filters(GroundingRecord, scope), GroundingRecord.analysis_id == analysis_id,
                GroundingRecord.call_id == call_id,
            ))
            if row is None:
                raise DomainError("NOT_FOUND", "Object unavailable", 404)
            return self._view(scope, row, session)

    def list(self, principal, scope, analysis_id, *, reauthorize):
        self.analysis.get(principal, scope, analysis_id, reauthorize=reauthorize)
        with self.store._tx() as session:
            self._authorized(session, principal, scope, analysis_id, reauthorize)
            rows = session.scalars(select(GroundingRecord).where(
                *filters(GroundingRecord, scope), GroundingRecord.analysis_id == analysis_id,
            ).order_by(GroundingRecord.created_at, GroundingRecord.id)).all()
            return [self._view(scope, row, session) for row in rows]
