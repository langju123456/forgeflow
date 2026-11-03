import { describe, expect, it } from "vitest";
import { toApiError } from "./api";
import { explainError, queueState } from "./labels";
import type { ApprovalSummary } from "./types";

const base: ApprovalSummary = {
  id: "a1",
  run_id: "r1",
  review_id: "v1",
  kind: "ISSUE",
  decision: "PENDING",
  revision: 0,
  created_by: "workbench-proposer",
  reviewer: null,
  proposal_hash: "0".repeat(64),
  target_receipt_id: null,
  created_at: "2026-11-03T10:00:00+00:00",
  expires_at: "2026-11-03T11:00:00+00:00",
  receipt_id: null,
};
const before = Date.parse("2026-11-03T10:30:00+00:00");

describe("queueState", () => {
  it("shows an executed receipt before the decision", () => {
    expect(queueState({ ...base, decision: "APPROVED", receipt_id: "x" }, before)).toBe("EXECUTED");
  });
  it("marks a pending approval past its expiry as expired", () => {
    expect(queueState(base, before)).toBe("PENDING");
    expect(queueState(base, Date.parse("2026-11-03T11:00:00+00:00"))).toBe("EXPIRED");
  });
  it("keeps a decision once made", () => {
    expect(queueState({ ...base, decision: "REJECTED" }, before)).toBe("REJECTED");
  });
});

describe("errors", () => {
  it("reads the server's error code", () => {
    expect(toApiError(409, { code: "REVISION_CONFLICT", message: "x" })).toEqual({ status: 409, code: "REVISION_CONFLICT", message: "x" });
  });
  it("names validation failures without a code", () => {
    expect(toApiError(422, { detail: [] }).code).toBe("INVALID_REQUEST");
  });
  it("explains every code the desk acts on, and falls back for the rest", () => {
    expect(explainError("SERVICE_EXECUTION_REQUIRED")).toContain("执行器");
    expect(explainError("SOMETHING_NEW")).toBe("服务端拒绝了这个操作。");
  });
});
