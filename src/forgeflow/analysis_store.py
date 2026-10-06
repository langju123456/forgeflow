"""Scoped, fenced S2 analysis persistence, independent of Gate and business actions.

The synchronous ``reauthorize(session, principal, scope, material)`` callback is a
trusted control-plane hook. It must validate live source ACL and export authority
using the supplied transaction, and raise DomainError on rejection. Provider
requests run only after reserve_call commits; no network operation belongs here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import JSON, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint, case, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from forgeflow.contracts import DomainError, Principal, Scope, authorize, canonical, digest
from forgeflow.store import SCOPE, Base, Run, Scoped, Store, aware, filters, fk, uid, values, view

Reauthorize = Callable[[Session, Principal, Scope, dict], None]
HEX = re.compile(r"^[0-9a-f]{64}$")
KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PHASE_ROLES = {"PLAN": "supervisor", "CONTEXT_PROPOSAL": "pm", "FINAL_REPORT": "pm", "SUMMARY": "supervisor"}
TERMINAL = {"COMPLETE", "FAILED", "INCOMPLETE", "CANCELLED", "OUTCOME_UNKNOWN"}


class AnalysisRun(Scoped, Base):
    __tablename__ = "analysis_runs"
    __table_args__ = (
        fk("runs", local="run_id"),
        UniqueConstraint(*SCOPE, "created_by", "idempotency_key"),
        UniqueConstraint(*SCOPE, "id", "run_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    material_hash: Mapped[str] = mapped_column(String(64))
    material_json: Mapped[dict] = mapped_column(JSON)
    parent_request_hash: Mapped[str] = mapped_column(String(64))
    parent_snapshot_hash: Mapped[str] = mapped_column(String(64))
    classification: Mapped[int] = mapped_column(Integer)
    profile_id: Mapped[str] = mapped_column(String(128))
    profile_revision: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[object] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_expires_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    calls_reserved: Mapped[int] = mapped_column(Integer, default=0)
    tokens_reserved: Mapped[int] = mapped_column(Integer, default=0)
    summary_json: Mapped[dict | None] = mapped_column(JSON)
    summary_hash: Mapped[str | None] = mapped_column(String(64))


class AnalysisCall(Scoped, Base):
    __tablename__ = "analysis_calls"
    __table_args__ = (
        ForeignKeyConstraint(
            SCOPE + ["analysis_id", "run_id"], [f"analysis_runs.{v}" for v in SCOPE + ["id", "run_id"]]
        ),
        fk("runs", local="run_id"),
        UniqueConstraint(*SCOPE, "id", "analysis_id"),
        UniqueConstraint(*SCOPE, "analysis_id", "step_key"),
        UniqueConstraint(*SCOPE, "analysis_id", "role", "phase"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36))
    run_id: Mapped[str] = mapped_column(String(36))
    step_key: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(32))
    phase: Mapped[str] = mapped_column(String(32))
    profile_id: Mapped[str] = mapped_column(String(128))
    profile_revision: Mapped[str] = mapped_column(String(128))
    input_hash: Mapped[str] = mapped_column(String(64))
    context_hash: Mapped[str] = mapped_column(String(64))
    material_hash: Mapped[str] = mapped_column(String(64))
    material_json: Mapped[dict] = mapped_column(JSON)
    intent_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="DISPATCHING")
    delivery_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    usage_state: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    usage_json: Mapped[dict] = mapped_column(JSON)
    provider_metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    input_token_limit: Mapped[int] = mapped_column(Integer)
    output_token_limit: Mapped[int] = mapped_column(Integer)
    tokens_reserved: Mapped[int] = mapped_column(Integer)
    lease_token: Mapped[str] = mapped_column(String(36))
    lease_owner: Mapped[str] = mapped_column(String(128))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[object] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    output_json: Mapped[dict | None] = mapped_column(JSON)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))


class AnalysisProviderSlot(Base):
    """Global fixed-profile concurrency slot, never exposed through scoped reads."""

    __tablename__ = "analysis_provider_slots"
    profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64))
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(64))
    analysis_id: Mapped[str | None] = mapped_column(String(36))
    call_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        ForeignKeyConstraint(SCOPE + ["analysis_id"], [f"analysis_runs.{v}" for v in SCOPE + ["id"]]),
        ForeignKeyConstraint(
            SCOPE + ["call_id", "analysis_id"], [f"analysis_calls.{v}" for v in SCOPE + ["id", "analysis_id"]]
        ),
    )


class AnalysisEvent(Scoped, Base):
    __tablename__ = "analysis_events"
    __table_args__ = (
        fk("analysis_runs", local="analysis_id"),
        UniqueConstraint(*SCOPE, "analysis_id", "sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON)
    material_json: Mapped[dict | None] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True))


class AnalysisStore:
    def __init__(self, store: Store):
        self.store = store
        self.engine = store.engine

    def create_schema(self):
        # Parent schema must already exist; never rebuild or migrate sealed S1.
        Base.metadata.create_all(
            self.engine,
            tables=[
                AnalysisRun.__table__,
                AnalysisCall.__table__,
                AnalysisProviderSlot.__table__,
                AnalysisEvent.__table__,
            ],
        )

    @staticmethod
    def _material(material: dict) -> dict:
        if not isinstance(material, dict):
            raise DomainError("INVALID_ANALYSIS_MATERIAL", status=422)
        result = dict(material)
        for name in ("profile_id", "profile_revision", "policy_revision"):
            if not isinstance(result.get(name), str) or not KEY.fullmatch(result[name]):
                raise DomainError("INVALID_ANALYSIS_MATERIAL", status=422)
        if not isinstance(result.get("source_snapshot"), dict):
            raise DomainError("INVALID_ANALYSIS_MATERIAL", status=422)
        for name, default, maximum in (
            ("classification", 0, 3),
            ("max_calls", 4, 4),
            ("max_input_tokens", 3500, 3500),
            ("max_output_tokens", 800, 800),
            ("timeout_seconds", 180, 180),
        ):
            result.setdefault(name, default)
            if (
                type(result[name]) is not int
                or not (0 if name == "classification" else 1) <= result[name] <= maximum
            ):
                raise DomainError("INVALID_ANALYSIS_LIMIT", status=422)
        result.setdefault("provider_billing", "LOCAL_NO_BILLING")
        if result["provider_billing"] != "LOCAL_NO_BILLING":
            raise DomainError("PAID_PROFILE_DISABLED", status=403)
        if len(canonical(result).encode()) > 128 * 1024:
            raise DomainError("ANALYSIS_MATERIAL_LIMIT", status=422)
        return result

    @staticmethod
    def _hook(reauthorize: Reauthorize, s, p, scope, material):
        if not callable(reauthorize):
            raise DomainError("REAUTHORIZATION_REQUIRED", status=403)
        result = reauthorize(s, p, scope, material)
        if result is not None:
            raise DomainError("INVALID_AUTHORIZATION_CALLBACK", status=403)

    def _parent(self, s, p, scope, run_id, lock=True, check_acl=True):
        parent = self.store._get(s, Run, scope, run_id, lock=lock)
        if check_acl:
            self.store._run_acl(s, p, scope, parent)
        return parent

    def _load(self, s, p, scope, analysis_id, *, check_parent_acl=True):
        authorize(p, scope, {"supervisor", "pm", "worker", "submitter", "reviewer"})
        # Look up identity without locking; lock parent before child consistently.
        row = self.store._get(s, AnalysisRun, scope, analysis_id)
        authorize(p, scope, classification=row.classification)
        parent = self._parent(s, p, scope, row.run_id, check_acl=check_parent_acl)
        row = s.scalar(
            select(AnalysisRun)
            .where(*filters(AnalysisRun, scope), AnalysisRun.id == analysis_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return row, parent

    @staticmethod
    def _writer(p, row):
        if p.subject_id != row.created_by and not (p.kind == "service" and "worker" in p.roles):
            raise DomainError("NOT_FOUND", "Object unavailable", 404)

    def _authority(self, s, p, scope, row, parent, reauthorize, material=None):
        if parent.request_hash != row.parent_request_hash or parent.snapshot_hash != row.parent_snapshot_hash:
            raise DomainError("ANALYSIS_MATERIAL_CHANGED", status=409)
        self._hook(reauthorize, s, p, scope, material or row.material_json)

    def _lease(self, s, row, parent, token, epoch, owner):
        now = self.store._now(s)
        if (
            row.lease_token != token
            or row.lease_epoch != epoch
            or row.lease_owner != owner
            or row.lease_expires_at is None
            or aware(row.lease_expires_at) <= now
        ):
            raise DomainError("STALE_ANALYSIS_LEASE")
        if row.cancel_requested_at or parent.cancel_requested_at:
            raise DomainError("ANALYSIS_CANCEL_REQUESTED")
        if aware(row.deadline) <= now:
            raise DomainError("ANALYSIS_DEADLINE")
        if row.state != "RUNNING":
            raise DomainError("ANALYSIS_TERMINAL")
        return now

    def _event(self, s, p, scope, row, kind, payload, material=None):
        payload = dict(payload)
        if material is not None:
            payload["context_material_hash"] = digest(material)
        prior = s.scalar(
            select(AnalysisEvent)
            .where(*filters(AnalysisEvent, scope), AnalysisEvent.analysis_id == row.id)
            .order_by(AnalysisEvent.sequence.desc())
            .limit(1)
        )
        stamp = self.store._now(s)
        metadata = {
            "scope": values(scope),
            "analysis_id": row.id,
            "actor": p.subject_id,
            "kind": kind,
            "sequence": 1 if prior is None else prior.sequence + 1,
            "payload_hash": digest(payload),
            "prev_hash": "0" * 64 if prior is None else prior.event_hash,
            "created_at": stamp.isoformat(),
        }
        s.add(
            AnalysisEvent(
                **values(scope),
                id=uid(),
                analysis_id=row.id,
                sequence=metadata["sequence"],
                actor=p.subject_id,
                kind=kind,
                payload_json=payload,
                material_json=material,
                payload_hash=metadata["payload_hash"],
                prev_hash=metadata["prev_hash"],
                event_hash=digest(metadata),
                created_at=stamp,
            )
        )
        row.revision += 1
        s.flush()

    def _view(self, s, scope, row, *, reveal_lease=False):
        result = view(row)
        if not reveal_lease:
            result["lease_token"] = result["lease_owner"] = None
        calls = s.scalars(
            select(AnalysisCall)
            .where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            .order_by(
                case({name: index for index, name in enumerate(PHASE_ROLES)}, value=AnalysisCall.phase),
                AnalysisCall.created_at,
                AnalysisCall.id,
            )
        ).all()
        result["calls"] = []
        for item in calls:
            data = view(item)
            data.pop("lease_token", None)
            data.pop("lease_owner", None)
            result["calls"].append(data)
        return result

    def start(self, p, scope, run_id, key, material, *, reauthorize):
        authorize(p, scope, {"supervisor", "pm", "submitter"})
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)
        material = self._material(material)
        authorize(p, scope, classification=material["classification"])
        # Public parent access repeats existing S1 whole-result authorization.
        self.store.get_run(p, scope, run_id)
        with self.store._tx() as s:
            parent = self._parent(s, p, scope, run_id)
            if parent.snapshot_hash is None or parent.assessment_json is None:
                raise DomainError("TECHNICAL_EVIDENCE_REQUIRED")
            self._hook(reauthorize, s, p, scope, material)
            request_hash = digest(
                {"run_id": run_id, "material": material, "parent_snapshot": parent.snapshot_hash}
            )
            identifier, now = uid(), self.store._now(s)
            self.store._insert_ignore(
                s,
                AnalysisRun,
                {
                    **values(scope),
                    "id": identifier,
                    "run_id": run_id,
                    "created_by": p.subject_id,
                    "idempotency_key": key,
                    "material_hash": request_hash,
                    "material_json": material,
                    "parent_request_hash": parent.request_hash,
                    "parent_snapshot_hash": parent.snapshot_hash,
                    "classification": material["classification"],
                    "profile_id": material["profile_id"],
                    "profile_revision": material["profile_revision"],
                    "state": "QUEUED",
                    "revision": 0,
                    "created_at": now,
                    "deadline": now + timedelta(seconds=material["timeout_seconds"]),
                    "lease_epoch": 0,
                    "calls_reserved": 0,
                    "tokens_reserved": 0,
                },
                SCOPE + ["created_by", "idempotency_key"],
            )
            row = s.scalar(
                select(AnalysisRun)
                .where(
                    *filters(AnalysisRun, scope),
                    AnalysisRun.created_by == p.subject_id,
                    AnalysisRun.idempotency_key == key,
                )
                .with_for_update()
            )
            if row.material_hash != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT")
            if row.id == identifier:
                self._event(
                    s,
                    p,
                    scope,
                    row,
                    "ANALYSIS_CREATED",
                    {
                        "material_hash": row.material_hash,
                        "parent_snapshot_hash": parent.snapshot_hash,
                        "profile_revision": row.profile_revision,
                    },
                )
            for call in s.scalars(
                select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            ):
                self._hook(reauthorize, s, p, scope, call.material_json)
            result = self._view(s, scope, row)
            result["replayed"] = row.id != identifier
            return result

    def get(self, p, scope, analysis_id, *, reauthorize):
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._authority(s, p, scope, row, parent, reauthorize)
            for call in s.scalars(
                select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            ):
                self._hook(reauthorize, s, p, scope, call.material_json)
            return self._view(s, scope, row)

    def events(self, p, scope, analysis_id, *, reauthorize):
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._authority(s, p, scope, row, parent, reauthorize)
            for call in s.scalars(
                select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            ):
                self._hook(reauthorize, s, p, scope, call.material_json)
            items = s.scalars(
                select(AnalysisEvent)
                .where(*filters(AnalysisEvent, scope), AnalysisEvent.analysis_id == row.id)
                .order_by(AnalysisEvent.sequence)
            ).all()
            for item in items:
                if item.material_json:
                    self._hook(reauthorize, s, p, scope, item.material_json)
            return [view(item) for item in items]

    def claim(self, p, scope, analysis_id, owner, lease_seconds=180, *, reauthorize):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if (
            not isinstance(owner, str)
            or not KEY.fullmatch(owner)
            or type(lease_seconds) is not int
            or not 1 <= lease_seconds <= 180
        ):
            raise DomainError("INVALID_ANALYSIS_LEASE", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._writer(p, row)
            self._authority(s, p, scope, row, parent, reauthorize)
            now = self.store._now(s)
            if row.cancel_requested_at or parent.cancel_requested_at:
                raise DomainError("ANALYSIS_CANCEL_REQUESTED")
            if aware(row.deadline) <= now:
                raise DomainError("ANALYSIS_DEADLINE")
            if row.state in TERMINAL:
                raise DomainError("ANALYSIS_TERMINAL")
            if row.lease_token:
                raise DomainError(
                    "ANALYSIS_LEASE_BUSY"
                    if aware(row.lease_expires_at) > now
                    else "ANALYSIS_RECOVERY_REQUIRED"
                )
            row.lease_owner, row.lease_token = owner, uid()
            row.lease_epoch += 1
            row.lease_expires_at = min(now + timedelta(seconds=lease_seconds), aware(row.deadline))
            row.state = "RUNNING"
            self._event(s, p, scope, row, "ANALYSIS_CLAIMED", {"epoch": row.lease_epoch, "owner": owner})
            return {
                "analysis_id": row.id,
                "token": row.lease_token,
                "epoch": row.lease_epoch,
                "owner": owner,
                "expires_at": aware(row.lease_expires_at).isoformat(),
                "deadline": aware(row.deadline).isoformat(),
            }

    def _slot(self, s, profile_id):
        self.store._insert_ignore(s, AnalysisProviderSlot, {"profile_id": profile_id}, ["profile_id"])
        return s.scalar(
            select(AnalysisProviderSlot)
            .where(AnalysisProviderSlot.profile_id == profile_id)
            .with_for_update()
        )

    def reserve_call(
        self,
        p,
        scope,
        analysis_id,
        token,
        epoch,
        owner,
        step_key,
        role,
        phase,
        input_hash,
        context_hash,
        *,
        input_tokens=3500,
        output_tokens=800,
        timeout_seconds=60,
        material=None,
        reauthorize,
    ):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        phase = "FINAL_REPORT" if phase == "ROLE_REPORT" else phase
        if (
            not isinstance(step_key, str)
            or not KEY.fullmatch(step_key)
            or PHASE_ROLES.get(phase) != role
            or not HEX.fullmatch(input_hash or "")
            or not HEX.fullmatch(context_hash or "")
        ):
            raise DomainError("INVALID_ANALYSIS_CALL", status=422)
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 60:
            raise DomainError("INVALID_ANALYSIS_LIMIT", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._writer(p, row)
            call_material = self._material(material) if material is not None else row.material_json
            for name in (
                "profile_id",
                "profile_revision",
                "policy_revision",
                "classification",
                "max_calls",
                "max_input_tokens",
                "max_output_tokens",
                "timeout_seconds",
                "provider_billing",
            ):
                if call_material[name] != row.material_json[name]:
                    raise DomainError("ANALYSIS_MATERIAL_CHANGED")
            self._authority(s, p, scope, row, parent, reauthorize, call_material)
            for limit, maximum in (
                (input_tokens, row.material_json["max_input_tokens"]),
                (output_tokens, row.material_json["max_output_tokens"]),
            ):
                if type(limit) is not int or not 1 <= limit <= maximum:
                    raise DomainError("ANALYSIS_BUDGET_EXCEEDED")
            intent = {
                "step_key": step_key,
                "role": role,
                "phase": phase,
                "input_hash": input_hash,
                "context_hash": context_hash,
                "material_hash": digest(call_material),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "timeout_seconds": timeout_seconds,
            }
            existing = s.scalar(
                select(AnalysisCall)
                .where(
                    *filters(AnalysisCall, scope),
                    AnalysisCall.analysis_id == row.id,
                    AnalysisCall.step_key == step_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.intent_hash != digest(intent):
                    raise DomainError("ANALYSIS_CALL_CONFLICT")
                result = view(existing)
                result["dispatch_allowed"] = False
                return result
            now = self._lease(s, row, parent, token, epoch, owner)
            if row.calls_reserved >= row.material_json["max_calls"]:
                raise DomainError("ANALYSIS_BUDGET_EXCEEDED")
            duplicate_phase = s.scalar(
                select(AnalysisCall.id).where(
                    *filters(AnalysisCall, scope),
                    AnalysisCall.analysis_id == row.id,
                    AnalysisCall.role == role,
                    AnalysisCall.phase == phase,
                )
            )
            if duplicate_phase is not None:
                raise DomainError("ANALYSIS_CALL_CONFLICT")
            if phase != tuple(PHASE_ROLES)[row.calls_reserved]:
                raise DomainError("ANALYSIS_PHASE_ORDER")
            slot = self._slot(s, row.profile_id)
            if slot.call_id is not None:
                raise DomainError("PROVIDER_UNAVAILABLE", "Provider concurrency unavailable")
            now = self._lease(s, row, parent, token, epoch, owner)
            deadline = min(
                now + timedelta(seconds=timeout_seconds), aware(row.deadline), aware(row.lease_expires_at)
            )
            if call_material.get("export_grant_expires_at"):
                deadline = min(deadline, aware(call_material["export_grant_expires_at"]))
            if deadline <= now:
                raise DomainError("ANALYSIS_DEADLINE")
            call = AnalysisCall(
                **values(scope),
                id=uid(),
                analysis_id=row.id,
                run_id=row.run_id,
                step_key=step_key,
                role=role,
                phase=phase,
                profile_id=row.profile_id,
                profile_revision=row.profile_revision,
                input_hash=input_hash,
                context_hash=context_hash,
                material_hash=digest(call_material),
                material_json=call_material,
                intent_hash=digest(intent),
                state="DISPATCHING",
                delivery_status="PENDING",
                usage_state="UNKNOWN",
                usage_json={
                    "state": "UNKNOWN",
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_microusd": None,
                },
                input_token_limit=input_tokens,
                output_token_limit=output_tokens,
                tokens_reserved=input_tokens + output_tokens,
                lease_token=token,
                lease_owner=owner,
                lease_epoch=epoch,
                created_at=now,
                deadline=deadline,
            )
            s.add(call)
            s.flush()
            row.calls_reserved += 1
            row.tokens_reserved += call.tokens_reserved
            slot.tenant_id, slot.workspace_id, slot.project_id = scope.key()
            slot.analysis_id, slot.call_id = row.id, call.id
            self._event(
                s,
                p,
                scope,
                row,
                "CALL_DISPATCH_INTENT",
                {
                    "call_id": call.id,
                    "phase": phase,
                    "role": role,
                    "intent_hash": call.intent_hash,
                    "tokens_reserved": call.tokens_reserved,
                },
            )
            result = view(call)
            result["timeout_seconds"] = (deadline - now).total_seconds()
            result["dispatch_allowed"] = True
            return result

    @staticmethod
    def _usage(usage):
        if usage is None:
            return {"state": "UNKNOWN", "input_tokens": None, "output_tokens": None, "cost_microusd": None}
        if (
            not isinstance(usage, dict)
            or set(usage) - {"state", "input_tokens", "output_tokens", "cost_microusd"}
            or usage.get("state") not in {"CONFIRMED", "UNKNOWN", "NOT_DISPATCHED"}
        ):
            raise DomainError("INVALID_PROVIDER_USAGE", status=422)
        result = {"state": usage["state"]}
        for name in ("input_tokens", "output_tokens", "cost_microusd"):
            value = usage.get(name)
            if value is not None and (type(value) is not int or value < 0):
                raise DomainError("INVALID_PROVIDER_USAGE", status=422)
            result[name] = value
        if result["state"] == "CONFIRMED" and any(
            result[k] is None for k in ("input_tokens", "output_tokens")
        ):
            raise DomainError("INVALID_PROVIDER_USAGE", status=422)
        return result

    @staticmethod
    def _provider_metadata(metadata):
        if metadata is None:
            return {}
        allowed = {"actual_model_revision", "provider_request_id", "finish_reason", "transport", "timing_ms"}
        if not isinstance(metadata, dict) or set(metadata) - allowed:
            raise DomainError("INVALID_PROVIDER_METADATA", status=422)
        result = {}
        for name, value in metadata.items():
            if value is None:
                result[name] = None
            elif name == "timing_ms":
                if type(value) is not int or not 0 <= value <= 180000:
                    raise DomainError("INVALID_PROVIDER_METADATA", status=422)
                result[name] = value
            elif not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
                raise DomainError("INVALID_PROVIDER_METADATA", status=422)
            else:
                result[name] = value
        return result

    @staticmethod
    def _call_identity(call, row, token, epoch, owner):
        if (
            call.analysis_id != row.id
            or call.lease_token != token
            or call.lease_epoch != epoch
            or call.lease_owner != owner
        ):
            raise DomainError("STALE_ANALYSIS_CALL")

    def _release_slot(self, s, scope, row, call):
        slot = self._slot(s, row.profile_id)
        if (
            slot.call_id == call.id
            and slot.analysis_id == row.id
            and (slot.tenant_id, slot.workspace_id, slot.project_id) == scope.key()
        ):
            slot.tenant_id = slot.workspace_id = slot.project_id = slot.analysis_id = slot.call_id = None

    def finish_call(
        self,
        p,
        scope,
        analysis_id,
        call_id,
        token,
        epoch,
        owner,
        validated_output,
        usage,
        *,
        output_valid=True,
        provider_metadata=None,
        reauthorize,
    ):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        usage = self._usage(usage)
        provider_metadata = self._provider_metadata(provider_metadata)
        if usage["state"] == "NOT_DISPATCHED":
            raise DomainError("INVALID_PROVIDER_USAGE", status=422)
        if not isinstance(validated_output, dict) or len(canonical(validated_output).encode()) > 128 * 1024:
            raise DomainError("ANALYSIS_OUTPUT_LIMIT", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            call = self.store._get(s, AnalysisCall, scope, call_id, lock=True)
            self._call_identity(call, row, token, epoch, owner)
            if call.state != "DISPATCHING":
                raise DomainError("ANALYSIS_CALL_TERMINAL")
            # Delivery and usage are provider facts even when publication is fenced.
            call.delivery_status, call.usage_state, call.usage_json = "DELIVERED", usage["state"], usage
            call.provider_metadata_json = provider_metadata
            call.finished_at = self.store._now(s)
            self._release_slot(s, scope, row, call)
            publication_error = None
            try:
                self.store._run_acl(s, p, scope, parent)
                now = self._lease(s, row, parent, token, epoch, owner)
                if aware(call.deadline) <= now:
                    raise DomainError("ANALYSIS_CALL_DEADLINE")
                self._authority(s, p, scope, row, parent, reauthorize, call.material_json)
                now = self._lease(s, row, parent, token, epoch, owner)
                if aware(call.deadline) <= now:
                    raise DomainError("ANALYSIS_CALL_DEADLINE")
            except DomainError as error:
                publication_error = error.code
            if publication_error:
                call.state, call.error_code = "PUBLICATION_REJECTED", publication_error
                row.state = (
                    "CANCELLED" if row.cancel_requested_at or parent.cancel_requested_at else "INCOMPLETE"
                )
                self._event(
                    s,
                    p,
                    scope,
                    row,
                    "CALL_PUBLICATION_REJECTED",
                    {"call_id": call.id, "code": publication_error, "usage_state": usage["state"]},
                )
                return {
                    "id": call.id,
                    "state": call.state,
                    "delivery_status": call.delivery_status,
                    "usage_state": call.usage_state,
                    "publication_allowed": False,
                    "error_code": publication_error,
                }
            if (
                provider_metadata.get("actual_model_revision", call.profile_revision) != call.profile_revision
                or not output_valid
                or (usage["input_tokens"] is not None and usage["input_tokens"] > call.input_token_limit)
                or (usage["output_tokens"] is not None and usage["output_tokens"] > call.output_token_limit)
            ):
                call.state, call.error_code, row.state = "OUTPUT_INVALID", "OUTPUT_INVALID", "FAILED"
            else:
                call.state, call.output_json, call.output_hash = (
                    "VALIDATED",
                    validated_output,
                    digest(validated_output),
                )
            self._event(
                s,
                p,
                scope,
                row,
                "CALL_FINISHED",
                {
                    "call_id": call.id,
                    "state": call.state,
                    "output_hash": call.output_hash,
                    "usage_state": call.usage_state,
                },
            )
            result = view(call)
            result["publication_allowed"] = call.state == "VALIDATED"
            return result

    def fail_unknown(
        self,
        p,
        scope,
        analysis_id,
        call_id,
        token,
        epoch,
        owner,
        *,
        code="PROVIDER_OUTCOME_UNKNOWN",
        usage=None,
    ):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            raise DomainError("INVALID_ERROR_CODE", status=422)
        usage = self._usage(usage)
        if usage["state"] == "NOT_DISPATCHED":
            raise DomainError("INVALID_PROVIDER_USAGE", status=422)
        with self.store._tx() as s:
            row, _parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            call = self.store._get(s, AnalysisCall, scope, call_id, lock=True)
            self._call_identity(call, row, token, epoch, owner)
            if call.state != "DISPATCHING":
                return {
                    "id": call.id,
                    "state": call.state,
                    "delivery_status": call.delivery_status,
                    "usage_state": call.usage_state,
                }
            call.state, call.delivery_status, call.error_code = "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", code
            call.usage_state, call.usage_json, call.finished_at = usage["state"], usage, self.store._now(s)
            if row.state not in {"CANCELLED", "COMPLETE"}:
                row.state = "OUTCOME_UNKNOWN"
            self._event(
                s,
                p,
                scope,
                row,
                "CALL_OUTCOME_UNKNOWN",
                {"call_id": call.id, "code": code, "reservation_retained": True, "slot_retained": True},
            )
            return {
                "id": call.id,
                "state": call.state,
                "delivery_status": call.delivery_status,
                "usage_state": call.usage_state,
            }

    def record_tool(self, p, scope, analysis_id, token, epoch, owner, receipt, *, material, reauthorize):
        """Record a real read-only service round, never a model execution claim."""
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"receipts", "proposal_hash"}
            or not isinstance(receipt["proposal_hash"], str)
            or not HEX.fullmatch(receipt["proposal_hash"])
        ):
            raise DomainError("INVALID_TOOL_RECEIPT", status=422)
        items = receipt["receipts"]
        if not isinstance(items, list) or not 1 <= len(items) <= 2:
            raise DomainError("INVALID_TOOL_RECEIPT", status=422)
        material = self._material(material)
        snapshots = material["source_snapshot"].get("bundles", [material["source_snapshot"]])
        known_refs = {source.get("ref") for snapshot in snapshots for source in snapshot.get("sources", [])}
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "tool",
                "transport",
                "role",
                "query_hash",
                "top_k",
                "result_count",
                "source_refs",
            }:
                raise DomainError("INVALID_TOOL_RECEIPT", status=422)
            if (
                item["tool"] not in {"knowledge.search", "memory.search"}
                or item["transport"] != "IN_PROCESS"
                or item["role"] != "pm"
            ):
                raise DomainError("INVALID_TOOL_RECEIPT", status=422)
            if not isinstance(item["query_hash"], str) or not HEX.fullmatch(item["query_hash"]):
                raise DomainError("INVALID_TOOL_RECEIPT", status=422)
            if (
                type(item["top_k"]) is not int
                or not 1 <= item["top_k"] <= 5
                or type(item["result_count"]) is not int
                or not 0 <= item["result_count"] <= item["top_k"]
            ):
                raise DomainError("INVALID_TOOL_RECEIPT", status=422)
            refs = item["source_refs"]
            if (
                not isinstance(refs, list)
                or len(refs) != item["result_count"]
                or any(not isinstance(ref, str) or ref not in known_refs for ref in refs)
            ):
                raise DomainError("INVALID_TOOL_RECEIPT", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._writer(p, row)
            for name in (
                "profile_id",
                "profile_revision",
                "policy_revision",
                "classification",
                "max_calls",
                "max_input_tokens",
                "max_output_tokens",
                "timeout_seconds",
                "provider_billing",
            ):
                if material[name] != row.material_json[name]:
                    raise DomainError("ANALYSIS_MATERIAL_CHANGED")
            self._authority(s, p, scope, row, parent, reauthorize, material)
            self._lease(s, row, parent, token, epoch, owner)
            validated = s.scalars(
                select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            ).all()
            if len(validated) != 2 or {call.phase for call in validated if call.state == "VALIDATED"} != {
                "PLAN",
                "CONTEXT_PROPOSAL",
            }:
                raise DomainError("ANALYSIS_TOOL_PHASE")
            previous = s.scalar(
                select(AnalysisEvent.id).where(
                    *filters(AnalysisEvent, scope),
                    AnalysisEvent.analysis_id == row.id,
                    AnalysisEvent.kind == "ANALYSIS_TOOL_READ",
                )
            )
            if previous is not None:
                raise DomainError("TOOL_ROUND_ALREADY_RECORDED")
            self._event(s, p, scope, row, "ANALYSIS_TOOL_READ", receipt, material=material)
            return {"analysis_id": row.id, "kind": "ANALYSIS_TOOL_READ", "receipt_hash": digest(receipt)}

    def finish_not_dispatched(
        self, p, scope, analysis_id, call_id, token, epoch, owner, *, code="PROVIDER_PRE_DISPATCH_DENIED"
    ):
        """Trusted adapter proved no provider request was sent; never a timeout path."""
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            raise DomainError("INVALID_ERROR_CODE", status=422)
        with self.store._tx() as s:
            row, _parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            call = self.store._get(s, AnalysisCall, scope, call_id, lock=True)
            self._call_identity(call, row, token, epoch, owner)
            if call.state != "DISPATCHING":
                raise DomainError("ANALYSIS_CALL_TERMINAL")
            call.state, call.delivery_status = "NOT_DISPATCHED", "NOT_DISPATCHED"
            call.usage_state, call.usage_json = (
                "NOT_DISPATCHED",
                {
                    "state": "NOT_DISPATCHED",
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_microusd": None,
                },
            )
            call.error_code, call.finished_at = code, self.store._now(s)
            row.tokens_reserved -= call.tokens_reserved
            call.tokens_reserved = 0
            row.state = "CANCELLED" if row.cancel_requested_at else "FAILED"
            self._release_slot(s, scope, row, call)
            self._event(
                s,
                p,
                scope,
                row,
                "CALL_NOT_DISPATCHED",
                {"call_id": call.id, "code": code, "automatic_retry": False},
            )
            return {
                "id": call.id,
                "state": call.state,
                "delivery_status": call.delivery_status,
                "usage_state": call.usage_state,
            }

    def abort(self, p, scope, analysis_id, token, epoch, owner, *, code):
        """A control-plane/tool step failed; this does not rewrite validated calls."""
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", code):
            raise DomainError("INVALID_ERROR_CODE", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            if row.state in TERMINAL:
                return {"id": row.id, "state": row.state}
            if row.lease_token != token or row.lease_epoch != epoch or row.lease_owner != owner:
                raise DomainError("STALE_ANALYSIS_LEASE")
            now = self.store._now(s)
            expired = (
                row.lease_expires_at is None
                or aware(row.lease_expires_at) <= now
                or aware(row.deadline) <= now
            )
            active = s.scalars(
                select(AnalysisCall)
                .where(
                    *filters(AnalysisCall, scope),
                    AnalysisCall.analysis_id == row.id,
                    AnalysisCall.state == "DISPATCHING",
                )
                .with_for_update()
            ).all()
            for call in active:
                call.state, call.delivery_status, call.error_code = "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", code
                call.finished_at = self.store._now(s)
            if row.cancel_requested_at or parent.cancel_requested_at:
                row.state = "CANCELLED"
            else:
                row.state = "OUTCOME_UNKNOWN" if active else ("INCOMPLETE" if expired else "FAILED")
            row.lease_epoch += 1
            row.lease_token = row.lease_owner = None
            row.lease_expires_at = None
            self._event(
                s,
                p,
                scope,
                row,
                "ANALYSIS_ABORTED",
                {"code": code, "state": row.state, "automatic_provider_retry": False},
            )
            return {"id": row.id, "state": row.state}

    def complete(self, p, scope, analysis_id, token, epoch, owner, summary, *, reauthorize):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        if not isinstance(summary, dict) or len(canonical(summary).encode()) > 128 * 1024:
            raise DomainError("ANALYSIS_OUTPUT_LIMIT", status=422)
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id)
            self._writer(p, row)
            self._lease(s, row, parent, token, epoch, owner)
            self._authority(s, p, scope, row, parent, reauthorize)
            calls = s.scalars(
                select(AnalysisCall).where(*filters(AnalysisCall, scope), AnalysisCall.analysis_id == row.id)
            ).all()
            if len(calls) != 4 or {c.phase for c in calls if c.state == "VALIDATED"} != set(PHASE_ROLES):
                raise DomainError("ANALYSIS_INCOMPLETE")
            for call in calls:
                self._hook(reauthorize, s, p, scope, call.material_json)
            self._lease(s, row, parent, token, epoch, owner)
            row.summary_json, row.summary_hash = summary, digest(summary)
            report = next(c for c in calls if c.phase == "FINAL_REPORT")
            requested_status = summary.get("status", summary.get("analysis_status"))
            row.state = (
                "COMPLETE"
                if requested_status == "COMPLETE" and (report.output_json or {}).get("status") == "COMPLETE"
                else "INCOMPLETE"
            )
            self._event(
                s, p, scope, row, "ANALYSIS_FINISHED", {"state": row.state, "summary_hash": row.summary_hash}
            )
            row.lease_token = row.lease_owner = None
            row.lease_expires_at = None
            return self._view(s, scope, row)

    def cancel(self, p, scope, analysis_id):
        authorize(p, scope, {"supervisor", "pm", "submitter"})
        with self.store._tx() as s:
            row, _parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            if row.state == "COMPLETE":
                raise DomainError("ANALYSIS_TERMINAL")
            if row.cancel_requested_at is None:
                row.cancel_requested_at = self.store._now(s)
                row.state = "CANCELLED"
                self._event(s, p, scope, row, "ANALYSIS_CANCEL_REQUESTED", {"provider_stop_confirmed": False})
            return {
                "id": row.id,
                "state": row.state,
                "cancel_requested_at": aware(row.cancel_requested_at).isoformat(),
            }

    def recover(self, p, scope, analysis_id):
        authorize(p, scope, {"supervisor", "pm", "worker"})
        with self.store._tx() as s:
            row, parent = self._load(s, p, scope, analysis_id, check_parent_acl=False)
            self._writer(p, row)
            now = self.store._now(s)
            if row.lease_token and row.lease_expires_at and aware(row.lease_expires_at) > now:
                raise DomainError("ANALYSIS_LEASE_BUSY")
            calls = s.scalars(
                select(AnalysisCall)
                .where(
                    *filters(AnalysisCall, scope),
                    AnalysisCall.analysis_id == row.id,
                    AnalysisCall.state == "DISPATCHING",
                )
                .with_for_update()
            ).all()
            for call in calls:
                call.state, call.delivery_status, call.error_code = (
                    "OUTCOME_UNKNOWN",
                    "OUTCOME_UNKNOWN",
                    "WORKER_INTERRUPTED",
                )
                call.finished_at = now
                self._event(
                    s,
                    p,
                    scope,
                    row,
                    "CALL_RECOVERY_UNKNOWN",
                    {"call_id": call.id, "reservation_retained": True, "slot_retained": True},
                )
            if calls:
                row.state = "OUTCOME_UNKNOWN"
            elif row.cancel_requested_at or parent.cancel_requested_at:
                row.state = "CANCELLED"
            elif row.state not in TERMINAL:
                row.state = "INCOMPLETE" if aware(row.deadline) <= now else "QUEUED"
            row.lease_epoch += 1
            row.lease_token = row.lease_owner = None
            row.lease_expires_at = None
            self._event(
                s,
                p,
                scope,
                row,
                "ANALYSIS_RECOVERED",
                {"state": row.state, "epoch": row.lease_epoch, "automatic_provider_retry": False},
            )
            return {
                "id": row.id,
                "state": row.state,
                "lease_epoch": row.lease_epoch,
                "calls": [
                    {
                        "id": call.id,
                        "state": call.state,
                        "delivery_status": call.delivery_status,
                        "usage_state": call.usage_state,
                    }
                    for call in calls
                ],
            }
