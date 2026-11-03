import type {
  ApiError,
  ApprovalSummary,
  Explain,
  Fixture,
  ActionKind,
  ReviewSummary,
  Scope,
  WorkbenchConfig,
} from "./types";

/** Every request carries the chosen identity as a Bearer token; scope travels in the query or the body. */
export class Api {
  constructor(
    private readonly scope: Scope,
    private readonly token: string,
  ) {}

  private query(): string {
    return new URLSearchParams(this.scope).toString();
  }

  private async call<T>(method: "GET" | "POST", path: string, body?: unknown, idempotent = false): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    // A new key per click: a retried request with the same key is replayed by the server, never applied twice.
    if (idempotent) headers["Idempotency-Key"] = newKey();
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) throw toApiError(response.status, data);
    return data as T;
  }

  reviews() {
    return this.call<ReviewSummary[]>("GET", `/fabric/reviews?${this.query()}`);
  }

  approvals() {
    return this.call<ApprovalSummary[]>("GET", `/fabric/approvals?${this.query()}`);
  }

  explain(approvalId: string) {
    return this.call<Explain>("GET", `/fabric/approvals/${approvalId}/explain?${this.query()}`);
  }

  createReview(runId: string, fixture: Fixture) {
    return this.call<{ id: string }>("POST", `/fabric/runs/${runId}/review`, { scope: this.scope, fixture_id: fixture }, true);
  }

  propose(review: ReviewSummary, kind: ActionKind, targetReceiptId?: string) {
    return this.call<{ id: string }>(
      "POST",
      `/fabric/reviews/${review.id}/proposals`,
      { scope: this.scope, review_hash: review.report_hash, kind, target_receipt_id: targetReceiptId ?? null },
      true,
    );
  }

  decide(explain: Explain, decision: "APPROVED" | "REJECTED") {
    // The reviewer signs exactly the parameters they saw: the proposal hash and the revision they read.
    return this.call<unknown>("POST", `/fabric/approvals/${explain.approval.id}/decision`, {
      scope: this.scope,
      proposal_hash: explain.parameters.proposal_hash,
      expected_revision: explain.approval.revision,
      decision,
    });
  }

  execute(approvalId: string) {
    return this.call<{ receipt: { id: string }; replayed?: boolean }>("POST", `/fabric/approvals/${approvalId}/execute`, this.scope);
  }
}

export async function loadConfig(): Promise<WorkbenchConfig> {
  const response = await fetch("/workbench-config");
  if (!response.ok) throw toApiError(response.status, null);
  return response.json();
}

export function toApiError(status: number, data: unknown): ApiError {
  if (data && typeof data === "object" && "code" in data) {
    const record = data as { code: unknown; message?: unknown };
    return { status, code: String(record.code), message: String(record.message ?? record.code) };
  }
  if (status === 422) return { status, code: "INVALID_REQUEST", message: "Request did not match the contract" };
  return { status, code: `HTTP_${status}`, message: "Unexpected response" };
}

export function isApiError(value: unknown): value is ApiError {
  return !!value && typeof value === "object" && "code" in value && "status" in value;
}

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `wb-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
