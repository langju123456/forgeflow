import type { ActionKind, ApprovalSummary, Gate, ReverseKind } from "./types";

export const KIND_LABEL: Record<ActionKind, string> = {
  TEST_STAGE: "进入测试阶段",
  ISSUE: "创建本地 Issue",
  MEMORY_ACTIVATE: "启用 Memory 引用",
  STAGE_REVERT: "撤回阶段变更",
  ISSUE_RETRACT: "撤回 Issue",
  MEMORY_REVOKE: "撤销 Memory 引用",
};

/** Each compensation reverses one executed forward action of a matching kind. */
export const REVERSES: Record<ReverseKind, ActionKind> = {
  STAGE_REVERT: "TEST_STAGE",
  ISSUE_RETRACT: "ISSUE",
  MEMORY_REVOKE: "MEMORY_ACTIVATE",
};

export const GATE_LABEL: Record<Gate, string> = { PASS: "通过", BLOCKED: "阻断", UNKNOWN: "未知" };

export type QueueState = "PENDING" | "APPROVED" | "REJECTED" | "EXECUTED" | "EXPIRED";

export function queueState(item: ApprovalSummary, now = Date.now()): QueueState {
  if (item.receipt_id) return "EXECUTED";
  if (item.decision !== "PENDING") return item.decision;
  return Date.parse(item.expires_at) <= now ? "EXPIRED" : "PENDING";
}

export const STATE_LABEL: Record<QueueState, string> = {
  PENDING: "待审批",
  APPROVED: "已批准，待执行",
  REJECTED: "已拒绝",
  EXECUTED: "已执行",
  EXPIRED: "已过期",
};

/** What each server error means for the person at the desk, and what to do next. */
const ERRORS: Record<string, string> = {
  UNAUTHENTICATED: "没有身份。切换到一个身份再试。",
  NOT_FOUND: "当前身份看不到或不能操作这个对象。审批要用审阅人身份，执行要用执行器身份。",
  INDEPENDENT_REVIEW_REQUIRED: "提案人不能审批自己的提案。换一个审阅人。",
  HUMAN_REQUEST_REQUIRED: "这一步必须由人发起，服务身份不能代劳。",
  HUMAN_REVIEW_REQUIRED: "审批必须由人做，服务身份不能审批。",
  SERVICE_EXECUTION_REQUIRED: "只有执行器（服务身份）能执行，人不能直接执行。",
  TRUSTED_EXECUTOR_REQUIRED: "这个执行器没有执行权限。",
  REVISION_CONFLICT: "有人先改过这条审批。刷新后看最新版本再决定。",
  PROPOSAL_HASH_MISMATCH: "你看到的参数已经变了。刷新后按新参数决定。",
  APPROVAL_ALREADY_DECIDED: "这条审批已经有结论了。",
  APPROVAL_EXPIRED: "审批已过期。重新提案生成新版本。",
  FABRIC_GATE_NOT_PASS: "门禁没有通过，不能提这个动作。",
  EVT_GATE_NOT_PASS: "EVT 门禁没有通过。",
  FABRIC_EFFECT_SOURCES_REQUIRED: "启用 Memory 需要先有已执行的动作作为来源。先执行一个 Issue。",
  FABRIC_ACTION_ALREADY_PROPOSED: "同一个动作已经提过了。",
  FABRIC_PARAMETERS_CHANGED: "提案之后参数依据变了，审批失效。重新提案。",
  FABRIC_SOURCE_CHANGED: "证据来源变了，原评审不再成立。重新评审。",
  FABRIC_RUN_NOT_EXECUTABLE: "这个 run 当前不能执行动作。",
  FABRIC_COMPENSATION_MISMATCH: "补偿动作和要撤回的回执不匹配。",
  STAGE_CONFLICT: "项目阶段已经变了，这个阶段动作不再适用。",
  IDEMPOTENCY_CONFLICT: "同一个请求键对应了不同内容，被拒绝。",
  INVALID_REQUEST: "请求不符合接口约定。",
};

export function explainError(code: string): string {
  return ERRORS[code] ?? "服务端拒绝了这个操作。";
}

export function short(hash: string | null | undefined, n = 10): string {
  return hash ? hash.slice(0, n) : "—";
}
