"""The one Store that production entry points use: legacy hardware-only approvals are never granted.

The hardware-only gate cannot authorize a stage change or business action; EVT acceptance and
Fabric actions have their own bound approvals. The condition lives in ``legacy_approval_gate`` so
the HTTP route and the Store cannot drift apart. Blocked attempts are audited in their own
transaction, because the blocked operation itself rolls back.
"""

from sqlalchemy import select

from .contracts import DomainError, authorize
from .store import Action, Approval, Run, Store, filters

BLOCKED_EVENT = "LEGACY_APPROVAL_BLOCKED"


def legacy_approval_gate(decision):
    """Raise for a hardware-only APPROVED decision; REJECTED stays available."""
    if decision == "APPROVED":
        raise DomainError("EVT_BOUND_APPROVAL_REQUIRED", status=409)


class EVTBoundStore(Store):
    def _audit_blocked(self, p, scope, run_id, approval_id, decision, entry):
        with self._tx() as s:
            approval = s.scalar(select(Approval).where(*filters(Approval, scope), Approval.id == approval_id))
            if approval is None or approval.run_id != run_id:
                return
            run = self._get(s, Run, scope, run_id, lock=True)
            self._run_acl(s, p, scope, run)
            self._event(s, p, scope, run, BLOCKED_EVENT, {
                "approval_id": approval.id, "kind": approval.kind, "decision": decision, "entry": entry})

    def decide(self, p, scope, identifier, proposal_hash, expected_run_revision, decision):
        authorize(p, scope, {"reviewer"})
        if p.kind != "user":
            raise DomainError("HUMAN_REVIEW_REQUIRED", status=403)
        try:
            legacy_approval_gate(decision)
        except DomainError:
            with self._tx() as s:
                run_id = s.scalar(select(Approval.run_id).where(*filters(Approval, scope), Approval.id == identifier))
            if run_id is not None:
                self._audit_blocked(p, scope, run_id, identifier, decision, "decide")
            raise
        return super().decide(p, scope, identifier, proposal_hash, expected_run_revision, decision)

    def dispatch_local(self, p, scope, identifier, action_id, owner, *, lease_token, epoch):
        authorize(p, scope, {"worker"})
        with self._tx() as s:
            action = s.scalar(select(Action).where(
                *filters(Action, scope), Action.id == action_id, Action.run_id == identifier))
            blocked = (action.approval_id, action.status) if action is not None else None
        # Only an APPROVED row can reach an effect; replays and other states keep their own errors.
        if blocked is not None and blocked[1] == "APPROVED":
            try:
                legacy_approval_gate("APPROVED")
            except DomainError:
                self._audit_blocked(p, scope, identifier, blocked[0], "APPROVED", "dispatch_local")
                raise
        return super().dispatch_local(p, scope, identifier, action_id, owner, lease_token=lease_token, epoch=epoch)
