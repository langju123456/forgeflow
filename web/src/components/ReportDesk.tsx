import { useEffect, useState } from "react";
import { desk, ms, snippetLines, STATE_TEXT } from "../reports";
import type { AskResult, Citation, DeskInfo } from "../reports";

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

export function ReportDesk() {
  const [info, setInfo] = useState<DeskInfo | null>(null);
  const [offline, setOffline] = useState(false);
  const [question, setQuestion] = useState("");
  const [gate, setGate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AskResult | null>(null);
  const [open, setOpen] = useState<Citation | null>(null);

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
    try {
      const answer = await desk.ask(text, gate);
      setResult(answer);
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
                  {info.gate_available ? (gate ? "开：只留下原文写明答案的段落" : "关：检索结果直接交给回答") : "不可用（本机没有 qwen3）"}
                </label>
              </dd>
            </div>
            <div className="kv">
              <dt>回答</dt>
              <dd>{info.answer_available ? "本机模型生成，只能引用给它的段落" : "只显示证据（本机没有可用的回答模型）"}</dd>
            </div>
          </dl>
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
                  <p className="answer-text">
                    {result.kept.length && !info.answer_available
                      ? "下面是检索到的证据；本机没有回答模型，所以不生成回答。"
                      : "年报里没有找到能回答这个问题的内容。"}
                  </p>
                  {result.dropped.length > 0 && <p className="hint">检索到 {result.dropped.length} 段相关内容，但核对后都没有写明答案，见下方“相关但不能回答”。</p>}
                </div>
              )}
              <p className="timing mono dim">
                检索 {ms(result.timings.retrieval_ms)} · 核对 {ms(result.timings.gate_ms)} · 回答 {ms(result.timings.answer_ms)} · {result.mode === "hybrid" ? "混合检索" : "词面检索"}
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
