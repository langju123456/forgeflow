// Shapes returned by the Fabric API (src/forgeflow/fabric_api.py). Only the fields the workbench reads.

export type Scope = { tenant_id: string; workspace_id: string; project_id: string };

export type Identity = { token: string; subject_id: string; kind: "user" | "service"; roles: string[] };

export type WorkbenchConfig = { mode: string; run_id: string; scope: Scope; identities: Identity[] };

export type Gate = "PASS" | "BLOCKED" | "UNKNOWN";

export type ForwardKind = "TEST_STAGE" | "ISSUE" | "MEMORY_ACTIVATE";
export type ReverseKind = "STAGE_REVERT" | "ISSUE_RETRACT" | "MEMORY_REVOKE";
export type ActionKind = ForwardKind | ReverseKind;

export type Fixture = "aligned" | "bom-mismatch" | "missing-exported";

export type ReviewSummary = {
  id: string;
  run_id: string;
  fixture_id: Fixture;
  created_by: string;
  report_hash: string;
  flow_gate: Gate;
  evt_gate: Gate;
};

export type Decision = "PENDING" | "APPROVED" | "REJECTED";

export type ApprovalSummary = {
  id: string;
  run_id: string;
  review_id: string;
  kind: ActionKind;
  decision: Decision;
  revision: number;
  created_by: string;
  reviewer: string | null;
  proposal_hash: string;
  target_receipt_id: string | null;
  created_at: string;
  expires_at: string;
  receipt_id: string | null;
};

export type Explain = {
  approval: {
    id: string;
    kind: ActionKind;
    decision: Decision;
    revision: number;
    version: number;
    expires_at: string;
    created_by: string;
    adapter_current: boolean;
    history_versions: number;
  };
  parameters: {
    version: number;
    proposal_hash: string;
    review_hash: string;
    technical_hash: string;
    values: Record<string, unknown>;
  };
  if_executed: Record<string, unknown>;
  after_decision: string;
  independence: { may_not_decide: string[] };
  gate: { flow_gate: Gate; evt_gate: Gate; full_evt_eligibility: boolean };
  can_be_decided: { now: boolean; reasons: string[]; renew_with: string | null };
  executed: { receipt_id: string } | null;
  provenance: string;
};

export type ApiError = { status: number; code: string; message: string };
