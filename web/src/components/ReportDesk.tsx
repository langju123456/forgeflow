import { useEffect, useState } from "react";
import { desk, ms, snippetLines, STAGES, stageIndex, STATE_TEXT, WITHDRAWN_TEXT } from "../reports";
import type { AskResult, Citation, DeskInfo, JobProgress } from "../reports";

// Example questions are deliberately not from the evaluation set in preparation/s4/real-reports/questions.json.
const EXAMPLES = [
  "宁德时代 2025 年经营活动产生的现金流量净额是多少？",
  "茅台 2025 年直销渠道的营业收入是多少？",
  "What was Apple's products gross margin percentage in fiscal 2025?",
  "腾讯 2025 年的毛利是多少？",
  "茅台 2025 年在日本的销售收入是多少？",
];

function CitationCard({ c, active, onOpen }: { c: Citation; active: boolean; onOpen: (c: Citation) => void }) {
  return (
    <button className={`evidence${active ? " on" : ""}`} onClick={() => onOpen(c)}>
      <span className="evidence-head">
        <strong>{c.company ?? c.doc}</strong>
        <span className="mono dim">p.{c.page}</span>
        <span className={`chip chip-kind`}>{c.kind === "table" ? "表格" : "正文"}</span>
      </span>
      {snippetLines(c.snippet).map((line, i) => (
        <span key={i} className="evidence-line">
          {line}
        </span>
      ))}
    </button>
  );
}

/** The job's path through the service: 排队 → 检索 → 生成 → 核对完成, with the attempt number when it was retried. */
function JobStrip({ progress }: { progress: JobProgress }) {
  const at = stageIndex(progress.stage);
  return (
    <div className="job-strip" aria-label="进度">
      {STAGES.map((stage, i) => (
        <span key={stage.key} className={`job-step${i < at ? " done" : ""}${i === at ? " now" : ""}`}>
          {stage.label}
        </span>
      ))}
      {progress.attempt > 1 && <span className="chip chip-PENDING">第 {progress.attempt} 次尝试</span>}
      {progress.state === "joined" && <span className="chip chip-APPROVED">同一个问题正在回答，已合并</span>}
    </div>
  );
}

