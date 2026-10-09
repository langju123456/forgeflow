"""Plan or apply a renewal of an expiring document: same content, later expiry, new version.

Rehearse on a copy of the database first. The new version becomes current, so runs that cite the old version
stop passing the freshness check from the moment it is written; the plan lists them before anything is written.
Without --apply this only reads. It needs an explicit --database-url and never falls back to the configured
database, so a rehearsal cannot reach the live one by accident.
"""

import argparse
import json
import re

from sqlalchemy import inspect, select

from .contracts import DomainError, Principal, Scope
from .evt_store import EVTBoundStore
from .store import Action, Approval, Document, DocumentVersion, Run, aware, filters

EFFECT = ("The new version becomes current: runs that cite the old version fail the freshness check with "
          "SOURCE_CHANGED from the moment it is written. Back the database up first (pg_dump for PostgreSQL).")
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}


def _pending(session, store, scope, run):
    items = [row.id for row in session.scalars(select(Approval).where(
        *filters(Approval, scope), Approval.run_id == run.id, Approval.decision.in_(["PENDING", "APPROVED"])))
        if session.scalar(select(Action.status).where(*filters(Action, scope), Action.approval_id == row.id))
        != "SUCCEEDED"]
    if inspect(store.engine).has_table("fabric_approvals"):
        from .fabric_store import FabricApproval, FabricReceipt

        items += [row.id for row in session.scalars(select(FabricApproval).where(
            *filters(FabricApproval, scope), FabricApproval.run_id == run.id,
            FabricApproval.decision.in_(["PENDING", "APPROVED"])))
            if not session.scalar(select(FabricReceipt.id).where(
                *filters(FabricReceipt, scope), FabricReceipt.approval_id == row.id))]
    return items


def seed_document_once(store, principal, scope, document_id, version, content, roles, classification, valid_until):
    """Seed a document into a database that has none. An existing one is never touched: versions are immutable
    and changing a live document is what renew() is for, with a plan first."""
    with store._tx() as session:
        exists = session.scalar(select(Document.id).where(*filters(Document, scope), Document.id == document_id))
    if exists is None:
        store.put_document(principal, scope, document_id, version, content, roles, classification,
                           valid_until=valid_until)


def renew(store, scope, document_id, *, valid_until, apply=False):
    requested = aware(valid_until)
    with store._tx() as session:
        document = session.scalar(select(Document).where(*filters(Document, scope), Document.id == document_id))
        if document is None or document.deleted:
            raise DomainError("NOT_FOUND", "Object unavailable", 404)
        versions = session.scalars(select(DocumentVersion).where(
            *filters(DocumentVersion, scope), DocumentVersion.document_id == document_id)).all()
        current = next(row for row in versions if row.version == document.current_version)
        match = re.fullmatch(r"v(\d+)", current.version)
        if match is None:
            raise DomainError("RENEWAL_VERSION_UNSUPPORTED", status=422)
        expiry = aware(current.valid_until) if current.valid_until else None
        renewed_before = any(row.source_hash == current.source_hash and row.version != current.version
                             for row in versions)
        if expiry == requested and renewed_before:
            return {"status": "ALREADY_CURRENT", "document": document_id, "version": current.version,
                    "valid_until": requested.isoformat()}
        if expiry is not None and requested <= expiry:
            raise DomainError("RENEWAL_NOT_LATER", status=422)
        affected = []
        for run in session.scalars(select(Run).where(*filters(Run, scope))):
            cited = [item for item in (run.snapshot_json or {}).get("documents", [])
                     if item.get("id") == document_id and item.get("version") == current.version]
            if cited and run.state not in TERMINAL:
                affected.append({"run_id": run.id, "state": run.state,
                                 "pending_approvals": _pending(session, store, scope, run)})
        plan = {"status": "DRY_RUN", "document": document_id, "from_version": current.version,
                "to_version": f"v{int(match.group(1)) + 1}", "valid_until": requested.isoformat(),
                "content_sha256": current.source_hash, "affected": affected, "effect": EFFECT}
        content, roles, classification = current.content, set(document.roles), document.classification
    if not apply:
        return plan
    admin = Principal(subject_id="sop-renewal", kind="user", roles=frozenset({"admin"}), scopes=(scope,), clearance=3)
    store.put_document(admin, scope, document_id, plan["to_version"], content, roles, classification,
                       valid_until=requested.isoformat())
    return {**plan, "status": "RENEWED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", dest="url", required=True)
    parser.add_argument("--scope", required=True, help="tenant/workspace/project")
    parser.add_argument("--document", required=True)
    parser.add_argument("--valid-until", required=True, help="ISO 8601, e.g. 2027-04-05T00:00:00Z")
    parser.add_argument("--apply", action="store_true", help="write the new version (default: plan only)")
    args = parser.parse_args(argv)
    tenant, workspace, project = args.scope.split("/")
    scope = Scope(tenant_id=tenant, workspace_id=workspace, project_id=project)
    result = renew(EVTBoundStore(args.url), scope, args.document, valid_until=args.valid_until, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
