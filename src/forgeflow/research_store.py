"""Local annual research accounts and append-only revisions. Enroll offline to enable the store."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import contracts
from .contracts import DomainError, canonical, digest


def _upgrade_grants(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 1:
        raise DomainError("SCHEMA_TOO_NEW", "研究库版本较新，请使用匹配的应用版本。", 503)
    if version == 0:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS source_request (id TEXT PRIMARY KEY, subject TEXT NOT NULL,
            run TEXT NOT NULL, transport TEXT NOT NULL, expires REAL NOT NULL)""")
        db.execute("PRAGMA user_version=1")
        db.commit()


def root_path() -> Path:
    raw = os.environ.get("FORGEFLOW_RESEARCH_DIR")
    if raw is None:
        return Path(__file__).resolve().parents[2] / ".workbench" / "annual-research"
    if not Path(raw).is_absolute():
        raise DomainError("RESEARCH_DISABLED", "FORGEFLOW_RESEARCH_DIR 需指定本机研究库绝对路径。", 503)
    return Path(raw)


def issue(root: Path, subject: str, role: str) -> str:
    """Offline enrollment. Existing account names cannot be replaced by enrollment."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", subject) or role not in {"author", "reviewer"}:
        raise ValueError("账号使用 1–64 位英文、数字、短横线或下划线；角色为 author 或 reviewer。")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError("请指定独立的私有目录，权限须为 0700。")
    path = root / "research.sqlite3"
    path.touch(mode=0o600, exist_ok=True)
    token = secrets.token_urlsafe(32)
    db = sqlite3.connect(path)
    try:
        _upgrade_grants(db)
        db.executescript("""
            CREATE TABLE IF NOT EXISTS account (subject TEXT PRIMARY KEY, role TEXT NOT NULL, token_hash TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS draft (id TEXT PRIMARY KEY, owner TEXT NOT NULL, reviewer TEXT,
                version INTEGER NOT NULL, head TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revision (draft TEXT, version INTEGER, digest TEXT UNIQUE, payload TEXT NOT NULL,
                PRIMARY KEY(draft,version));
            CREATE TABLE IF NOT EXISTS event (seq INTEGER PRIMARY KEY, draft TEXT, version INTEGER, kind TEXT,
                actor TEXT NOT NULL, memo TEXT NOT NULL, at TEXT NOT NULL, digest TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_delivery ON event(draft,version) WHERE kind='delivered';
        """)
        db.execute("INSERT INTO account VALUES(?,?,?)", (subject, role, _token_hash(token)))
        db.commit()
    finally:
        db.close()
    return token


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def connection(*, write=False):
    path = root_path() / "research.sqlite3"
    if not path.is_file():
        raise DomainError("RESEARCH_DISABLED", "请先在本机创建研究员和审阅人账号。", 503)
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        _upgrade_grants(db)
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        db.commit()
    finally:
        db.close()


def identify(token: str):
    with connection() as db:
        row = db.execute("SELECT subject,role FROM account WHERE token_hash=?", (_token_hash(token),)).fetchone()
    if not row:
        raise DomainError("IDENTITY_REQUIRED", "本机身份凭证无效。", 401)
    return dict(row)


def role_is(actor, role):
    if actor["role"] != role:
        raise DomainError("ROLE_REQUIRED", "当前身份无权执行此操作。", 403)


def grant_source(actor, run_id, transport):
    role_is(actor, "author")
    grant = uuid4().hex
    with connection(write=True) as db:
        now = time.time()
        db.execute("DELETE FROM source_request WHERE expires<=?", (now,))
        db.execute("INSERT INTO source_request VALUES(?,?,?,?,?)", (grant, actor["subject"], run_id, transport, now + 3600))
    return grant


def granted_source(actor, grant, transport):
    with connection() as db:
        row = db.execute("SELECT run FROM source_request WHERE id=? AND subject=? AND transport=? AND expires>?",
                         (grant, actor["subject"], transport, time.time())).fetchone()
    if not row:
        raise DomainError("SOURCE_REQUEST_UNAVAILABLE", "请用当前研究员身份重新运行查表。", 404)
    return row["run"]


def _visible(db, actor, draft_id):
    row = db.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    if row and (actor["subject"] == row["owner"] or
                (actor["subject"] == row["reviewer"] and row["state"] != "draft")):
        return row
    raise DomainError("DRAFT_NOT_FOUND", "当前身份没有这份可见稿件。", 404)


def _summary(row):
    return {k: row[k] for k in ("id", "owner", "reviewer", "version", "head", "state")}


def list_drafts(actor):
    with connection() as db:
        rows = db.execute("""SELECT d.*, r.payload FROM draft d JOIN revision r ON r.draft=d.id AND r.version=d.version
            WHERE d.owner=? OR (d.reviewer=? AND d.state!='draft') ORDER BY d.rowid DESC""",
                          (actor["subject"], actor["subject"]))
        return [_summary(row) | {"question": json.loads(row["payload"])["evidence"]["question"]} for row in rows]


def view(db, actor, draft_id):
    row = _visible(db, actor, draft_id)
    payload = db.execute("SELECT payload FROM revision WHERE draft=? AND version=?", (draft_id, row["version"])).fetchone()
    history = db.execute("SELECT * FROM event WHERE draft=? ORDER BY seq", (draft_id,))
    # A reviewer sees submitted versions. Intermediate unsent author notes stay private.
    events = [dict(e) for e in history]
    if actor["role"] == "reviewer":
        submitted = {e["version"] for e in events if e["kind"] == "submitted"}
        events = [e for e in events if e["version"] in submitted]
    record = json.loads(payload[0])
    if digest(record) != row["head"]:
        raise DomainError("INTEGRITY_ERROR", "保存的正文与当前版本摘要不一致，请核对研究库。", 409)
    delivery = next((e for e in events if e["version"] == row["version"] and e["kind"] == "delivered"), None)
    return {**_summary(row), "question": record["evidence"]["question"], "payload": record, "events": events,
            "local_receipt": digest(delivery) if delivery else None}


def get_draft(actor, draft_id):
    with connection() as db:
        return view(db, actor, draft_id)


def current_evidence(actor, draft_id, expected):
    """Return the exact parent evidence for a scoped revision before saving it.

    ``save`` repeats the head check inside its write transaction.  This read only
    chooses which already-archived metrics may be retained; it never turns a
    stale parent into a valid write.
    """
    with connection() as db:
        row = _visible(db, actor, draft_id)
        _expect(row, expected)
        payload = db.execute("SELECT payload FROM revision WHERE draft=? AND version=?",
                             (draft_id, row["version"])).fetchone()
        return json.loads(payload["payload"])["evidence"]


def _event(db, row, kind, subject, memo=""):
    db.execute("INSERT INTO event(draft,version,kind,actor,memo,at,digest) VALUES(?,?,?,?,?,?,?)",
               (row["id"], row["version"], kind, subject, memo, contracts.utcnow().isoformat(), row["head"]))


def _expect(row, expected):
    if row["head"] != expected:
        raise DomainError("HEAD_CHANGED", "稿件已修订，请重新打开当前版本。", 409)


def save(actor, evidence, note, *, draft_id=None, expected=None):
    role_is(actor, "author")
    with connection(write=True) as db:
        if draft_id:
            row = _visible(db, actor, draft_id)
            _expect(row, expected)
            if row["state"] == "submitted":
                raise DomainError("IN_REVIEW", "送审后锁定该版，请等待审阅决定。", 409)
            version = row["version"] + 1
        else:
            draft_id, version = uuid4().hex, 1
        payload = {"schema_version": "forgeflow.research.revision.v1", "draft_id": draft_id, "version": version,
                   "parent_digest": expected, "owner": actor["subject"], "note": note,
                   "saved_at": contracts.utcnow().isoformat(), "evidence": evidence}
        head = digest(payload)
        if version == 1:
            db.execute("INSERT INTO draft VALUES(?,?,NULL,?,?, 'draft')", (draft_id, actor["subject"], version, head))
        else:
            db.execute("UPDATE draft SET version=?,head=?,state='draft' WHERE id=?", (version, head, draft_id))
        db.execute("INSERT INTO revision VALUES(?,?,?,?)", (draft_id, version, head, canonical(payload)))
        _event(db, {"id": draft_id, "version": version, "head": head}, "saved", actor["subject"])
        return view(db, actor, draft_id)


def decide(actor, draft_id, expected, action, reviewer=None, memo=""):
    target = {"submit": "submitted", "return": "returned", "approve": "approved", "deliver": "delivered"}[action]
    role_is(actor, "reviewer" if action in {"return", "approve"} else "author")
    with connection(write=True) as db:
        row = _visible(db, actor, draft_id)
        _expect(row, expected)
        if action == "deliver" and row["state"] == "delivered":
            return view(db, actor, draft_id)
        initial = {"submit": "draft", "return": "submitted", "approve": "submitted", "deliver": "approved"}[action]
        if row["state"] != initial:
            raise DomainError("INVALID_STATE", "此操作要求稿件处于对应的审阅状态。", 409)
        if action == "submit":
            if row["reviewer"] and reviewer != row["reviewer"]:
                raise DomainError("REVIEWER_BOUND", "同一稿件的各版本由原指派人复核。", 409)
            if not db.execute("SELECT 1 FROM account WHERE subject=? AND role='reviewer'", (reviewer,)).fetchone():
                raise DomainError("REVIEWER_REQUIRED", "请指定已经在本机登记的审阅人账号。", 422)
            db.execute("UPDATE draft SET reviewer=? WHERE id=?", (reviewer, draft_id))
        if action == "return" and not memo.strip():
            raise DomainError("REASON_REQUIRED", "请写明需要研究员修改的地方。", 422)
        db.execute("UPDATE draft SET state=? WHERE id=?", (target, draft_id))
        _event(db, row, target, actor["subject"], memo)
        return view(db, actor, draft_id)


def main():
    parser = argparse.ArgumentParser(description="在本机创建一个年报研究身份。凭证仅在登记时显示一次。")
    parser.add_argument("role", choices=["author", "reviewer"])
    parser.add_argument("subject")
    args = parser.parse_args()
    try:
        print(issue(root_path(), args.subject, args.role))
    except (ValueError, DomainError, sqlite3.IntegrityError) as exc:
        parser.exit(2, f"登记失败：{exc}\n")


if __name__ == "__main__":
    main()
