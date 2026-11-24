import type { Citation } from "./reports";

type Operation = { metric: string; formula: string; numerator: number; denominator: number; percent: string | null };
type MetricUpdate = { run_id: string; tool_version: string; citation_ids: string[] };

export type ResearchRevision = {
  draft_id: string;
  version: number;
  note: string;
  evidence: {
    question: string; answer: string; run_id: string;
    tool_version?: string;
    citations: (Citation & { source_hash: string; row_version: string; file_prefix: string })[];
    source: { key: string; document: string; sha256: string; url: string };
    calculation?: { periods: number[]; services: number[]; total_sales: number[]; unit: string; operations: Operation[] } | null;
    metric_updates?: Record<string, MetricUpdate>;
  };
};

type Group = "正文" | "指标" | "原格" | "来源";
type Field = { group: Group; label: string; value: string };
export type Difference = Omit<Field, "value"> & { before: string | null; after: string | null;
  status: "新增" | "移除" | "改动" | "保留" };

function fields(revision: ResearchRevision) {
  const { evidence } = revision;
  const entries = new Map<string, Field>();
  function add(key: string, group: Group, label: string, value: string) {
    entries.set(key, { group, label, value });
  }
  add("question", "正文", "研究问题", evidence.question);
  add("answer", "正文", "工具结论", evidence.answer);
  add("note", "正文", "研究员说明", revision.note);
  const calc = evidence.calculation;
  if (calc) {
    const periods = calc.periods.map((p) => "FY" + p).join(" / ");
    add("periods", "原格", "计算期间（本期／上期）", periods);
    add("unit", "原格", "金额单位", calc.unit);
    calc.periods.forEach((year, i) => {
      add("services:" + year, "原格", "FY" + year + " 服务收入", calc.services[i].toLocaleString("en-US") + " " + calc.unit);
      add("total:" + year, "原格", "FY" + year + " 总收入", calc.total_sales[i].toLocaleString("en-US") + " " + calc.unit);
    });
    for (const op of calc.operations) {
      const value = op.percent === null ? "无定义" : op.percent + "%";
      add("metric:" + op.metric, "指标", op.metric, [
        value + " · " + periods, op.formula,
        "分子 " + op.numerator.toLocaleString("en-US") + " · 分母 " + op.denominator.toLocaleString("en-US") + "（" + calc.unit + "）",
      ].join("\n"));
    }
    for (const [metric, update] of Object.entries(evidence.metric_updates ?? {})) {
      add("recheck:" + metric, "指标", "本次复核 · " + metric,
        ["查表运行 " + update.run_id, "工具 " + update.tool_version, "原格 " + update.citation_ids.join("、")].join("\n"));
    }
  }
  const src = evidence.source;
  add("source", "来源", "年报文件", [src.document, src.key, src.url, "SHA-256 " + src.sha256].join("\n"));
  for (const cite of evidence.citations) {
    const label = cite.id;
    add("location:" + cite.id, "来源", "原表位置 · " + label,
      [cite.doc, cite.company, cite.kind, "PDF 指纹前缀 " + cite.file_prefix,
        "第 " + cite.page + " 页", "坐标 " + (cite.bbox?.join(", ") ?? "未记录"),
        "原格版本 " + cite.row_version, "SHA-256 " + cite.source_hash].join("\n"));
    add("excerpt:" + cite.id, "来源", "原格摘录 · " + label, cite.snippet);
  }
  return entries;
}

export function compareRevisions(previous: ResearchRevision, current: ResearchRevision): Difference[] {
  if (previous.draft_id !== current.draft_id || previous.version >= current.version) {
    throw new Error("请选择同一稿件的两个先后版本。");
  }
  const old = fields(previous);
  const next = fields(current);
  return [...new Set([...old.keys(), ...next.keys()])].map((key) => {
    const before = old.get(key);
    const after = next.get(key);
    const field = (after ?? before)!;
    const status = !before ? "新增" : !after ? "移除" : before.value === after.value ? "保留" : "改动";
    return { group: field.group, label: field.label, before: before?.value ?? null, after: after?.value ?? null, status };
  });
}

/** The server filters events per identity. An author's intermediate, unsubmitted saves can be absent. */
export function priorVisibleVersion(events: { version: number; digest: string }[], current: number) {
  let prior: { version: number; digest: string } | undefined;
  for (const event of events) {
    if (event.version < current && (!prior || event.version > prior.version)) prior = event;
  }
  return prior;
}
