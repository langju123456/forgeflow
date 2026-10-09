"""The representative chain: request, check, model analysis, Gate, then approvals that a person decides.

This module orders the steps and checks their invariants. The real Docker check, MCP handshake and model call
come in as callbacks, so the same code runs against the offline fixtures in tests and against Docker and the
local model in the rehearsal. It never decides an approval and never executes one: those stay separate, explicit
commands, so the record shows who decided what.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

from . import contracts
from .contracts import DomainError, RunRequest
from .evt_review import EVTApproval
from .fabric_contract import FABRIC_SCOPE
from .fabric_store import FabricApproval, FabricReceipt
from .fixture_family import cli_fixture
from .sre_ops import event_chain_valid
from .store import Event, Project, Run, filters, view

COPY_PREFIX = "forgeflow_chain_"
ACTIONS = ("TEST_STAGE", "ISSUE")


def require_copy_database(url):
    """The driver writes runs, approvals and receipts; it only runs on a database named as a copy of the live one."""
    try:
        name = Path(make_url(url).database or "").name
    except Exception:  # an unparseable URL must not be echoed back
        name = ""
    if not name.startswith(COPY_PREFIX):
        raise DomainError("CHAIN_NEEDS_A_DATABASE_COPY",
                          f"The chain driver only runs on a database whose name starts with {COPY_PREFIX}", 403)


async def start_chain(source, service, owner, *, key, run_id=None, fixture="pass", check=None, analyze=None):
    """Submit and check a run (or resume one), analyse it, review it and propose the actions; decide nothing."""
    steps = []

    def record(name, **info):
        steps.append({"step": name, "at": contracts.utcnow().isoformat(), **info})

    if run_id is None:
        request = RunRequest(scope=FABRIC_SCOPE, fixture_id=cli_fixture(fixture))
        run = source.submit(owner, request, key)
        record("submit", run_id=run["id"], fixture_id=request.fixture_id,
               same_key_returns_same_run=source.submit(owner, request, key)["id"] == run["id"])
        run_id = run["id"]
        if check is not None:
            checked = await check(FABRIC_SCOPE, run_id)
            result = checked.get("result_json") or {}
            record("check", status=result.get("status"), cleanup=result.get("cleanup"),
                   assessment=(checked.get("assessment_json") or {}).get("status"),
                   mcp_protocol=(result.get("mcp") or {}).get("protocol_version"),
                   mcp_transport=(result.get("mcp") or {}).get("transport"),
                   scanner_engine_executed=(result.get("scan") or {}).get("engine_executed"))
    if analyze is not None:
        analysis = await analyze(owner, FABRIC_SCOPE, run_id, "chain-analysis:" + key)
        record("analysis", state=analysis["state"], model_calls=len(analysis["calls"]),
               analysis_id=analysis.get("id"))
    review = service.create(owner, FABRIC_SCOPE, run_id, "chain-review:" + key)
    gates = review["report_json"]
    record("review", review_id=review["id"], flow_gate=gates["flow_gate"], evt_gate=gates["evt_gate"],
           report_hash=review["report_hash"])
    proposals, refused = {}, {}
    for kind in ACTIONS:
        try:
            approval = service.propose(owner, FABRIC_SCOPE, review["id"], review["report_hash"],
                                       f"chain-{kind}:{key}", kind)
        except DomainError as error:
            refused[kind] = error.code
            record("refused:" + kind, code=error.code)
        else:
            proposals[kind] = {"approval_id": approval["id"], "proposal_hash": approval["proposal_hash"]}
            record("propose:" + kind, approval_id=approval["id"], proposal_hash=approval["proposal_hash"],
                   decision=approval["decision"])
    return {"run_id": run_id, "review_id": review["id"], "report_hash": review["report_hash"],
            "technical_hash": gates["technical_hash"], "flow_gate": gates["flow_gate"], "evt_gate": gates["evt_gate"],
            "proposals": proposals, "refused": refused, "steps": steps}


def verify_chain(source, service, owner, state):
    """What the record shows now: the audit chain, the events, the approvals and receipts, the review still binding."""
    with source._tx() as session:
        events = [view(row) for row in session.scalars(select(Event).where(
            *filters(Event, FABRIC_SCOPE), Event.run_id == state["run_id"]).order_by(Event.sequence))]
        approvals = session.scalars(select(FabricApproval).where(
            *filters(FabricApproval, FABRIC_SCOPE), FabricApproval.run_id == state["run_id"])).all()
        receipts = session.scalars(select(FabricReceipt).where(
            *filters(FabricReceipt, FABRIC_SCOPE), FabricReceipt.run_id == state["run_id"])).all()
        decisions = {row.kind: row.decision for row in approvals}
        receipt_kinds = sorted(row.kind for row in receipts)
    review = service.get(owner, FABRIC_SCOPE, state["review_id"])
    return {"audit_chain_valid": event_chain_valid(FABRIC_SCOPE, state["run_id"], events),
            "event_kinds": [event["kind"] for event in events], "approvals": len(approvals), "decisions": decisions,
            "receipts": len(receipts), "receipt_kinds": receipt_kinds,
            "review_matches": review["report_hash"] == state["report_hash"]}


def original_digest(source, scope):
    """One digest over a project's runs, event-chain heads, stage and EVT acceptances; equal means untouched."""
    with source._tx() as session:
        runs = sorted((row.id, row.state, row.revision) for row in session.scalars(
            select(Run).where(*filters(Run, scope))))
        heads = sorted((row.run_id, row.sequence, row.event_hash) for row in session.scalars(
            select(Event).where(*filters(Event, scope))))
        project = session.scalar(select(Project).where(*filters(Project, scope)))
        stage = [project.current_stage, project.stage_revision] if project else None
        accepted = sorted((row.id, row.decision) for row in session.scalars(
            select(EVTApproval).where(*filters(EVTApproval, scope))))
    return contracts.digest({"runs": runs, "events": heads, "stage": stage, "evt_acceptances": accepted})
