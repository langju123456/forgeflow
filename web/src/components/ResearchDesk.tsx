import { useState } from "react";
import type { AskResult, DeskInfo } from "../reports";
import { desk } from "../reports";
import { compareRevisions, priorVisibleVersion } from "../revisionDiff";
import type { Difference, ResearchRevision } from "../revisionDiff";

type Identity = { subject: string; role: "author" | "reviewer" };
type Summary = { id: string; question: string; owner: string; reviewer: string | null; version: number; head: string; state: string };
type Event = { seq: number; version: number; digest: string; kind: string; actor: string; memo: string; at: string };
type Draft = Summary & { payload: ResearchRevision; events: Event[]; local_receipt: string | null };
type Comparison = { version: number; rows: Difference[]; lastReturn?: string };

const states: Record<string, string> = { draft: "草稿", saved: "保存", submitted: "待审阅", returned: "已退回",
  approved: "已批准", delivered: "已本地交付" };

async function request(token: string, path: string, body?: unknown) {
  const response = await fetch("/research/" + path, { method: body === undefined ? "GET" : "POST",
    cache: "no-store", headers: { Authorization: "Bearer " + token, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail?.message ?? "请求失败，请检查研究库和当前身份。");
  }
  return response;
}

function Changes({ comparison, draft }: { comparison: Comparison; draft: Draft }) {
  const changed = comparison.rows.filter((r) => r.status !== "保留");
  const retained = comparison.rows.filter((r) => r.status === "保留");
  const sourceChanged = changed.some((r) => r.group === "来源");
  const pages = [...new Set(draft.payload.evidence.citations.map((c) => c.page).filter((p) => p !== null))];
  const visible = [...changed.filter((r) => r.group === "指标"), ...retained.filter((r) => r.group === "指标"),
    ...changed.filter((r) => r.group !== "指标")];
  const other = retained.filter((r) => r.group !== "指标");
  return <section className="revision-comparison" aria-label="稿件版本对照">
    <h4>第 {comparison.version} 版与第 {draft.version} 版对照</h4>
    {comparison.lastReturn && <p>上次退回意见　{comparison.lastReturn}</p>}
    <p>{sourceChanged ? "来源或原格摘录有变化，请核对本版原表。" : "来源与原格摘录一致。"}
      {pages.length > 0 && " 本版引用第 " + pages.join("、") + " 页。"}</p>
    {!changed.length && <p>两版的正文、财务指标、原格和来源一致。</p>}
    {visible.length > 0 && <div className="revision-scroll"><table aria-label="字段对照">
      <thead><tr><th scope="col">字段</th><th scope="col">第 {comparison.version} 版</th><th scope="col">第 {draft.version} 版</th></tr></thead>
      <tbody>{visible.map((r) => <tr key={r.group + r.label}>
        <th scope="row"><span>{r.status}</span> {r.label}</th>
        <td>{r.before === null ? "—" : r.before || "（空白）"}</td>
        <td>{r.after === null ? "—" : r.after || "（空白）"}</td>
      </tr>)}</tbody></table></div>}
    {other.length > 0 && <details><summary>查看其余 {other.length} 项相同字段</summary>
      <dl>{other.map((r) => <div key={r.group + r.label}><dt>{r.label}</dt><dd>{r.after || "（空白）"}</dd></div>)}</dl>
    </details>}
  </section>;
}

function Session({ token, user, initial, result, info, logout }: { token: string; user: Identity; initial: Summary[];
  result: AskResult | null; info: DeskInfo | null; logout: () => void }) {
  const [list, setList] = useState(initial);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [note, setNote] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [memo, setMemo] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const isAuthor = user.role === "author";
  const from = result?.found && ["apple-services-v1", "annual-cash-v1"].includes(result.answerer)
    ? result.research_grant : null;

  function open(data: Draft) {
    setComparison(null);
    setDraft(data); setNote(data.payload.note); setReviewer(data.reviewer ?? ""); setMemo("");
  }
  async function act(task: () => Promise<void>) {
    setBusy(true); setMessage("");
    try { await task(); } catch (e) { setMessage(e instanceof Error ? e.message : "操作失败"); }
    finally { setBusy(false); }
  }
  async function refresh() {
    const next: Summary[] = await (await request(token, "drafts")).json();
    setList(next);
    if (draft && next.some((item) => item.id === draft.id)) {
      open(await (await request(token, "drafts/" + draft.id)).json());
    } else { setDraft(null); }
  }
  async function change(path: string, body: unknown) {
    const updated: Draft = await (await request(token, path, body)).json();
    open(updated); setList((items) => [updated, ...items.filter((d) => d.id !== updated.id)]);
  }
  function decision(action: string) {
    if (draft) void act(() => change("drafts/" + draft.id + "/decisions", { expected: draft.head, action, reviewer: reviewer || null, memo }));
  }
  async function download(version: number, fmt: "json" | "html") {
    if (!draft) return;
    const r = await request(token, "drafts/" + draft.id + "/versions/" + version + "/export/" + fmt);
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url; a.download = "research-" + draft.id + "-v" + version + "." + fmt; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const source = draft?.payload.evidence.source;
  const currentSource = info?.reports.find((r) => r.key === source?.key && r.state === "ok" && r.sha256 === source?.sha256);
  const versions = [...new Set(draft?.events.map((event) => event.version) ?? [])].reverse();
  const prior = draft && priorVisibleVersion(draft.events, draft.version);
  async function compare() {
    if (!draft || !prior) return;
    const path = "drafts/" + draft.id + "/versions/" + prior.version + "/export/json";
    const record = await (await request(token, path)).json();
    if (record.digest !== prior.digest || record.payload?.version !== prior.version ||
        record.payload.draft_id !== draft.id || record.payload.schema_version !== "forgeflow.research.revision.v1") {
      throw new Error("历史版本与当前稿件记录不匹配，请刷新后再对照。");
    }
    const rows = compareRevisions(record.payload, draft.payload);
    const lastReturn = draft.events.find((e) => e.version === prior.version && e.kind === "returned")?.memo;
    setComparison({ version: prior.version, rows, lastReturn });
  }
  return <>
    <p>{user.subject} · {isAuthor ? "研究员" : "审阅人"} <button onClick={logout}>退出身份</button></p>
    <fieldset disabled={busy}>
      <button onClick={() => void act(refresh)}>刷新稿件</button>
      {list.map((item) => <button key={item.id} onClick={() => void act(async () => open(await (await request(token, "drafts/" + item.id)).json()))}>
        {item.question} · 第 {item.version} 版 · {states[item.state]}
      </button>)}
      {isAuthor && <div><label>研究员说明<textarea aria-label="研究员说明" value={note} onChange={(e) => setNote(e.target.value)} maxLength={4000}/></label>
        {!from && <p>请先用当前身份查询年度现金流或 Apple 服务收入，再保存本次结果。</p>}
        <button disabled={!from} onClick={() => void act(() => change("drafts", { grant_id: from, note }))}>用本次查表新建稿件</button>
        {draft && draft.state !== "submitted" && <button disabled={!from} onClick={() => void act(() => change("drafts/" + draft.id + "/revisions", { expected: draft.head, grant_id: from, note }))}>用本次查表保存新版</button>}
      </div>}
      {draft && <article aria-label="研究稿详情">
        <h3>第 {draft.version} 版 · {states[draft.state]}</h3>
        <p>{draft.payload.evidence.question}</p><p>{draft.payload.evidence.answer}</p>
        {draft.payload.note && <p>研究员说明　{draft.payload.note}</p>}
        {prior && <button onClick={() => comparison ? setComparison(null) : void act(compare)} aria-expanded={!!comparison}>
          {comparison ? "收起版本对照" : "与第 " + prior.version + " 版对照"}
        </button>}
        {comparison && <Changes comparison={comparison} draft={draft} />}
        <p>版本摘要 <code>{draft.head}</code></p>
        {draft.local_receipt && <p>本地交付回执 <code>{draft.local_receipt}</code></p>}
        <ul>{draft.payload.evidence.citations.map((c) => {
          const page = currentSource && desk.pageUrl(c);
          return <li key={c.id}>{page ? <a href={page} target="_blank" rel="noreferrer">查看第 {c.page} 页原表</a> : <span>第 {c.page} 页 · 当前 PDF 未核对</span>}
            <details><summary>查看已保存的原格摘录</summary><pre>{c.snippet}</pre></details></li>;
        })}</ul>
        <ul>{draft.events.map((e) => <li key={e.seq}>第 {e.version} 版 · {e.actor} · {states[e.kind]} {e.memo}</li>)}</ul>
        {isAuthor && draft.state === "draft" && <><label>指派审阅人<input value={reviewer} onChange={(e) => setReviewer(e.target.value)}/></label>
          <button disabled={!reviewer.trim()} onClick={() => decision("submit")}>提交此版审阅</button></>}
        {!isAuthor && draft.state === "submitted" && <><label>审阅意见<textarea value={memo} onChange={(e) => setMemo(e.target.value)} maxLength={2000}/></label>
          <button onClick={() => decision("approve")}>批准此版</button><button disabled={!memo.trim()} onClick={() => decision("return")}>退回修改</button></>}
        {isAuthor && draft.state === "approved" && <button onClick={() => decision("deliver")}>登记本地交付</button>}
        {versions.map((version) => <p key={version}>第 {version} 版　<button onClick={() => void act(() => download(version, "html"))}>下载阅读稿</button>{" "}
          <button onClick={() => void act(() => download(version, "json"))}>下载证据清单</button></p>)}
      </article>}
    </fieldset>
    {message && <p role="alert">{message}</p>}
  </>;
}

export function ResearchDesk({ result, info, onIdentity }: { result: AskResult | null; info: DeskInfo | null; onIdentity: (token?: string) => void }) {
  const [entry, setEntry] = useState("");
  const [session, setSession] = useState<{ token: string; user: Identity; drafts: Summary[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function login() {
    setBusy(true); setError("");
    const token = entry.trim(); setEntry("");
    try {
      const [user, drafts] = await Promise.all(["me", "drafts"].map(async (route) => (await request(token, route)).json()));
      setSession({ token, user, drafts });
      onIdentity(user.role === "author" ? token : undefined);
    } catch (e) { setError(e instanceof Error ? e.message : "身份无效"); }
    finally { setBusy(false); }
  }
  return <section className="card research" aria-label="研究稿审阅">
    <h2>研究稿与审阅</h2>
    {session ? <Session key={session.user.subject} {...session} initial={session.drafts} result={result} info={info}
      logout={() => { setSession(null); onIdentity(); setError(""); }}/>: <form onSubmit={(e) => { e.preventDefault(); void login(); }}>
      <label>本机身份凭证<input type="password" autoComplete="off" value={entry} onChange={(e) => setEntry(e.target.value)} /></label>
      <button disabled={busy || !entry.trim()}>打开我的稿件</button>
      {error && <p role="alert">{error}</p>}
    </form>}
  </section>;
}
