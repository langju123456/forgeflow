import type { Identity } from "../types";

const ROLE_NAME: Record<string, string> = {
  "dev-proposer": "提案人",
  "dev-reviewer": "审阅人",
  "dev-executor": "执行器",
};

const ROLE_HINT: Record<string, string> = {
  "dev-proposer": "发起评审和提案，不能审批自己的提案",
  "dev-reviewer": "只能批准或拒绝，不能提案也不能执行",
  "dev-executor": "服务身份，只执行已批准的动作",
};

export function IdentitySwitch(props: { identities: Identity[]; token: string; onChange: (token: string) => void }) {
  return (
    <div className="identity" role="radiogroup" aria-label="当前身份">
      {props.identities.map((identity) => (
        <button
          key={identity.token}
          role="radio"
          aria-checked={identity.token === props.token}
          className={identity.token === props.token ? "identity-btn on" : "identity-btn"}
          title={ROLE_HINT[identity.token] ?? identity.roles.join(", ")}
          onClick={() => props.onChange(identity.token)}
        >
          <span>{ROLE_NAME[identity.token] ?? identity.subject_id}</span>
          <small>{identity.kind === "service" ? "服务" : "人"}</small>
        </button>
      ))}
    </div>
  );
}