export function ReportDesk() {
  const [info, setInfo] = useState<DeskInfo | null>(null);
  const [offline, setOffline] = useState(false);
  const [question, setQuestion] = useState("");
  // Off by default: on the real annual reports the gate did not meet its pre-registered bar (absent 7/12 -> 8/12).
  const [gate, setGate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResult | null>(null);
  const [open, setOpen] = useState<Citation | null>(null);
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [lastDraft, setLastDraft] = useState("");

  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const next = await desk.info();
        if (stop) return;
        setInfo(next);
        setOffline(false);
        if (next.status.state !== "ready") setTimeout(poll, 2000);
      } catch {
        if (!stop) setOffline(true);
      }
    };
    void poll();
    return () => {
      stop = true;
    };
  }, []);

  const ready = info?.status.state === "ready";

  const ask = async (text: string) => {
    if (!text.trim() || !ready) return;
    setQuestion(text);
    setBusy(true);
    setError(null);
    setOpen(null);
    setResult(null);
    setProgress(null);
    setLastDraft("");
    try {
      let draft = "";
      const answer = info?.service
        ? await desk.askStream(text, gate, (p) => {
            draft = p.draft;
            setProgress(p);
            if (p.kept.length) setOpen((current) => current ?? p.kept[0]);
          })
        : await desk.ask(text, gate);
      setResult(answer);
      setLastDraft(draft);
      setOpen(answer.citations[0] ?? answer.kept[0] ?? null);
    } catch (e) {
      setResult(null);
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  if (offline) return <div className="boot">年报问答没有启动：用 scripts/workbench.sh 启动本地服务（需要 pdf 扩展和 ~/code/finreports 里的年报）。</div>;
  if (!info) return <div className="boot">正在连接本地服务…</div>;

  const pageUrl = open ? desk.pageUrl(open) : null;
  const evidence = result ? (result.citations.length ? result.citations : result.kept) : [];

  return (
    <main className="desk">
      <aside className="side">
        <section className="card">
          <div className="card-head">
            <h3>年报</h3>
            <span className={`gate gate-${ready ? "PASS" : "UNKNOWN"}`}>{ready ? "就绪" : info.status.detail || "加载中"}</span>
          </div>
          <ul className="reports">
            {info.reports.map((r) => (
              <li key={r.key} className={`report report-${r.state}`}>
                <span className="report-name">{r.company}</span>
                <span className="dim">
                  {r.document} · {r.language === "zh" ? "中文" : "英文"} · {r.pages} 页
                </span>
                <span className={r.state === "ok" ? "ok" : "bad"}>
                  {STATE_TEXT[r.state]}
                  {r.rows ? ` · ${r.rows} 条可引用内容` : ""}
                </span>
              </li>
            ))}
          </ul>
        </section>
        <section className="card">
          <h3>检索与核对</h3>
          <dl>
            <div className="kv">
              <dt>检索</dt>
              <dd>{info.mode === "hybrid" ? "混合检索（词面 + bge-m3 语义）" : "只用词面（本机没有 bge-m3）"}</dd>
            </div>
            <div className="kv">
              <dt>能否回答核对</dt>
              <dd>
                <label className="toggle">
                  <input type="checkbox" checked={gate} disabled={!info.gate_available} onChange={(e) => setGate(e.target.checked)} />
                  {info.gate_available ? (gate ? "开：只留下原文写明答案的段落（每题多约 27 秒）" : "关：检索结果直接交给回答") : "不可用（本机没有 qwen3）"}
                </label>
              </dd>
            </div>
            <div className="kv">
              <dt>回答</dt>
              <dd>{info.answer_available ? "本机模型生成，只能引用给它的段落" : "只显示证据（本机没有可用的回答模型）"}</dd>
            </div>
          </dl>
          {info.service && info.queue && (
            <div className="kv">
              <dt>服务</dt>
              <dd>
                队列 {info.queue.waiting}/{info.queue.max} · {info.queue.workers} 个 worker · {info.model}
              </dd>
            </div>
          )}
          <p className="hint">全部在你电脑上运行，年报和问题不会发给外部模型。</p>
        </section>
      </aside>

      <section className="desk-main">
        <form
          className="card ask"
          onSubmit={(e) => {
            e.preventDefault();
            void ask(question);
          }}
        >
          <input
            className="ask-input"
            value={question}
            placeholder={ready ? "问年报里的问题，中文或英文都可以" : "年报加载中…"}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={!ready || busy}
            aria-label="问题"
          />
          <button className="btn" disabled={!ready || busy || !question.trim()}>
            {busy ? "查找中…" : "提问"}
          </button>
          <div className="examples">
            {EXAMPLES.map((text) => (
              <button type="button" key={text} className="example" disabled={!ready || busy} onClick={() => void ask(text)}>
                {text}
              </button>
            ))}
          </div>
        </form>

        {error && <div className="notice notice-error"><strong>没有完成</strong><span>{error}</span></div>}

        {busy && progress && (
          <div className="desk-grid">
            <section className="card">
              <JobStrip progress={progress} />
              {progress.stage === "retrying" && <p className="hint">依赖暂时不可用，稍后自动重试：{progress.retryReason}</p>}
              {progress.draft ? (
                <div className="answer draft">
                  <p className="eyebrow">草稿 · 生成中，还没核对引用</p>
                  <p className="answer-text">
                    {progress.draft}
                    <span className="caret" />
                  </p>
                </div>
              ) : (
                <p className="hint">{progress.stage === "queued" ? "等待 worker…" : progress.stage === "retrieving" ? "检索中…" : "等待模型输出第一个字…"}</p>
              )}
              {progress.kept.length > 0 && <h3 className="section-title">检索到的证据</h3>}
              <div className="evidence-list">
                {progress.kept.map((c) => (
                  <CitationCard key={c.id} c={c} active={open?.id === c.id} onOpen={setOpen} />
                ))}
              </div>
            </section>
            <section className="card page-view">
              {pageUrl ? <img src={pageUrl} alt={`${open?.company} 第 ${open?.page} 页`} /> : <p className="empty">证据出来后可以在这里看原页。</p>}
            </section>
          </div>
        )}

        {result && (
          <div className="desk-grid">
            <section className="card">
              {result.found ? (
                <div className="answer">
                  <p className="eyebrow">回答 · 带页码引用</p>
                  <p className="answer-text">{result.answer}</p>
                  <div className="cites">
                    {result.citations.map((c) => (
                      <button key={c.id} className="cite" onClick={() => setOpen(c)}>
                        {c.company} · p.{c.page}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="answer none">
                  <p className="eyebrow">回答</p>
                  {lastDraft && result.status && WITHDRAWN_TEXT[result.status] && (
                    <p className="hint">生成过草稿“{lastDraft.slice(0, 60)}{lastDraft.length > 60 ? "…" : ""}”：{WITHDRAWN_TEXT[result.status]}</p>
                  )}
                  <p className="answer-text">
                    {result.kept.length && !info.answer_available
                      ? "下面是检索到的证据；本机没有回答模型，所以不生成回答。"
                      : result.status === "UNSUPPORTED" || result.status === "ANSWER_ERROR"
                        ? "没有能展示的回答。"
                        : "年报里没有找到能回答这个问题的内容。"}
                  </p>
                  {result.dropped.length > 0 && <p className="hint">检索到 {result.dropped.length} 段相关内容，但核对后都没有写明答案，见下方“相关但不能回答”。</p>}
                </div>
              )}
              <p className="timing mono dim">
                {result.cached
                  ? "缓存命中，没有调用模型；以下是第一次回答时的耗时 · "
                  : result.timings.queue_ms !== undefined
                    ? `排队 ${ms(result.timings.queue_ms)} · `
                    : ""}
                检索 {ms(result.timings.retrieval_ms)} · 核对 {ms(result.timings.gate_ms)} ·
                {result.timings.first_token_ms !== undefined ? ` 首字 ${ms(result.timings.first_token_ms)} ·` : ""} 回答{" "}
                {ms(result.timings.answer_ms)} · {result.mode === "hybrid" ? "混合检索" : "词面检索"}
                {result.attempt && result.attempt > 1 ? ` · 第 ${result.attempt} 次尝试完成` : ""}
              </p>

              {evidence.length > 0 && <h3 className="section-title">证据</h3>}
              <div className="evidence-list">
                {evidence.map((c) => (
                  <CitationCard key={c.id} c={c} active={open?.id === c.id} onOpen={setOpen} />
                ))}
              </div>
              {result.dropped.length > 0 && (
                <>
                  <h3 className="section-title">相关但不能回答</h3>
                  <div className="evidence-list muted">
                    {result.dropped.map((c) => (
                      <CitationCard key={c.id} c={c} active={open?.id === c.id} onOpen={setOpen} />
                    ))}
                  </div>
                </>
              )}
            </section>
            <section className="card page-view">
              {pageUrl ? (
                <>
                  <p className="eyebrow">
                    {open?.company} · 第 {open?.page} 页 · 高亮是被引用的区域
                  </p>
                  <img src={pageUrl} alt={`${open?.company} 第 ${open?.page} 页`} />
                </>
              ) : (
                <p className="empty">点一条证据，在这里看年报原页。</p>
              )}
            </section>
          </div>
        )}
      </section>
    </main>
  );
}
