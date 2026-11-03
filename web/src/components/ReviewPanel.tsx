import { useMemo, useState } from "react";
import { GATE_LABEL, KIND_LABEL, REVERSES, short } from "../labels";
import type { ActionKind, ApprovalSummary, Fixture, ForwardKind, Identity, ReverseKind, ReviewSummary } from "../types";

const FIXTURES: { id: Fixture; label: string }[] = [
  { id: "aligned", label: "证据一致" },
  { id: "bom-mismatch", label: "BOM 不一致" },
  { id: "missing-exported", label: "缺导出文件" },
];

const FORWARD: ForwardKind[] = ["TEST_STAGE", "ISSUE", "MEMORY_ACTIVATE"];
const REVERSE: ReverseKind[] = ["STAGE_REVERT", "ISSUE_RETRACT", "MEMORY_REVOKE"];

export function ReviewPanel(props: {
  runId: string;
  reviews: ReviewSummary[];
  approvals: ApprovalSummary[];
  identity: Identity | null;
  busy: boolean;
  onCreateReview: (fixture: Fixture) => void;
  onPropose: (review: ReviewSummary, kind: ActionKind, target?: string) => void;
}) {
  const [fixture, setFixture] = useState<Fixture>("aligned");
  const [reviewId, setReviewId] = useState<string | null>(null);
  const isProposer = props.identity?.roles.includes("supervisor") ?? false;
  const review = props.reviews.find((item) => item.id === reviewId) ?? props.reviews[0] ?? null;

  // A compensation needs an executed receipt of the action it reverses.
  const receipts = useMemo(() => {
    const byKind = new Map<ActionKind, ApprovalSummary[]>();
    for (const item of props.approvals) {
      if (!item.receipt_id) continue;
      byKind.set(item.kind, [...(byKind.get(item.kind) ?? []), item]);
    }
    return byKind;
  }, [props.approvals]);

  return (
    <div className="card">
      <div className="card-head">
        <h2>评审与提案</h2>
        <span className="mono dim" title={props.runId}>
          run {short(props.runId, 8)}
        </span>
      </div>

      <div className="row">
        <label className="field">
          <span>证据场景</span>
          <select id="fixture" value={fixture} onChange={(event) => setFixture(event.target.value as Fixture)}>
            {FIXTURES.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <button className="btn" disabled={!isProposer || props.busy} onClick={() => props.onCreateReview(fixture)}>
          生成评审
        </button>
      </div>
      {!isProposer && <p className="hint">评审和提案要用提案人身份。</p>}

      {props.reviews.length > 0 && (
        <ul className="reviews">
          {props.reviews.map((item) => (
            <li key={item.id}>
              <button
                className={item.id === review?.id ? "review on" : "review"}
                onClick={() => setReviewId(item.id)}
              >
                <span>{FIXTURES.find((f) => f.id === item.fixture_id)?.label ?? item.fixture_id}</span>
                <span className={`gate gate-${item.flow_gate}`}>流程 {GATE_LABEL[item.flow_gate]}</span>
                <span className={`gate gate-${item.evt_gate}`}>EVT {GATE_LABEL[item.evt_gate]}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {review && (
        <div className="propose">
          <p className="hint">
            对评审 <span className="mono">{short(review.report_hash)}</span> 提出动作。提案会绑定评审哈希、证据和执行代码的指纹。
          </p>
          <div className="kinds">
            {FORWARD.map((kind) => (
              <button key={kind} className="btn ghost" disabled={!isProposer || props.busy} onClick={() => props.onPropose(review, kind)}>
                {KIND_LABEL[kind]}
              </button>
            ))}
          </div>
          <div className="kinds">
            {REVERSE.map((kind) => {
              const target = receipts.get(REVERSES[kind])?.[0];
              return (
                <button
                  key={kind}
                  className="btn ghost"
                  disabled={!isProposer || props.busy || !target}
                  title={target ? `撤回回执 ${short(target.receipt_id)}` : "要先有一个已执行的对应动作"}
                  onClick={() => target && props.onPropose(review, kind, target.receipt_id ?? undefined)}
                >
                  {KIND_LABEL[kind]}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
