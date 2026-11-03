import { GATE_LABEL, KIND_LABEL, explainError, short } from "../labels";
import type { ApiError, Explain, Identity } from "../types";

const REASON_LABEL: Record<string, string> = {
  EXPIRED: "审批已过期",
  REJECTED: "已被拒绝",
  ADAPTER_CHANGED: "执行代码变了",
  EVIDENCE_EXPIRED: "证据已过期",
  SOURCE_CHANGED: "来源变了",
  FABRIC_SOURCE_CHANGED: "证据来源变了",
  FABRIC_EVIDENCE_REQUIRED: "缺少证据",
  REVIEW_STALE: "评审已过时",
};

function Row(props: { label: string; children: React.ReactNode }) {
  return (
    <div className="kv">
      <dt>{props.label}</dt>
      <dd>{props.children}</dd>
    </div>
  );
}

function Hash(props: { value: string }) {
  return (
    <span className="mono hash" title={props.value}>
      {short(props.value, 16)}…
    </span>
  );
}

function describeEffect(effect: Record<string, unknown>): string[] {
  const lines: string[] = [];
  const stage = effect.stage as { from: string; to: string } | undefined;
  const creates = effect.creates as { kind: string; status: string; title?: string; subject?: string } | undefined;
  const item = effect.item as { id: string; from_status: string; to_status: string } | undefined;
  if (stage) lines.push(`项目阶段 ${stage.from} → ${stage.to}`);
  if (creates) lines.push(`新建 ${creates.kind}（${creates.status}）${creates.title ?? creates.subject ?? ""}`);
  if (item) lines.push(`条目 ${short(item.id, 8)}：${item.from_status} → ${item.to_status}`);
  if (effect.reverses_receipt) lines.push(`撤回回执 ${short(String(effect.reverses_receipt))}`);
  lines.push(effect.external_writes ? "会写外部系统" : "不写任何外部系统，只写本地数据库");
  if (effect.simulation) lines.push("合成场景里的模拟动作");
  return lines;
}

export function ExplainPanel(props: {
  explain: Explain | null;
  error: ApiError | null;
  identity: Identity | null;
  busy: boolean;
  onDecide: (decision: "APPROVED" | "REJECTED") => void;
  onExecute: () => void;
}) {
  const { explain, identity } = props;
  if (props.error) {
    return (
      <div className="card detail">
        <h2>看不到这条审批</h2>
        <p className="hint">
          <span className="mono">{props.error.code}</span>：{explainError(props.error.code)}
        </p>
      </div>
    );
  }
  if (!explain) {
    return (
      <div className="card detail placeholder">
        <h2>选一条审批</h2>
        <p>左边队列里选一条，这里会列出审批人需要知道的全部：动作做什么、绑定了哪些哈希、门禁状态、谁不能审批、现在能不能决定。</p>
        <ol className="steps">
          <li>提案人：生成评审，提一个动作</li>
          <li>审阅人：看清参数和后果，批准或拒绝</li>
          <li>执行器：执行已批准的动作，写入回执</li>
        </ol>
      </div>
    );
  }

  const { approval, parameters, gate, independence, can_be_decided: decidable, executed } = explain;
  const isReviewer = identity?.roles.includes("reviewer") ?? false;
  const isExecutor = identity?.kind === "service" && identity.roles.includes("fabric_executor");
  const blockedSelf = identity ? independence.may_not_decide.includes(identity.subject_id) : false;
  const canDecide = isReviewer && decidable.now && !blockedSelf;
  const canExecute = isExecutor && approval.decision === "APPROVED" && !executed;

  return (
    <div className="card detail">
      <div className="detail-head">
        <div>
          <p className="eyebrow">审批 · 第 {approval.version} 版 · revision {approval.revision}</p>
          <h2>{KIND_LABEL[approval.kind]}</h2>
        </div>
        <span className={`chip chip-${executed ? "EXECUTED" : approval.decision}`}>
          {executed ? "已执行" : approval.decision === "PENDING" ? "待审批" : approval.decision === "APPROVED" ? "已批准" : "已拒绝"}
        </span>
      </div>

      <section className="block">
        <h3>如果执行，会发生什么</h3>
        <ul className="effects">
          {describeEffect(explain.if_executed).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <p className="hint">批准只记录结论，不会执行；执行要由执行器单独完成。</p>
      </section>

      <section className="block">
        <h3>这次审批绑定了什么</h3>
        <dl>
          <Row label="提案哈希">
            <Hash value={parameters.proposal_hash} />
          </Row>
          <Row label="评审哈希">
            <Hash value={parameters.review_hash} />
          </Row>
          <Row label="技术证据哈希">
            <Hash value={parameters.technical_hash} />
          </Row>
          <Row label="执行代码">
            {approval.adapter_current ? <span className="ok">与提案时一致</span> : <span className="bad">已变化，审批失效</span>}
          </Row>
        </dl>
        <p className="hint">任何一项变了，审批就作废，必须重新提案；审批人签的是这组哈希，不是界面上的文字。</p>
      </section>

      <section className="block">
        <h3>门禁</h3>
        <div className="gates">
          <span className={`gate gate-${gate.flow_gate}`}>流程门禁 {GATE_LABEL[gate.flow_gate]}</span>
          <span className={`gate gate-${gate.evt_gate}`}>EVT 门禁 {GATE_LABEL[gate.evt_gate]}</span>
          <span className="gate gate-UNKNOWN">完整 EVT 资格 {gate.full_evt_eligibility ? "有" : "无"}</span>
        </div>
      </section>

      <section className="block">
        <h3>谁能决定</h3>
        <dl>
          <Row label="提案人">{approval.created_by}</Row>
          <Row label="不能审批的人">{independence.may_not_decide.join("、") || "—"}</Row>
          <Row label="现在能否决定">
            {approval.decision !== "PENDING" ? (
              <span>已有结论（{approval.decision === "APPROVED" ? "批准" : "拒绝"}），不能再改</span>
            ) : decidable.now ? (
              <span className="ok">能</span>
            ) : (
              <span className="bad">
                不能
                {decidable.reasons.length > 0 && "：" + decidable.reasons.map((r) => REASON_LABEL[r] ?? r).join("、")}
              </span>
            )}
          </Row>
          <Row label="过期时间">{new Date(approval.expires_at).toLocaleString()}</Row>
          {approval.history_versions > 0 && <Row label="历史版本">{approval.history_versions} 个旧版本已保留</Row>}
          {executed && (
            <Row label="回执">
              <span className="mono">{short(executed.receipt_id, 12)}</span>
            </Row>
          )}
        </dl>
      </section>

      <div className="actions">
        <button className="btn approve" disabled={!canDecide || props.busy} onClick={() => props.onDecide("APPROVED")}>
          批准
        </button>
        <button className="btn reject" disabled={!canDecide || props.busy} onClick={() => props.onDecide("REJECTED")}>
          拒绝
        </button>
        <button className="btn execute" disabled={!canExecute || props.busy} onClick={props.onExecute}>
          执行
        </button>
        <p className="hint action-hint">
          {blockedSelf
            ? "你是这条提案的提案人，不能审批。"
            : !isReviewer && approval.decision === "PENDING"
              ? "切到审阅人身份才能批准或拒绝。"
              : approval.decision === "APPROVED" && !executed && !isExecutor
                ? "切到执行器身份才能执行。"
                : ""}
        </p>
      </div>
    </div>
  );
}
