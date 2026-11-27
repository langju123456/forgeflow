// Run using web/node_modules/.bin/vite-node scripts/revision_diff_dev.mjs from the repository root.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { compareRevisions } from "../web/src/revisionDiff.ts";

const root = "preparation/annual-report-workbench/";
function read(version) {
  const file = "apple-review-v" + version + ".json";
  const bytes = readFileSync(root + file);
  return { file, sha256: createHash("sha256").update(bytes).digest("hex"), data: JSON.parse(bytes).payload };
}

// Baseline 1 uses only the two visible conclusion texts, split at Chinese sentence and clause boundaries.
function conclusionChanges(a, b) {
  const split = (s) => s.split(/[。；]/u).filter(Boolean);
  const old = split(a.evidence.answer);
  const next = split(b.evidence.answer);
  return { removed: old.filter((s) => !next.includes(s)), added: next.filter((s) => !old.includes(s)) };
}

// Baseline 2 aligns object fields by name and arrays by index, reporting every leaf, including execution fields.
function leaves(value, path = "", out = new Map()) {
  if (value === null || typeof value !== "object") out.set(path, value);
  else for (const [key, field] of Object.entries(value)) leaves(field, path + "/" + key, out);
  return out;
}
function allChanges(a, b) {
  const left = leaves(a), right = leaves(b);
  return [...new Set([...left.keys(), ...right.keys()])].filter((key) => left.get(key) !== right.get(key))
    .map((path) => ({ path, before: left.get(path) ?? null, after: right.get(path) ?? null }));
}

const first = read(1), second = read(2);
const base = second.data;
function probe(mutate) {
  const p = structuredClone(base);
  p.version++;
  mutate(p);
  return p;
}
const pairs = [
  ["公开年报已保存的 v1 与 v2", "public-dev", first.data, base],
  ["保留显示值，仅扰动分母", "synthetic", base, probe((p) => { p.evidence.calculation.operations[0].denominator++; })],
  ["保留结论，仅移动原表定位", "synthetic", base, probe((p) => { p.evidence.citations[0].bbox[0]++; })],
  ["重新执行相同内容", "synthetic", base, probe((p) => { p.evidence.run_id = "derived-run"; })],
];
const runs = pairs.map(([case_name, kind, a, b]) => {
  const semantic = compareRevisions(a, b);
  return { case_name, kind, model_calls_per_strategy: 0, external_cost: 0,
    text: conclusionChanges(a, b), json: allChanges(a, b),
    review: { changes: semantic.filter((r) => r.status !== "保留"), retained_metrics: semantic.filter((r) => r.group === "指标" && r.status === "保留") } };
});
console.log(JSON.stringify({ schema_version: 1, case_id: "apple-services-growth-contribution-dev",
  base_commit: "b74c8bb439658d1530a762391e13ae1861bd8968",
  inputs: [first, second].map(({ file, sha256 }) => ({ file, sha256 })), runs }, null, 2));
