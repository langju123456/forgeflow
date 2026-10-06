"""Transactional, scope-bearing persistence. SQLite is a test mode, PostgreSQL the runtime.

No external effects occur here. The local tracker is deliberately a database adapter.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.schema import AddConstraint

from forgeflow.contracts import (
    DomainError,
    Principal,
    RunRequest,
    Scope,
    authorize,
    digest,
)


def uid() -> str:
    return str(uuid4())


def aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class Scoped:
    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)


SCOPE = ["tenant_id", "workspace_id", "project_id"]


def fk(table: str, column: str = "id", local: str | None = None) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(SCOPE + [local or column], [f"{table}.{v}" for v in SCOPE + [column]])


def project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(SCOPE, [f"projects.{v}" for v in SCOPE])


class Project(Scoped, Base):
    __tablename__ = "projects"
    current_stage: Mapped[str] = mapped_column(String(32), default="PROTOTYPE")
    stage_revision: Mapped[int] = mapped_column(Integer, default=0)


class Run(Scoped, Base):
    __tablename__ = "runs"
    __table_args__ = (project_fk(), UniqueConstraint(*SCOPE, "created_by", "idempotency_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(32), default="QUEUED")
    current_step: Mapped[str] = mapped_column(String(32), default="readiness")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cleanup_required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[dict | None] = mapped_column(JSON)
    assessment_json: Mapped[dict | None] = mapped_column(JSON)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))


class Step(Scoped, Base):
    __tablename__ = "steps"
    __table_args__ = (fk("runs", local="run_id"), UniqueConstraint(*SCOPE, "run_id", "step_key"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    step_key: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sandbox_handle: Mapped[str | None] = mapped_column(String(128))
    scanner_handle: Mapped[str | None] = mapped_column(String(128))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))


class Event(Scoped, Base):
    __tablename__ = "events"
    __table_args__ = (
        fk("runs", local="run_id"),
        fk("steps", local="step_id"),
        UniqueConstraint(*SCOPE, "run_id", "sequence"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    step_id: Mapped[str | None] = mapped_column(String(36))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64))
    classification: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Artifact(Scoped, Base):
    __tablename__ = "artifacts"
    __table_args__ = (fk("runs", local="run_id"), fk("steps", local="step_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    step_id: Mapped[str] = mapped_column(String(36))
    path: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    classification: Mapped[int] = mapped_column(Integer, default=0)
    provenance: Mapped[dict] = mapped_column(JSON)


class Evidence(Scoped, Base):
    __tablename__ = "evidence"
    __table_args__ = (fk("runs", local="run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    source_ref: Mapped[str] = mapped_column(String(256))
    source_version: Mapped[str] = mapped_column(String(128))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)


class Approval(Scoped, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        fk("runs", local="run_id"),
        UniqueConstraint(*SCOPE, "run_id", "kind", "proposal_hash"),
        ForeignKeyConstraint(
            SCOPE + ["consumed_by_action"],
            [f"action_intents.{v}" for v in SCOPE + ["id"]],
            name="fk_approvals_consumed_scoped_action",
            use_alter=True,
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(32))
    proposal: Mapped[dict] = mapped_column(JSON)
    proposal_hash: Mapped[str] = mapped_column(String(64))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    proposal_revision: Mapped[int] = mapped_column(Integer)
    expected_stage_revision: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(32), default="PENDING")
    reviewer: Mapped[str | None] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_by_action: Mapped[str | None] = mapped_column(String(36))


class Action(Scoped, Base):
    __tablename__ = "action_intents"
    __table_args__ = (
        fk("runs", local="run_id"),
        fk("approvals", local="approval_id"),
        UniqueConstraint(*SCOPE, "action_key"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    action_key: Mapped[str] = mapped_column(String(256))
    tool: Mapped[str] = mapped_column(String(64))
    parameters: Mapped[dict] = mapped_column(JSON)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32), default="WAITING_APPROVAL")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    dispatch_owner: Mapped[str | None] = mapped_column(String(128))
    dispatch_token: Mapped[str | None] = mapped_column(String(36))
    dispatch_epoch: Mapped[int | None] = mapped_column(Integer)
    dispatch_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Receipt(Scoped, Base):
    __tablename__ = "action_receipts"
    __table_args__ = (fk("action_intents", local="action_id"), UniqueConstraint(*SCOPE, "action_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(36))
    target: Mapped[str] = mapped_column(String(128))
    external_id: Mapped[str] = mapped_column(String(128))
    request_digest: Mapped[str] = mapped_column(String(64))
    response_digest: Mapped[str] = mapped_column(String(64))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32))


class LocalIssue(Scoped, Base):
    __tablename__ = "local_issues"
    __table_args__ = (fk("action_intents", local="action_id"), UniqueConstraint(*SCOPE, "action_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    action_id: Mapped[str] = mapped_column(String(36))
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[dict] = mapped_column(JSON)


class StageTransition(Scoped, Base):
    __tablename__ = "stage_transitions"
    __table_args__ = (
        project_fk(),
        fk("runs", local="run_id"),
        fk("action_intents", local="action_id"),
        fk("approvals", local="approval_id"),
        UniqueConstraint(*SCOPE, "expected_revision"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str] = mapped_column(String(36))
    approval_id: Mapped[str] = mapped_column(String(36))
    from_stage: Mapped[str] = mapped_column(String(32))
    to_stage: Mapped[str] = mapped_column(String(32))
    expected_revision: Mapped[int] = mapped_column(Integer)
    assessment_hash: Mapped[str] = mapped_column(String(64))


class Document(Scoped, Base):
    __tablename__ = "documents"
    __table_args__ = (project_fk(),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    current_version: Mapped[str] = mapped_column(String(128))
    roles: Mapped[list] = mapped_column(JSON)
    classification: Mapped[int] = mapped_column(Integer)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class DocumentVersion(Scoped, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        fk("documents", local="document_id"),
        UniqueConstraint(*SCOPE, "document_id", "version"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Scoped, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        fk("document_versions", local="version_id"),
        UniqueConstraint(*SCOPE, "version_id", "ordinal"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))


class Embedding(Scoped, Base):
    """Schema port only; no synthetic vectors are inserted or claimed as embeddings."""

    __tablename__ = "embeddings"
    __table_args__ = (fk("chunks", local="chunk_id"), UniqueConstraint(*SCOPE, "chunk_id", "model_revision"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(36))
    model_id: Mapped[str] = mapped_column(String(128))
    model_revision: Mapped[str] = mapped_column(String(128))
    dimension: Mapped[int] = mapped_column(Integer)
    normalization: Mapped[str] = mapped_column(String(32))
    vector: Mapped[list] = mapped_column(JSON)
    generation: Mapped[str] = mapped_column(String(128))


class Memory(Scoped, Base):
    __tablename__ = "memories"
    __table_args__ = (fk("runs", local="run_id"), UniqueConstraint(*SCOPE, "dedup_hash"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(32), default="episodic")
    subject: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    source_hash: Mapped[str] = mapped_column(String(64))
    dedup_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    classification: Mapped[int] = mapped_column(Integer)
    roles: Mapped[list] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(128))
    human_confirmed: Mapped[bool] = mapped_column(Boolean)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemorySource(Scoped, Base):
    __tablename__ = "memory_sources"
    __table_args__ = (fk("memories", local="memory_id"), fk("events", local="event_id"))
    memory_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)


def values(scope: Scope) -> dict:
    return scope.model_dump()


def filters(model, scope: Scope):
    return [getattr(model, key) == value for key, value in values(scope).items()]


def view(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = aware(value).isoformat() if isinstance(value, datetime) else value
    return result


class Store:
    def __init__(self, url: str):
        self.url = url
        self.engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {},
            pool_pre_ping=True,
            hide_parameters=True,
        )
        self._lock = threading.RLock()
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA busy_timeout=30000")

    def create_schema(self):
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(select(func.pg_advisory_xact_lock(48270201)))
            Base.metadata.create_all(connection)
            # Additive, version-specific migration for the initial development schema.
            # Never delete tables or rebuild an existing SQLite database silently.
            name = "fk_approvals_consumed_scoped_action"
            existing = inspect(connection).get_foreign_keys("approvals")
            if not any(v["name"] == name for v in existing):
                if self.engine.dialect.name != "postgresql":
                    raise DomainError(
                        "SCHEMA_MIGRATION_REQUIRED", "Existing SQLite test DB needs explicit migration"
                    )
                constraint = next(v for v in Approval.__table__.foreign_key_constraints if v.name == name)
                connection.execute(AddConstraint(constraint))

    @contextmanager
    def _tx(self):
        # BEGIN IMMEDIATE provides file-wide writer exclusion across SQLite Store instances.
        lock = self._lock if self.engine.dialect.name == "sqlite" else threading.RLock()
        with lock, self.engine.connect() as connection:
            transaction = None
            if self.engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                transaction = connection.begin()
            with Session(bind=connection, expire_on_commit=False) as session:
                try:
                    yield session
                    session.flush()
                    transaction.commit() if transaction else connection.commit()
                except BaseException:
                    transaction.rollback() if transaction else connection.rollback()
                    raise

    def _now(self, session: Session) -> datetime:
        clock = (
            func.clock_timestamp() if self.engine.dialect.name == "postgresql" else func.current_timestamp()
        )
        return aware(session.scalar(select(clock)))

    def _get(self, s, model, scope, identifier, lock=False):
        if lock and model is Run:
            # All writers acquire project before run, including submit/replay, so
            # cross-run stage CAS cannot deadlock with a concurrent completion.
            self._project(s, scope, lock=True)
        statement = select(model).where(*filters(model, scope), model.id == identifier)
        if lock:
            statement = statement.with_for_update()
        row = s.scalar(statement)
        if row is None:
            raise DomainError("NOT_FOUND", "Object unavailable", 404)
        return row

    def _project(self, s, scope, lock=False):
        statement = select(Project).where(*filters(Project, scope))
        return s.scalar(statement.with_for_update() if lock else statement)

    def _insert_ignore(self, s, model, data, columns):
        insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        s.execute(insert(model).values(**data).on_conflict_do_nothing(index_elements=columns))

    def _ensure_project(self, s, scope):
        self._insert_ignore(
            s, Project, {**values(scope), "current_stage": "PROTOTYPE", "stage_revision": 0}, SCOPE
        )
        return self._project(s, scope, lock=True)

    def ensure_project(self, scope: Scope) -> dict:
        """Trusted bootstrap-only helper; never exposed as an agent/API tool."""
        with self._tx() as s:
            return view(self._ensure_project(s, scope))

    def _event(self, s, p, scope, run, kind, payload, step=None, classification=0):
        previous = s.scalar(
            select(Event)
            .where(*filters(Event, scope), Event.run_id == run.id)
            .order_by(Event.sequence.desc())
            .limit(1)
        )
        sequence = (previous.sequence + 1) if previous else 1
        stamp = self._now(s)
        data = {
            "scope": values(scope),
            "run_id": run.id,
            "step_id": step.id if step else None,
            "sequence": sequence,
            "kind": kind,
            "actor": p.subject_id,
            "payload_hash": digest(payload),
            "prev_hash": previous.event_hash if previous else "0" * 64,
            "classification": classification,
            "created_at": stamp.isoformat(),
        }
        item = Event(
            **values(scope),
            id=uid(),
            run_id=run.id,
            step_id=data["step_id"],
            sequence=sequence,
            kind=kind,
            actor=p.subject_id,
            payload=payload,
            payload_hash=data["payload_hash"],
            prev_hash=data["prev_hash"],
            event_hash=digest(data),
            classification=classification,
            created_at=stamp,
        )
        s.add(item)
        s.flush()
        return item

    def submit(self, p: Principal, request: RunRequest, key: str) -> dict:
        authorize(p, request.scope, {"submitter", "supervisor"})
        if not key or len(key) > 128:
            raise DomainError("INVALID_IDEMPOTENCY_KEY", status=422)
        scope = request.scope
        with self._tx() as s:
            self._ensure_project(s, scope)
            identifier = uid()
            self._insert_ignore(
                s,
                Run,
                {
                    **values(scope),
                    "id": identifier,
                    "created_by": p.subject_id,
                    "idempotency_key": key,
                    "request_hash": digest(request),
                    "request_json": request.model_dump(mode="json"),
                    "state": "QUEUED",
                    "revision": 0,
                    "current_step": "readiness",
                    "cleanup_required": False,
                    "created_at": self._now(s),
                },
                SCOPE + ["created_by", "idempotency_key"],
            )
            run = s.scalar(
                select(Run)
                .where(*filters(Run, scope), Run.created_by == p.subject_id, Run.idempotency_key == key)
                .with_for_update()
            )
            if run.request_hash != digest(request):
                raise DomainError("IDEMPOTENCY_CONFLICT")
            if run.id == identifier:
                step = Step(
                    **values(scope),
                    id=uid(),
                    run_id=run.id,
                    step_key="readiness",
                    state="PENDING",
                    attempt=0,
                    fencing_epoch=0,
                )
                s.add(step)
                s.flush()
                self._event(s, p, scope, run, "RUN_SUBMITTED", {"request_hash": run.request_hash})
            return self._run_view(s, scope, run, p)

    def _run_acl(self, s, p, scope, run):
        authorize(p, scope, classification=(run.snapshot_json or {}).get("classification", 0))
        for reference in (run.snapshot_json or {}).get("documents", []):
            document = s.scalar(
                select(Document).where(*filters(Document, scope), Document.id == reference["id"])
            )
            if document is None:
                raise DomainError("NOT_FOUND", status=404)
            authorize(p, scope, set(document.roles), document.classification)

    def _run_view(self, s, scope, run, p=None):
        if p is not None:
            self._run_acl(s, p, scope, run)
        result = view(run)
        if p is not None and run.snapshot_json:
            snapshot = dict(run.snapshot_json)
            references = []
            for reference in snapshot.get("documents", []):
                document = s.scalar(
                    select(Document).where(*filters(Document, scope), Document.id == reference["id"])
                )
                # Report payloads are not a sanitization boundary. Refuse the whole report
                # when its context includes inaccessible sources, rather than leaking IDs
                # through proposals, logs, or an unrecognized nested payload shape.
                if document is None:
                    raise DomainError("NOT_FOUND", status=404)
                authorize(p, scope, set(document.roles), document.classification)
                references.append(reference)
            snapshot["documents"] = references
            result["snapshot_json"] = snapshot
        for label, model in [
            ("steps", Step),
            ("approvals", Approval),
            ("actions", Action),
            ("artifacts", Artifact),
            ("evidence", Evidence),
        ]:
            rows = s.scalars(select(model).where(*filters(model, scope), model.run_id == run.id)).all()
            result[label] = [
                view(v)
                for v in rows
                if p is None or not hasattr(v, "classification") or v.classification <= p.clearance
            ]
        if p is not None and not (p.kind == "service" and "worker" in p.roles):
            for step in result["steps"]:
                step["lease_token"] = None
                step["lease_owner"] = None
        result["project"] = view(self._project(s, scope))
        result["receipts"] = [
            view(v)
            for v in s.scalars(
                select(Receipt).where(
                    *filters(Receipt, scope), Receipt.action_id.in_([a["id"] for a in result["actions"]])
                )
            )
        ]
        return result

    def get_run(self, p: Principal, scope: Scope, identifier: str) -> dict:
        authorize(p, scope)
        with self._tx() as s:
            return self._run_view(s, scope, self._get(s, Run, scope, identifier), p)

    def events(self, p: Principal, scope: Scope, identifier: str) -> list[dict]:
        authorize(p, scope)
        with self._tx() as s:
            self._run_acl(s, p, scope, self._get(s, Run, scope, identifier))
            return [
                view(v)
                for v in s.scalars(
                    select(Event)
                    .where(
                        *filters(Event, scope),
                        Event.run_id == identifier,
                        Event.classification <= p.clearance,
                    )
                    .order_by(Event.sequence)
                )
            ]

    def claim(self, p, scope, identifier, owner, ttl=30):
        authorize(p, scope, {"worker"})
        if not 0 < ttl <= 300 or not owner:
            raise DomainError("INVALID_LEASE", status=422)
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            now = self._now(s)
            if run.cancel_requested_at:
                raise DomainError("CANCEL_REQUESTED")
            if run.cleanup_required:
                raise DomainError("CLEANUP_REQUIRED")
            if run.state in {"COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW"}:
                raise DomainError("RUN_TERMINAL")
            if run.state == "WAITING_APPROVAL":
                run.current_step = "dispatch"
            step = s.scalar(
                select(Step)
                .where(*filters(Step, scope), Step.run_id == run.id, Step.step_key == run.current_step)
                .with_for_update()
            )
            if step is None:
                step = Step(
                    **values(scope),
                    id=uid(),
                    run_id=run.id,
                    step_key=run.current_step,
                    state="PENDING",
                    attempt=0,
                    fencing_epoch=0,
                )
                s.add(step)
                s.flush()
            if step.lease_token:
                raise DomainError("LEASE_BUSY" if aware(step.lease_expires_at) > now else "RECOVERY_REQUIRED")
            step.attempt += 1
            step.fencing_epoch += 1
            step.scanner_handle = None  # New attempt resource; previous handle remains in audit events.
            step.lease_owner, step.lease_token = owner, uid()
            step.lease_expires_at, step.heartbeat_at = now + timedelta(seconds=ttl), now
            step.state = "RUNNING"
            # This identity is durable before Docker create; provider must use this exact name.
            step.sandbox_handle = (
                "ff-"
                + digest(
                    {"scope": scope.model_dump(), "run": run.id, "step": step.id, "attempt": step.attempt}
                )[:32]
            )
            run.state, run.revision = "RUNNING", run.revision + 1
            self._event(
                s,
                p,
                scope,
                run,
                "STEP_CLAIMED",
                {
                    "epoch": step.fencing_epoch,
                    "attempt": step.attempt,
                    "owner": owner,
                    "sandbox_handle": step.sandbox_handle,
                },
                step,
            )
            return {
                "run_id": run.id,
                "step_id": step.id,
                "step_key": step.step_key,
                "token": step.lease_token,
                "epoch": step.fencing_epoch,
                "attempt": step.attempt,
                "sandbox_handle": step.sandbox_handle,
                "revision": run.revision,
                "lease_expires_at": aware(step.lease_expires_at).isoformat(),
                "expires_at": aware(step.lease_expires_at).isoformat(),
            }

    def _lease(self, s, scope, run, token, epoch, allow_cancel=False):
        step = s.scalar(
            select(Step)
            .where(*filters(Step, scope), Step.run_id == run.id, Step.step_key == run.current_step)
            .with_for_update()
        )
        if (
            step is None
            or step.lease_token != token
            or step.fencing_epoch != epoch
            or step.lease_expires_at is None
            or aware(step.lease_expires_at) <= self._now(s)
        ):
            raise DomainError("STALE_LEASE")
        if run.cancel_requested_at and not allow_cancel:
            raise DomainError("CANCEL_REQUESTED")
        return step

    def heartbeat(self, p, scope, identifier, token, epoch, ttl=30):
        authorize(p, scope, {"worker"})
        if not 0 < ttl <= 300:
            raise DomainError("INVALID_LEASE", status=422)
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            now = self._now(s)
            step.heartbeat_at, step.lease_expires_at = now, now + timedelta(seconds=ttl)
            return {"expires_at": aware(step.lease_expires_at).isoformat(), "epoch": epoch}

    def set_sandbox_handle(self, p, scope, identifier, token, epoch, handle):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            if handle != step.sandbox_handle:
                raise DomainError("SANDBOX_IDENTITY_CHANGED")
            self._event(s, p, scope, run, "SANDBOX_PREPARED", {"handle": handle}, step)
            return {"sandbox_handle": handle, "epoch": epoch}

    def set_scanner_handle(self, p, scope, identifier, token, epoch, handle):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            expected = (
                "ffs-"
                + digest(
                    {
                        "scope": scope.model_dump(mode="json"),
                        "run": run.id,
                        "step": step.id,
                        "attempt": step.attempt,
                        "kind": "scanner",
                    }
                )[:32]
            )
            if handle != expected or (step.scanner_handle and step.scanner_handle != handle):
                raise DomainError("SCANNER_IDENTITY_CHANGED")
            step.scanner_handle = handle
            self._event(s, p, scope, run, "SCANNER_PREPARED", {"handle": handle}, step)
            return {"scanner_handle": handle, "epoch": epoch}

    def fail_check(self, p, scope, identifier, token, epoch, result):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            run.result_json, run.assessment_json = result, {"status": "UNKNOWN", "reason": "check failed"}
            run.state, run.revision = "FAILED", run.revision + 1
            run.cleanup_required = result.get("cleanup") == "FAILED"
            if run.cleanup_required:
                run.state = "NEEDS_REVIEW"
            step.state, step.output_hash = result.get("status", "FAILED"), digest(result)
            step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            self._event(
                s,
                p,
                scope,
                run,
                "CHECK_FAILED",
                {"result_hash": digest(result), "status": step.state, "cleanup": result.get("cleanup")},
                step,
            )
            return self._run_view(s, scope, run, p)

    def _prepare(self, s, p, scope, run, kind, parameters, project):
        proposal = {
            "scope": values(scope),
            "run_id": run.id,
            "kind": kind,
            "parameters": parameters,
            "snapshot": run.snapshot_json,
            "proposal_revision": run.revision,
            "expected_stage_revision": project.stage_revision,
        }
        approval = Approval(
            **values(scope),
            id=uid(),
            run_id=run.id,
            kind=kind,
            proposal=proposal,
            proposal_hash=digest(proposal),
            parameters_hash=digest(parameters),
            snapshot_hash=run.snapshot_hash,
            proposal_revision=run.revision,
            expected_stage_revision=project.stage_revision,
            decision="PENDING",
            expires_at=self._now(s) + timedelta(hours=1),
        )
        s.add(approval)
        s.flush()
        action = Action(
            **values(scope),
            id=uid(),
            run_id=run.id,
            action_key=f"{run.id}:{kind}",
            tool="issues.create.local" if kind == "ISSUE" else "stage.transition.local",
            parameters=parameters,
            parameters_hash=digest(parameters),
            approval_id=approval.id,
            status="WAITING_APPROVAL",
            attempt=0,
        )
        s.add(action)
        s.flush()
        self._event(
            s,
            p,
            scope,
            run,
            "APPROVAL_PREPARED",
            {"approval_id": approval.id, "action_id": action.id, "proposal_hash": approval.proposal_hash},
        )

    def complete_check(self, p, scope, identifier, token, epoch, result, assessment, snapshot):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            if step.step_key != "readiness" or run.assessment_json is not None:
                raise DomainError("CHECK_ALREADY_COMPLETED")
            if assessment.get("status") not in {"PASS", "BLOCKED", "UNKNOWN"}:
                raise DomainError("INVALID_ASSESSMENT", status=422)
            if assessment["status"] == "PASS" and (
                result.get("status") != "SUCCEEDED" or result.get("cleanup") != "REMOVED"
            ):
                raise DomainError("UNVERIFIED_PASS")
            if result.get("cleanup") == "FAILED":
                run.state, run.cleanup_required = "NEEDS_REVIEW", True
            else:
                run.state = "WAITING_APPROVAL"
            run.result_json, run.assessment_json, run.snapshot_json = result, assessment, snapshot
            run.snapshot_hash, run.revision = digest(snapshot), run.revision + 1
            step.state, step.output_hash = result.get("status", "FAILED"), digest(result)
            step.lease_owner, step.lease_token, step.lease_expires_at = None, None, None
            artifacts = result.get("artifacts", [])
            if len(artifacts) > 16 or sum(len(v["content"].encode()) for v in artifacts) > 2 * 1024 * 1024:
                raise DomainError("ARTIFACT_LIMIT")
            for artifact in artifacts:
                content_hash = hashlib.sha256(artifact["content"].encode()).hexdigest()
                size = len(artifact["content"].encode())
                path = PurePosixPath(artifact["path"])
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in artifact["path"]
                    or not path.parts
                    or artifact.get("sha256", content_hash) != content_hash
                    or artifact.get("size", size) != size
                ):
                    raise DomainError("ARTIFACT_MANIFEST_INVALID")
                s.add(
                    Artifact(
                        **values(scope),
                        id=uid(),
                        run_id=run.id,
                        step_id=step.id,
                        path=artifact["path"],
                        content=artifact["content"],
                        content_sha256=content_hash,
                        size=size,
                        classification=snapshot.get("classification", 0),
                        provenance={"input_hash": snapshot.get("input_hash"), "epoch": epoch},
                    )
                )
            for evidence in assessment.get("evidence", []):
                s.add(
                    Evidence(
                        **values(scope),
                        id=uid(),
                        run_id=run.id,
                        source_ref=str(evidence.get("source_ref", "SYNTHETIC")),
                        source_version=str(evidence.get("source_version", "1")),
                        content_hash=str(evidence.get("content_hash", digest(evidence))),
                        status=str(evidence.get("status", "UNKNOWN")),
                        valid_until=aware(evidence["valid_until"]) if evidence.get("valid_until") else None,
                        data=evidence,
                    )
                )
            s.flush()
            self._event(
                s,
                p,
                scope,
                run,
                "CHECK_COMPLETED",
                {
                    "result_hash": digest(result),
                    "assessment_hash": digest(assessment),
                    "status": assessment["status"],
                },
                step,
                classification=snapshot.get("classification", 0),
            )
            project = self._project(s, scope, lock=True)
            if not run.cleanup_required:
                self._prepare(
                    s,
                    p,
                    scope,
                    run,
                    "ISSUE",
                    {
                        "title": "Aurora EVT readiness review",
                        "gate_status": assessment["status"],
                        "assessment_hash": digest(assessment),
                    },
                    project,
                )
                if assessment["status"] == "PASS":
                    self._prepare(
                        s,
                        p,
                        scope,
                        run,
                        "STAGE",
                        {"from_stage": "PROTOTYPE", "to_stage": "EVT", "assessment_hash": digest(assessment)},
                        project,
                    )
            return self._run_view(s, scope, run, p)

    def recover(self, p, scope, identifier):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = s.scalar(
                select(Step)
                .where(*filters(Step, scope), Step.run_id == run.id, Step.step_key == run.current_step)
                .with_for_update()
            )
            if step is None or step.lease_token is None:
                raise DomainError("NO_EXPIRED_LEASE")
            if aware(step.lease_expires_at) > self._now(s):
                raise DomainError("LEASE_BUSY")
            handle = step.sandbox_handle
            step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            step.fencing_epoch += 1
            step.state = "OUTCOME_UNKNOWN"
            run.cleanup_required, run.state, run.revision = True, "NEEDS_REVIEW", run.revision + 1
            # An uncertain external dispatch is never reset to PREPARED.
            for action in s.scalars(
                select(Action).where(
                    *filters(Action, scope), Action.run_id == run.id, Action.status == "DISPATCHING"
                )
            ):
                action.status = "OUTCOME_UNKNOWN"
            self._event(
                s,
                p,
                scope,
                run,
                "LEASE_REVOKED",
                {"sandbox_handle": handle, "revoked_epoch": step.fencing_epoch - 1, "cleanup_required": True},
                step,
            )
            return {
                "run_id": run.id,
                "sandbox_handle": handle,
                "step_id": step.id,
                "scanner_handle": step.scanner_handle,
                "attempt": step.attempt,
                "epoch": step.fencing_epoch,
                "state": run.state,
                "cleanup_required": True,
            }

    def acknowledge_recovery(self, p, scope, identifier, cleanup):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            if not run.cleanup_required:
                raise DomainError("RECOVERY_NOT_PENDING")
            unknown = s.scalar(
                select(Action).where(
                    *filters(Action, scope), Action.run_id == run.id, Action.status == "OUTCOME_UNKNOWN"
                )
            )
            if cleanup not in {"REMOVED", "NOT_STARTED"} or unknown:
                self._event(
                    s,
                    p,
                    scope,
                    run,
                    "RECOVERY_BLOCKED",
                    {"cleanup": cleanup, "unknown_dispatch": bool(unknown)},
                )
                return self._run_view(s, scope, run, p)
            run.cleanup_required = False
            run.state = (
                "CANCELLED"
                if run.cancel_requested_at
                else ("WAITING_APPROVAL" if run.assessment_json else "QUEUED")
            )
            run.revision += 1
            self._event(s, p, scope, run, "RECOVERY_CLEANED", {"cleanup": cleanup, "state": run.state})
            return self._run_view(s, scope, run, p)

    def cancel(self, p, scope, identifier):
        authorize(p, scope, {"submitter", "supervisor", "reviewer", "worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            if not run.cancel_requested_at:
                run.cancel_requested_at, run.revision = self._now(s), run.revision + 1
                live = s.scalar(
                    select(Step).where(
                        *filters(Step, scope), Step.run_id == run.id, Step.lease_token.is_not(None)
                    )
                )
                if not live and run.state != "COMPLETED":
                    run.state = "CANCELLED"
                self._event(
                    s,
                    p,
                    scope,
                    run,
                    "CANCEL_REQUESTED",
                    {"state": run.state, "process_stop_acknowledged": not bool(live)},
                )
            return self._run_view(s, scope, run, p)

    def acknowledge_cancel(self, p, scope, identifier, token, epoch, cleanup):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch, allow_cancel=True)
            if not run.cancel_requested_at:
                raise DomainError("CANCEL_NOT_REQUESTED")
            step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            step.state = "CANCELLED" if cleanup in {"REMOVED", "NOT_STARTED"} else "OUTCOME_UNKNOWN"
            run.state = "CANCELLED" if step.state == "CANCELLED" else "NEEDS_REVIEW"
            run.cleanup_required = step.state != "CANCELLED"
            run.revision += 1
            self._event(
                s, p, scope, run, "CANCEL_ACKNOWLEDGED", {"cleanup": cleanup, "state": run.state}, step
            )
            return self._run_view(s, scope, run, p)

    def _material(self, run, approval, action=None):
        if (
            digest(approval.proposal) != approval.proposal_hash
            or digest(approval.proposal.get("parameters")) != approval.parameters_hash
            or digest(approval.proposal.get("snapshot")) != approval.snapshot_hash
            or digest(run.snapshot_json) != approval.snapshot_hash
            or run.snapshot_hash != approval.snapshot_hash
            or approval.proposal.get("run_id") != run.id
            or approval.run_id != run.id
            or approval.proposal.get("proposal_revision") != approval.proposal_revision
            or approval.proposal.get("expected_stage_revision") != approval.expected_stage_revision
        ):
            raise DomainError("PROPOSAL_CHANGED")
        if action is not None and (
            action.run_id != run.id
            or action.approval_id != approval.id
            or digest(action.parameters) != action.parameters_hash
            or action.parameters_hash != approval.parameters_hash
        ):
            raise DomainError("PROPOSAL_CHANGED")

    def decide(self, p, scope, identifier, proposal_hash, expected_run_revision, decision):
        authorize(p, scope, {"reviewer"})
        if p.kind != "user":
            raise DomainError("HUMAN_REVIEW_REQUIRED", status=403)
        if decision not in {"APPROVED", "REJECTED"}:
            raise DomainError("INVALID_DECISION", status=422)
        with self._tx() as s:
            # Lock run before approval to match dispatch/cancel lock order.
            approval = self._get(s, Approval, scope, identifier)
            run = self._get(s, Run, scope, approval.run_id, lock=True)
            self._run_acl(s, p, scope, run)
            approval = self._get(s, Approval, scope, identifier, lock=True)
            self._material(run, approval)
            if approval.proposal_hash != proposal_hash:
                raise DomainError("PROPOSAL_HASH_MISMATCH")
            if approval.decision == decision and approval.reviewer == p.subject_id:
                return {"approval": view(approval), "run_revision": run.revision, "replayed": True}
            if run.cancel_requested_at:
                raise DomainError("CANCEL_REQUESTED")
            if run.revision != expected_run_revision:
                raise DomainError("REVISION_CONFLICT")
            if approval.decision != "PENDING":
                raise DomainError("APPROVAL_ALREADY_DECIDED")
            if aware(approval.expires_at) <= self._now(s):
                raise DomainError("APPROVAL_EXPIRED", status=410)
            approval.decision, approval.reviewer = decision, p.subject_id
            run.revision += 1
            action = s.scalar(
                select(Action).where(*filters(Action, scope), Action.approval_id == approval.id)
            )
            action.status = "APPROVED" if decision == "APPROVED" else "REJECTED"
            self._event(
                s,
                p,
                scope,
                run,
                "APPROVAL_DECIDED",
                {"approval_id": identifier, "proposal_hash": proposal_hash, "decision": decision},
            )
            remaining = s.scalar(
                select(Action).where(
                    *filters(Action, scope),
                    Action.run_id == run.id,
                    Action.status.in_(["WAITING_APPROVAL", "APPROVED"]),
                )
            )
            if remaining is None:
                run.state = "COMPLETED"
            return {"approval": view(approval), "run_revision": run.revision, "replayed": False}

    def _fresh(self, s, p, scope, run):
        now = self._now(s)
        if run.snapshot_json.get("valid_until") and aware(run.snapshot_json["valid_until"]) <= now:
            raise DomainError("EVIDENCE_EXPIRED")
        for evidence in s.scalars(
            select(Evidence).where(*filters(Evidence, scope), Evidence.run_id == run.id)
        ):
            if evidence.valid_until and aware(evidence.valid_until) <= now:
                raise DomainError("EVIDENCE_EXPIRED")
        for reference in run.snapshot_json.get("documents", []):
            document = self._get(s, Document, scope, reference["id"], lock=True)
            authorize(p, scope, set(document.roles), document.classification)
            version = s.scalar(
                select(DocumentVersion).where(
                    *filters(DocumentVersion, scope),
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.version == reference["version"],
                )
            )
            if (
                document.deleted
                or document.current_version != reference["version"]
                or version is None
                or version.source_hash != reference["source_hash"]
            ):
                raise DomainError("SOURCE_CHANGED")
            if version.valid_until and aware(version.valid_until) <= now:
                raise DomainError("EVIDENCE_EXPIRED")

    def dispatch_local(self, p, scope, identifier, action_id, owner, *, lease_token, epoch):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            action = self._get(s, Action, scope, action_id, lock=True)
            if action.run_id != run.id:
                raise DomainError("NOT_FOUND", status=404)
            if run.cancel_requested_at:
                raise DomainError("CANCEL_REQUESTED")
            if action.status == "SUCCEEDED":
                receipt = s.scalar(
                    select(Receipt).where(*filters(Receipt, scope), Receipt.action_id == action.id)
                )
                return {"receipt": view(receipt), "replayed": True, "run_revision": run.revision}
            step = self._lease(s, scope, run, lease_token, epoch)
            if step.step_key != "dispatch" or step.lease_owner != owner or run.state != "RUNNING":
                raise DomainError("DISPATCH_LEASE_REQUIRED")
            approval = self._get(s, Approval, scope, action.approval_id, lock=True)
            self._material(run, approval, action)
            if action.status != "APPROVED" or approval.decision != "APPROVED":
                raise DomainError("APPROVAL_REQUIRED")
            if aware(approval.expires_at) <= self._now(s):
                raise DomainError("APPROVAL_EXPIRED", status=410)
            if approval.consumed_by_action is not None:
                raise DomainError("APPROVAL_CONSUMED")
            project = self._project(s, scope, lock=True)
            if approval.kind == "STAGE":
                if (
                    run.assessment_json.get("status") != "PASS"
                    or project.current_stage != "PROTOTYPE"
                    or project.stage_revision != approval.expected_stage_revision
                    or action.parameters.get("from_stage") != "PROTOTYPE"
                    or action.parameters.get("to_stage") != "EVT"
                ):
                    raise DomainError("STAGE_CONFLICT")
                self._fresh(s, p, scope, run)
            action.status, action.attempt = "DISPATCHING", action.attempt + 1
            action.dispatch_owner, action.dispatch_token, action.dispatch_epoch = owner, uid(), epoch
            action.dispatch_deadline = step.lease_expires_at
            approval.consumed_by_action = action.id
            self._event(
                s,
                p,
                scope,
                run,
                "ACTION_DISPATCH_CLAIMED",
                {"action_id": action.id, "proposal_hash": approval.proposal_hash, "epoch": epoch},
                step,
            )
            external_id = uid()
            if approval.kind == "ISSUE":
                s.add(
                    LocalIssue(
                        **values(scope),
                        id=external_id,
                        action_id=action.id,
                        title=action.parameters["title"],
                        body=action.parameters,
                    )
                )
                target = "ADAPTER_LOCAL:IssueTracker"
            else:
                s.add(
                    StageTransition(
                        **values(scope),
                        id=external_id,
                        run_id=run.id,
                        action_id=action.id,
                        approval_id=approval.id,
                        from_stage=project.current_stage,
                        to_stage="EVT",
                        expected_revision=project.stage_revision,
                        assessment_hash=digest(run.assessment_json),
                    )
                )
                project.current_stage, project.stage_revision = "EVT", project.stage_revision + 1
                target = "ADAPTER_LOCAL:ProjectStage"
            receipt = Receipt(
                **values(scope),
                id=uid(),
                action_id=action.id,
                target=target,
                external_id=external_id,
                request_digest=action.parameters_hash,
                response_digest=digest({"id": external_id, "target": target}),
                executed_at=self._now(s),
                status="SUCCEEDED",
            )
            s.add(receipt)
            action.status, run.revision = "SUCCEEDED", run.revision + 1
            s.flush()
            self._event(
                s,
                p,
                scope,
                run,
                "ACTION_SUCCEEDED",
                {
                    "action_id": action.id,
                    "receipt_id": receipt.id,
                    "target": target,
                    "external_id": external_id,
                },
                step,
            )
            pending = s.scalar(
                select(Action).where(
                    *filters(Action, scope),
                    Action.run_id == run.id,
                    Action.status.in_(["WAITING_APPROVAL", "APPROVED"]),
                )
            )
            if pending is None:
                run.state, step.state = "COMPLETED", "SUCCEEDED"
                step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            elif pending.status == "WAITING_APPROVAL" and not s.scalar(
                select(Action).where(
                    *filters(Action, scope), Action.run_id == run.id, Action.status == "APPROVED"
                )
            ):
                run.state, step.state = "WAITING_APPROVAL", "SUCCEEDED"
                step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            return {
                "receipt": view(receipt),
                "replayed": False,
                "run_revision": run.revision,
                "state": run.state,
            }

    def release_dispatch(self, p, scope, identifier, token, epoch):
        """Release an acknowledged local dispatch checkpoint between CLI invocations."""
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            run = self._get(s, Run, scope, identifier, lock=True)
            step = self._lease(s, scope, run, token, epoch)
            if step.step_key != "dispatch" or s.scalar(
                select(Action).where(
                    *filters(Action, scope),
                    Action.run_id == run.id,
                    Action.status.in_(["DISPATCHING", "OUTCOME_UNKNOWN"]),
                )
            ):
                raise DomainError("UNACKNOWLEDGED_DISPATCH")
            step.lease_token, step.lease_owner, step.lease_expires_at = None, None, None
            step.state, run.state, run.revision = "SUCCEEDED", "WAITING_APPROVAL", run.revision + 1
            self._event(s, p, scope, run, "DISPATCH_RELEASED", {"epoch": epoch, "state": run.state}, step)
            return self._run_view(s, scope, run, p)

    def put_document(self, p, scope, identifier, version, content, roles, classification, valid_until=None):
        authorize(p, scope, {"admin"}, classification)
        if p.kind != "user" or not roles or not 0 <= classification <= 3:
            raise DomainError("DOCUMENT_ADMIN_REQUIRED", status=403)
        with self._tx() as s:
            self._ensure_project(s, scope)
            document = s.scalar(
                select(Document).where(*filters(Document, scope), Document.id == identifier).with_for_update()
            )
            if document is None:
                document = Document(
                    **values(scope),
                    id=identifier,
                    current_version=version,
                    roles=sorted(roles),
                    classification=classification,
                    deleted=False,
                )
                s.add(document)
                s.flush()
            existing = s.scalar(
                select(DocumentVersion).where(
                    *filters(DocumentVersion, scope),
                    DocumentVersion.document_id == identifier,
                    DocumentVersion.version == version,
                )
            )
            source_hash = hashlib.sha256(content.encode()).hexdigest()
            validity = aware(valid_until) if valid_until else None
            if existing:
                if (
                    existing.source_hash != source_hash
                    or set(document.roles) != set(roles)
                    or document.classification != classification
                    or (aware(existing.valid_until) if existing.valid_until else None) != validity
                ):
                    raise DomainError("DOCUMENT_VERSION_IMMUTABLE")
                return {
                    **view(existing),
                    "id": identifier,
                    "citation_id": existing.id,
                    "roles": document.roles,
                    "classification": document.classification,
                }
            record = DocumentVersion(
                **values(scope),
                id=uid(),
                document_id=identifier,
                version=version,
                content=content,
                source_hash=source_hash,
                observed_at=self._now(s),
                valid_until=validity,
            )
            s.add(record)
            s.flush()
            document.current_version, document.roles, document.classification = (
                version,
                sorted(roles),
                classification,
            )
            s.add(
                Chunk(
                    **values(scope),
                    id=uid(),
                    version_id=record.id,
                    ordinal=0,
                    content=content,
                    source_hash=source_hash,
                )
            )
            return {
                **view(record),
                "id": identifier,
                "citation_id": record.id,
                "roles": document.roles,
                "classification": classification,
            }

    def document_rows(self, p, scope):
        authorize(p, scope)
        with self._tx() as s:
            # Candidate identities are authorized before querying any version content.
            documents = s.scalars(
                select(Document).where(
                    *filters(Document, scope),
                    Document.deleted.is_(False),
                    Document.classification <= p.clearance,
                )
            ).all()
            documents = [v for v in documents if p.roles & set(v.roles)]
            rows = []
            now = self._now(s)
            for document in documents:
                record = s.scalar(
                    select(DocumentVersion).where(
                        *filters(DocumentVersion, scope),
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.version == document.current_version,
                    )
                )
                rows.append(
                    {
                        **view(record),
                        "id": document.id,
                        "citation_id": record.id,
                        "roles": document.roles,
                        "classification": document.classification,
                        "status": "EXPIRED"
                        if record.valid_until and aware(record.valid_until) <= now
                        else "CURRENT",
                    }
                )
            return rows

    def memory_rows(self, p, scope):
        authorize(p, scope)
        with self._tx() as s:
            metadata = s.execute(
                select(Memory.id, Memory.roles).where(
                    *filters(Memory, scope),
                    Memory.classification <= p.clearance,
                    Memory.status.in_(["CANDIDATE", "CONFIRMED"]),
                )
            ).all()
            allowed = [identifier for identifier, roles in metadata if p.roles & set(roles)]
            permitted = []
            for identifier in allowed:
                try:
                    sources = s.execute(
                        select(Event.run_id, Event.classification)
                        .join(
                            MemorySource,
                            (MemorySource.tenant_id == Event.tenant_id)
                            & (MemorySource.workspace_id == Event.workspace_id)
                            & (MemorySource.project_id == Event.project_id)
                            & (MemorySource.event_id == Event.id),
                        )
                        .where(*filters(Event, scope), MemorySource.memory_id == identifier)
                    ).all()
                    for source_run_id, source_classification in sources:
                        self._run_acl(s, p, scope, self._get(s, Run, scope, source_run_id))
                        authorize(p, scope, classification=source_classification)
                    permitted.append(identifier)
                except DomainError:
                    continue
            rows = s.scalars(select(Memory).where(*filters(Memory, scope), Memory.id.in_(permitted))).all()
            result = []
            for memory in rows:
                if not p.roles & set(memory.roles):
                    continue
                item = view(memory)
                item["version"], item["citation_id"], item["subject_key"] = "1", memory.id, memory.subject
                item["supersedes"] = None
                item["source_event_ids"] = [
                    v.event_id
                    for v in s.scalars(
                        select(MemorySource).where(
                            *filters(MemorySource, scope), MemorySource.memory_id == memory.id
                        )
                    )
                ]
                item["sources"] = item["source_event_ids"]
                if memory.valid_until and aware(memory.valid_until) <= self._now(s):
                    item["status"] = "EXPIRED"
                result.append(item)
            return result

    def write_memory(
        self, p, scope, run_id, subject, content, source_event_ids, status="CANDIDATE", classification=0
    ):
        authorize(p, scope, {"memory_writer"}, classification)
        if status not in {"CANDIDATE", "CONFIRMED"} or not source_event_ids:
            raise DomainError("MEMORY_SOURCE_REQUIRED", status=422)
        if status == "CONFIRMED" and (p.kind != "user" or "reviewer" not in p.roles):
            raise DomainError("HUMAN_REVIEW_REQUIRED", status=403)
        with self._tx() as s:
            run = self._get(s, Run, scope, run_id, lock=True)
            self._run_acl(s, p, scope, run)
            sources = []
            for identifier in sorted(set(source_event_ids)):
                source = self._get(s, Event, scope, identifier)
                if source.run_id != run.id:
                    raise DomainError("NOT_FOUND", status=404)
                if source.classification > classification:
                    raise DomainError("CLASSIFICATION_DOWNGRADE", status=403)
                authorize(p, scope, classification=source.classification)
                sources.append(source)
            identity = digest(
                {
                    "subject": subject,
                    "content": content,
                    "sources": sorted(set(source_event_ids)),
                    "status": status,
                    "classification": classification,
                }
            )
            existing = s.scalar(select(Memory).where(*filters(Memory, scope), Memory.dedup_hash == identity))
            if existing:
                return view(existing)
            memory = Memory(
                **values(scope),
                id=uid(),
                run_id=run.id,
                type="episodic",
                subject=subject,
                content=content,
                source_hash=hashlib.sha256(content.encode()).hexdigest(),
                dedup_hash=identity,
                status=status,
                classification=classification,
                roles=sorted(
                    set.intersection(
                        *[
                            set(self._get(s, Document, scope, ref["id"]).roles)
                            for ref in (run.snapshot_json or {}).get("documents", [])
                        ]
                    )
                )
                if (run.snapshot_json or {}).get("documents")
                else ["hardware", "pm", "supervisor", "sre", "worker", "reviewer", "memory_writer"],
                created_by=p.subject_id,
                human_confirmed=status == "CONFIRMED",
                valid_from=self._now(s),
            )
            s.add(memory)
            s.flush()
            for source in sources:
                s.add(MemorySource(**values(scope), memory_id=memory.id, event_id=source.id))
            self._event(
                s,
                p,
                scope,
                run,
                "MEMORY_WRITTEN",
                {"memory_id": memory.id, "source_event_ids": sorted(set(source_event_ids)), "status": status},
                classification=classification,
            )
            return view(memory)

    def cite(self, p, scope, kind, identifier):
        authorize(p, scope)
        if kind == "document":
            with self._tx() as s:
                document_id = s.scalar(
                    select(DocumentVersion.document_id).where(
                        *filters(DocumentVersion, scope), DocumentVersion.id == identifier
                    )
                )
                if document_id is None:
                    raise DomainError("NOT_FOUND", status=404)
                document = self._get(s, Document, scope, document_id)
                authorize(p, scope, set(document.roles), document.classification)
                version = self._get(s, DocumentVersion, scope, identifier)
                status = (
                    "DELETED"
                    if document.deleted
                    else "SUPERSEDED"
                    if document.current_version != version.version
                    else (
                        "EXPIRED"
                        if version.valid_until and aware(version.valid_until) <= self._now(s)
                        else "CURRENT"
                    )
                )
                return {
                    **view(version),
                    "id": document.id,
                    "citation_id": version.id,
                    "classification": document.classification,
                    "roles": document.roles,
                    "status": status,
                }
        with self._tx() as s:
            model = {"memory": Memory, "event": Event, "artifact": Artifact}.get(kind)
            if model is None:
                raise DomainError("NOT_FOUND", status=404)
            columns = [model.run_id, model.classification] + ([model.roles] if model is Memory else [])
            metadata = s.execute(
                select(*columns).where(*filters(model, scope), model.id == identifier)
            ).first()
            if metadata is None:
                raise DomainError("NOT_FOUND", status=404)
            authorize(p, scope, set(metadata[2]) if model is Memory else None, metadata[1])
            self._run_acl(s, p, scope, self._get(s, Run, scope, metadata[0]))
            row = self._get(s, model, scope, identifier)
            result = view(row)
            if isinstance(row, Memory):
                result["version"], result["citation_id"], result["subject_key"] = "1", row.id, row.subject
                result["supersedes"] = None
                result["sources"] = [
                    view(self._get(s, Event, scope, item.event_id))
                    for item in s.scalars(
                        select(MemorySource).where(
                            *filters(MemorySource, scope), MemorySource.memory_id == row.id
                        )
                    )
                ]
                result["source_event_ids"] = [v["id"] for v in result["sources"]]
                for source in result["sources"]:
                    authorize(p, scope, classification=source["classification"])
                    self._run_acl(s, p, scope, self._get(s, Run, scope, source["run_id"]))
            return result

    def verify_events(self, p, scope, identifier):
        """Detect accidental/tampered event edits; not protection from a DB administrator."""
        events = self.events(p, scope, identifier)
        previous, expected = "0" * 64, 1
        for item in events:
            data = {
                "scope": values(scope),
                "run_id": identifier,
                "step_id": item["step_id"],
                "sequence": item["sequence"],
                "kind": item["kind"],
                "actor": item["actor"],
                "payload_hash": item["payload_hash"],
                "prev_hash": item["prev_hash"],
                "classification": item["classification"],
                "created_at": item["created_at"],
            }
            if (
                item["sequence"] != expected
                or item["prev_hash"] != previous
                or digest(item["payload"]) != item["payload_hash"]
                or digest(data) != item["event_hash"]
            ):
                return False
            previous, expected = item["event_hash"], expected + 1
        return True
