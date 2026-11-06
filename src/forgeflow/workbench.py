"""Local review workbench: the Fabric API plus the React UI on one loopback port.

DEV_ONLY. It seeds a SQLite database from the sealed M01 archive with the same offline rebinding the Fabric tests
use (scope and document references rebound, every derived hash rebuilt, no new checker execution), and serves three
fixed development identities so one person can play proposer, reviewer and executor:

    python -m forgeflow.workbench            # then open http://127.0.0.1:8780/workbench/

Nothing here is a production identity system; tokens are fixed strings and the server binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
from datetime import timedelta
from pathlib import Path

from sqlalchemy import DateTime

from . import contracts
from .contracts import Principal, digest
from .fabric_contract import FABRIC_SCOPE
from .fabric_runtime import FabricStore
from .fabric_store import FabricOperations
from .local import SCOPE as LEGACY_SCOPE
from .sre_ops import event_chain_valid
from .store import Action, Approval, Artifact, Event, Evidence, Run, Step, aware

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "deliveries/2026-10-06/s2c-fixes/evidence/experiment-01/raw-chains/M01.json"
WEB_DIST = ROOT / "web" / "dist"
DOC_ROLES = {"admin", "supervisor", "reviewer", "fabric_executor", "memory_writer", "hardware"}
DOC_CONTENT = "OFFLINE_DERIVED synthetic Aurora fixture, not a new checker execution or live PostgreSQL audit."

# Fixed DEV_ONLY tokens. The UI's role switcher sends one of these as the Bearer token.
IDENTITIES = {
    "dev-proposer": Principal(subject_id="workbench-proposer", kind="user", roles=frozenset({"admin", "supervisor"}),
                              scopes=(FABRIC_SCOPE,)),
    "dev-reviewer": Principal(subject_id="workbench-reviewer", kind="user", roles=frozenset({"reviewer"}),
                              scopes=(FABRIC_SCOPE,)),
    "dev-executor": Principal(subject_id="workbench-executor", kind="service",
                              roles=frozenset({"fabric_executor", "memory_writer", "hardware"}),
                              scopes=(FABRIC_SCOPE,)),
}


def _rechain(run, events):
    previous = "0" * 64
    for sequence, event in enumerate(events, 1):
        event["sequence"], event["prev_hash"] = sequence, previous
        event["payload_hash"] = digest(event["payload"])
        event["event_hash"] = digest({
            "scope": FABRIC_SCOPE.model_dump(mode="json"), "run_id": run["id"],
            **{key: event[key] for key in ("step_id", "sequence", "kind", "actor", "payload_hash",
                                           "prev_hash", "classification", "created_at")},
        })
        previous = event["event_hash"]


def _refresh_bindings(run, events):
    run["request_hash"] = digest(run["request_json"])
    run["snapshot_hash"] = digest(run["snapshot_json"])
    result_hash = digest(run["result_json"])
    for step in run["steps"]:
        if step["step_key"] == "readiness":
            step["output_hash"] = result_hash
    approvals = {}
    for approval in run["approvals"]:
        approval["proposal_hash"] = digest(approval["proposal"])
        approval["snapshot_hash"] = digest(approval["proposal"]["snapshot"])
        approvals[approval["id"]] = approval
    for event in events:
        if event["kind"] == "RUN_SUBMITTED":
            event["payload"]["request_hash"] = run["request_hash"]
        elif event["kind"] == "CHECK_COMPLETED":
            event["payload"].update(result_hash=result_hash, assessment_hash=digest(run["assessment_json"]),
                                    status=run["assessment_json"].get("status", "UNKNOWN"))
        elif event["kind"] == "APPROVAL_PREPARED":
            event["payload"]["proposal_hash"] = approvals[event["payload"]["approval_id"]]["proposal_hash"]
    _rechain(run, events)


def _offline_parent():
    archive = json.loads(ARCHIVE.read_text())
    run, events = copy.deepcopy((archive["parent_after"], archive["parent_events"]))
    old, new = LEGACY_SCOPE.model_dump(mode="json"), FABRIC_SCOPE.model_dump(mode="json")

    def rebind(value):
        if isinstance(value, dict):
            if all(value.get(key) == item for key, item in old.items()):
                value.update(new)
            for child in value.values():
                rebind(child)
        elif isinstance(value, list):
            for child in value:
                rebind(child)

    rebind(run)
    rebind(events)
    completion = next(event for event in events if event["kind"] == "CHECK_COMPLETED")
    run["snapshot_json"]["evaluation_clock"] = completion["created_at"]
    for approval in run["approvals"]:
        approval["proposal"]["snapshot"] = copy.deepcopy(run["snapshot_json"])
    _refresh_bindings(run, events)
    return run, events


def seed(database: Path) -> tuple[FabricStore, str]:
    """A fresh SQLite database holding one derived parent run in the Fabric scope. Returns the store and run id."""
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    source = FabricStore("sqlite:///" + str(database))
    source.create_schema()
    owner = IDENTITIES["dev-proposer"]
    source.ensure_project(FABRIC_SCOPE)
    run, events = _offline_parent()
    validity = (contracts.utcnow() + timedelta(days=7)).isoformat()
    run["snapshot_json"]["valid_until"] = validity
    for reference in run["snapshot_json"]["documents"]:
        document = source.put_document(owner, FABRIC_SCOPE, reference["id"], reference["version"], DOC_CONTENT,
                                       DOC_ROLES, 0, valid_until=validity)
        reference["source_hash"] = document["source_hash"]
    for approval in run["approvals"]:
        approval["proposal"]["snapshot"] = copy.deepcopy(run["snapshot_json"])
    for event in events:
        event["created_at"] = aware(event["created_at"]).isoformat()
    _refresh_bindings(run, events)
    with source._tx() as session:
        for model, items in ((Run, [run]), (Step, run["steps"]), (Artifact, run["artifacts"]),
                             (Evidence, run["evidence"]), (Approval, run["approvals"]),
                             (Action, run["actions"]), (Event, events)):
            for item in items:
                data = {column.name: item[column.name] for column in model.__table__.columns if column.name in item}
                for column in model.__table__.columns:
                    if isinstance(column.type, DateTime) and data.get(column.name):
                        data[column.name] = aware(data[column.name])
                session.add(model(**data))
            session.flush()
    FabricOperations(source).create_schema()
    if not event_chain_valid(FABRIC_SCOPE, run["id"], source.events(owner, FABRIC_SCOPE, run["id"])):
        raise RuntimeError("seeded event chain does not verify")
    return source, run["id"]


def create_workbench_app(source, run_id: str, desk=None):
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    from .fabric_api import create_fabric_app

    app = create_fabric_app(source, IDENTITIES, None, test_harness=True)
    app.title = "ForgeFlow review workbench (DEV_ONLY)"

    @app.get("/workbench-config")
    def config():
        return {"mode": "DEV_ONLY_LOCAL_SYNTHETIC", "run_id": run_id,
                "scope": FABRIC_SCOPE.model_dump(mode="json"),
                "identities": [{"token": token, "subject_id": p.subject_id, "kind": p.kind,
                                "roles": sorted(p.roles)} for token, p in IDENTITIES.items()]}

    @app.get("/")
    def root():
        return RedirectResponse("/workbench/")

    if desk is not None:
        from .report_desk import mount_report_desk

        mount_report_desk(app, desk)

    if WEB_DIST.exists():
        app.mount("/workbench", StaticFiles(directory=WEB_DIST, html=True), name="workbench")
    return app


def main(argv=None):
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(ROOT / ".workbench" / "workbench.db"))
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--reports", default=str(Path.home() / "code" / "finreports"),
                        help="folder with the public annual-report PDFs for the 年报问答 page")
    args = parser.parse_args(argv)
    source, run_id = seed(Path(args.db))
    desk = None
    try:
        import pdfplumber  # noqa: F401 - the report desk needs the pdf extra

        from .report_desk import ReportDesk, default_answerer

        desk = ReportDesk(Path(args.reports), Path(args.db).parent / "reports", answerer=default_answerer())
        threading.Thread(target=desk.load, name="report-desk-load", daemon=True).start()
    except ImportError:
        print("年报问答 is off: install the pdf extra (pip install -e '.[pdf]').")
    if not WEB_DIST.exists():
        print("web/dist is missing: run `npm ci && npm run build` in web/ first (the API still starts).")
    print(f"ForgeFlow review workbench: http://127.0.0.1:{args.port}/workbench/  (run {run_id})")
    uvicorn.run(create_workbench_app(source, run_id, desk), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
