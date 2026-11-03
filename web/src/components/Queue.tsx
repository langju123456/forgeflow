import { KIND_LABEL, STATE_LABEL, queueState, short } from "../labels";
import type { ApprovalSummary } from "../types";

export function Queue(props: { approvals: ApprovalSummary[]; selected: string | null; onSelect: (id: string) => void }) {
  const waiting = props.approvals.filter((item) => queueState(item) === "PENDING").length;
  return (
    <div className="card">
      <div className="card-head">
        <h2>审批队列</h2>
        <span className="count">{waiting} 条待审批</span>
      </div>
      {props.approvals.length === 0 ? (
        <p className="empty">还没有提案。用提案人身份生成评审，再提一个动作。</p>
      ) : (
        <ul className="queue">
          {props.approvals.map((item) => {
            const state = queueState(item);
            return (
              <li key={item.id}>
                <button
                  className={item.id === props.selected ? "queue-item on" : "queue-item"}
                  onClick={() => props.onSelect(item.id)}
                >
                  <span className={`stripe stripe-${state}`} aria-hidden="true" />
                  <span className="queue-main">
                    <strong>{KIND_LABEL[item.kind]}</strong>
                    <span className="mono dim">{short(item.proposal_hash)}</span>
                  </span>
                  <span className={`chip chip-${state}`}>{STATE_LABEL[state]}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
