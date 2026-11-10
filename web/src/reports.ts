// The 年报问答 page talks to src/forgeflow/report_desk.py (mounted by the workbench server at /reports), or,
// when it is served by the desk service (python -m forgeflow.desk_service all), to its gateway at /v2: the
// question becomes a job and the page follows the job's events (SSE) as they arrive.

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
  service?: boolean;
  model?: string;
  queue?: { waiting: number; workers: number; max: number };
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
  status?: string;
  cached?: boolean;
  attempt?: number;
  rejected_citations?: string[];
};

/** What the page shows while a service job runs; rebuilt from the job's events. */
export type JobProgress = {
  state: "cached" | "joined" | "queued";
  stage: "queued" | "retrieving" | "answering" | "checking" | "retrying" | "done";
  attempt: number;
  draft: string;
  kept: Citation[];
  retryReason: string | null;
  startedAt: number;
  firstTokenAt: number | null;
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

let serviceMode = false;

async function info(): Promise<DeskInfo> {
  const service = await fetch("/v2/reports");
  if (service.ok) {
    serviceMode = true;
    return json<DeskInfo>(service);
  }
  serviceMode = false;
  return fetch("/reports").then((r) => json<DeskInfo>(r));
}

/**
 * Ask through the desk service: POST creates (or joins, or answers from cache) a job, then the job's events
 * arrive over SSE. EventSource reconnects by itself and resumes with Last-Event-ID, so a dropped connection
 * loses no events. A draft is only a draft: the final event replaces it with the checked answer.
 */
async function askStream(question: string, gate: boolean, onProgress: (p: JobProgress) => void): Promise<AskResult> {
  const accepted = await fetch("/v2/reports/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, gate }),
  }).then((r) => json<{ job_id: string; state: JobProgress["state"]; events: string }>(r));
  const progress: JobProgress = {
    state: accepted.state, stage: "queued", attempt: 0, draft: "", kept: [], retryReason: null,
    startedAt: performance.now(), firstTokenAt: null,
  };
  const push = (change: Partial<JobProgress>) => {
    Object.assign(progress, change);
    onProgress({ ...progress });
  };
  push({});
  return new Promise<AskResult>((resolve, reject) => {
    const source = new EventSource(accepted.events);
    const data = (e: Event) => JSON.parse((e as MessageEvent).data);
    source.addEventListener("started", (e) => {
      const d = data(e);
      // A new attempt (retry or redelivery after a worker died) starts over: drop the old draft.
      push({ stage: "retrieving", attempt: d.attempt, draft: "", kept: [], retryReason: null });
    });
    source.addEventListener("retrieved", (e) => push({ stage: "answering", kept: data(e).kept }));
    source.addEventListener("token", (e) => {
      if (progress.firstTokenAt === null) progress.firstTokenAt = performance.now();
      push({ draft: progress.draft + data(e).text });
    });
    source.addEventListener("retrying", (e) => push({ stage: "retrying", retryReason: data(e).reason }));
    source.addEventListener("final", (e) => {
      source.close();
      push({ stage: "done" });
      resolve(data(e) as AskResult);
    });
    source.addEventListener("error", (e) => {
      const raw = (e as MessageEvent).data;
      if (raw === undefined) return; // a network hiccup: EventSource reconnects with Last-Event-ID
      source.close();
      const d = JSON.parse(raw);
      reject(new Error(`${d.code}：${d.message ?? ""}`));
    });
  });
}

export const desk = {
  info,
  isService: () => serviceMode,
  askStream,
  ask: (question: string, gate: boolean) =>
    fetch("/reports/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, gate }),
    }).then((r) => json<AskResult>(r)),
  pageUrl: (c: Citation) =>
    c.doc && c.page
      ? `${serviceMode ? "/v2" : ""}/reports/${c.doc}/pages/${c.page}.png${c.bbox ? `?box=${c.bbox.join(",")}` : ""}`
      : null,
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

export const STAGES: { key: JobProgress["stage"]; label: string }[] = [
  { key: "queued", label: "排队" },
  { key: "retrieving", label: "检索" },
  { key: "answering", label: "生成" },
  { key: "done", label: "核对完成" },
];

export function stageIndex(stage: JobProgress["stage"]): number {
  if (stage === "retrying") return 1;
  if (stage === "checking") return 2;
  return STAGES.findIndex((s) => s.key === stage);
}

export const WITHDRAWN_TEXT: Record<string, string> = {
  UNSUPPORTED: "草稿引用了没有给它的段落，核对后撤回。",
  NOT_FOUND: "模型判断这些段落没有写明答案。",
  ANSWER_ERROR: "模型输出不合格（不是要求的格式或被截断），没有当作回答。",
};

export const STATE_TEXT: Record<ReportInfo["state"], string> = {
  ok: "已校验",
  missing: "本地没有文件",
  changed: "文件和登记的指纹不一致",
};
