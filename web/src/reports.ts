// The 年报问答 page talks to src/forgeflow/report_desk.py (mounted by the workbench server at /reports).

export type ReportInfo = {
  key: string;
  company: string;
  document: string;
  language: "zh" | "en";
  pages: number;
  url: string;
  state: "ok" | "missing" | "changed";
  rows?: number;
  tables?: number;
};

export type DeskStatus = { state: string; detail: string; embedded: number; rows: number };

export type DeskInfo = {
  reports: ReportInfo[];
  status: DeskStatus;
  mode: "hybrid" | "lexical";
  gate_available: boolean;
  answer_available: boolean;
};

export type Citation = {
  id: string;
  doc: string | null;
  company: string | null;
  kind: "text" | "table" | null;
  page: number | null;
  bbox: number[] | null;
  snippet: string;
};

export type AskResult = {
  question: string;
  mode: string;
  gate: string;
  found: boolean;
  answer: string;
  citations: Citation[];
  kept: Citation[];
  dropped: Citation[];
  answerer: string;
  timings: Record<string, number>;
};

async function json<T>(response: Response): Promise<T> {
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail = data?.detail ?? data;
    throw new Error(detail?.message ?? detail?.code ?? `HTTP ${response.status}`);
  }
  return data as T;
}

export const desk = {
  info: () => fetch("/reports").then((r) => json<DeskInfo>(r)),
  ask: (question: string, gate: boolean) =>
    fetch("/reports/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, gate }),
    }).then((r) => json<AskResult>(r)),
  pageUrl: (c: Citation) =>
    c.doc && c.page ? `/reports/${c.doc}/pages/${c.page}.png${c.bbox ? `?box=${c.bbox.join(",")}` : ""}` : null,
};

/** The stored row starts with "[table] title | ..." or plain text; show the readable part. */
export function snippetLines(snippet: string, max = 4): string[] {
  return snippet
    .replace(/^\[table\]\s*/, "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, max);
}

export function ms(value: number | undefined): string {
  if (value === undefined) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(1)} 秒` : `${Math.round(value)} 毫秒`;
}

export const STATE_TEXT: Record<ReportInfo["state"], string> = {
  ok: "已校验",
  missing: "本地没有文件",
  changed: "文件和登记的指纹不一致",
};
