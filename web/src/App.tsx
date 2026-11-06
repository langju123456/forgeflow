import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Api, isApiError, loadConfig } from "./api";
import { ExplainPanel } from "./components/ExplainPanel";
import { IdentitySwitch } from "./components/IdentitySwitch";
import { Queue } from "./components/Queue";
import { ReportDesk } from "./components/ReportDesk";
import { ReviewPanel } from "./components/ReviewPanel";
import { explainError } from "./labels";
import type { ApiError, ApprovalSummary, Explain, ReviewSummary, WorkbenchConfig } from "./types";

export type Notice = { tone: "ok" | "error"; title: string; detail?: string };

function ReviewWorkbench({ tabs }: { tabs: ReactNode }) {
  const [config, setConfig] = useState<WorkbenchConfig | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [token, setToken] = useState("dev-proposer");
  const [reviews, setReviews] = useState<ReviewSummary[]>([]);
  const [approvals, setApprovals] = useState<ApprovalSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [explain, setExplain] = useState<Explain | null>(null);
  const [explainError_, setExplainError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  useEffect(() => {
    loadConfig().then(setConfig).catch(() => setBootError("连不上本地服务。先运行 python -m forgeflow.workbench。"));
  }, []);

  const api = useMemo(() => (config ? new Api(config.scope, token) : null), [config, token]);
  const identity = config?.identities.find((item) => item.token === token) ?? null;

  const refresh = useCallback(async () => {
    if (!api) return;
    try {
      const [nextReviews, nextApprovals] = await Promise.all([api.reviews(), api.approvals()]);
      setReviews(nextReviews);
      setApprovals(nextApprovals);
    } catch (error) {
      if (isApiError(error)) setNotice({ tone: "error", title: error.code, detail: explainError(error.code) });
    }
  }, [api]);

  const loadExplain = useCallback(async () => {
    if (!api || !selected) {
      setExplain(null);
      return;
    }
    try {
      setExplain(await api.explain(selected));
      setExplainError(null);
    } catch (error) {
      setExplain(null);
      setExplainError(isApiError(error) ? error : { status: 0, code: "NETWORK", message: String(error) });
    }
  }, [api, selected]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    void loadExplain();
  }, [loadExplain]);

  /** Run one server action, then re-read everything: the server, not the page, decides what the state is now. */
  const act = useCallback(
    async (step: string, done: string, action: () => Promise<unknown>, after?: (result: unknown) => void) => {
      setBusy(true);
      try {
        const result = await action();
        setNotice({ tone: "ok", title: done });
        after?.(result);
      } catch (error) {
        if (isApiError(error)) setNotice({ tone: "error", title: `${step}被拒绝：${error.code}`, detail: explainError(error.code) });
        else setNotice({ tone: "error", title: `${step}没有完成`, detail: String(error) });
      } finally {
        setBusy(false);
        await refresh();
        await loadExplain();
      }
    },
    [refresh, loadExplain],
  );

  if (bootError) return <div className="shell"><header className="topbar">{tabs}</header><div className="boot">{bootError}</div></div>;
  if (!config || !api) return <div className="boot">正在连接本地服务…</div>;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ForgeFlow</span>
          <span className="brand-sub">审阅工作台</span>
          <span className="dev-badge" title="本地合成数据，开发身份">DEV_ONLY · 合成数据</span>
        </div>
        {tabs}
        <IdentitySwitch identities={config.identities} token={token} onChange={setToken} />
      </header>

      {notice && (
        <div className={`notice notice-${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          <strong>{notice.title}</strong>
          {notice.detail && <span>{notice.detail}</span>}
          <button className="link" onClick={() => setNotice(null)} aria-label="关闭提示">
            关闭
          </button>
        </div>
      )}

      <main className="layout">
        <aside className="side">
          <ReviewPanel
            runId={config.run_id}
            reviews={reviews}
            approvals={approvals}
            identity={identity}
            busy={busy}
            onCreateReview={(fixture) => act("生成评审", "评审已生成", () => api.createReview(config.run_id, fixture))}
            onPropose={(review, kind, target) =>
              act("提案", "提案已生成", () => api.propose(review, kind, target), (result) => setSelected((result as { id: string }).id))
            }
          />
          <Queue approvals={approvals} selected={selected} onSelect={setSelected} />
        </aside>
        <section className="main">
          <ExplainPanel
            explain={explain}
            error={explainError_}
            identity={identity}
            busy={busy}
            onDecide={(decision) =>
              explain && act("审批", decision === "APPROVED" ? "已批准" : "已拒绝", () => api.decide(explain, decision))
            }
            onExecute={() => explain && act("执行", "已执行，回执已写入", () => api.execute(explain.approval.id))}
          />
        </section>
      </main>
    </div>
  );
}

type View = "review" | "reports";

function Tabs({ view, onChange }: { view: View; onChange: (view: View) => void }) {
  return (
    <nav className="tabs" aria-label="页面">
      <button className={`tab${view === "review" ? " on" : ""}`} onClick={() => onChange("review")}>
        审阅工作台
      </button>
      <button className={`tab${view === "reports" ? " on" : ""}`} onClick={() => onChange("reports")}>
        年报问答
      </button>
    </nav>
  );
}

export function App() {
  const [view, setView] = useState<View>(() => (window.location.hash === "#reports" ? "reports" : "review"));
  const go = (next: View) => {
    setView(next);
    window.history.replaceState(null, "", next === "reports" ? "#reports" : "#review");
  };
  const tabs = <Tabs view={view} onChange={go} />;
  if (view === "review") return <ReviewWorkbench tabs={tabs} />;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-name">ForgeFlow</span>
          <span className="brand-sub">年报问答</span>
          <span className="dev-badge" title="本机运行，公开年报">DEV_ONLY · 本机 · 公开年报</span>
        </div>
        {tabs}
      </header>
      <ReportDesk />
    </div>
  );
}
