import { describe, expect, it } from "vitest";
import firstArchive from "../../preparation/annual-report-workbench/apple-review-v1.json?raw";
import secondArchive from "../../preparation/annual-report-workbench/apple-review-v2.json?raw";
import { compareRevisions, priorVisibleVersion } from "./revisionDiff";
import type { ResearchRevision } from "./revisionDiff";

const old: ResearchRevision = JSON.parse(firstArchive).payload;
const current: ResearchRevision = JSON.parse(secondArchive).payload;
const changed = (a: ResearchRevision, b: ResearchRevision) => compareRevisions(a, b).filter((r) => r.status !== "保留");

describe("公开 Apple 修订", () => {
  it("区分移除的增速与保留的贡献，显示各自的两种分母", () => {
    const rows = compareRevisions(old, current);
    expect(rows.find((r) => r.label === "服务收入增速")).toMatchObject({ status: "移除", after: null });
    expect(rows.find((r) => r.label === "服务收入增速")?.before).toContain("分母 96,169");
    const contribution = rows.find((r) => r.label === "服务对公司收入增量的贡献");
    expect(contribution?.status).toBe("保留");
    expect(contribution?.after).toContain("分母 25,126");
    expect(rows.filter((r) => r.group === "来源").every((r) => r.status === "保留")).toBe(true);
  });
  it("重排原格引用或公式不会制造内容变更", () => {
    const newer = structuredClone(old);
    newer.version++;
    newer.evidence.citations.reverse();
    newer.evidence.calculation!.operations.reverse();
    expect(changed(old, newer)).toEqual([]);
  });
  it("百分比显示相同时仍能发现分母变化；来源摘要、坐标与原格拆开核对", () => {
    const newer = structuredClone(current);
    newer.version++;
    newer.evidence.calculation!.operations[0].denominator++;
    newer.evidence.citations[0].bbox![0]++;
    newer.evidence.source.sha256 = "1".repeat(64);
    expect(changed(current, newer).map((r) => r.group).sort()).toEqual(["指标", "来源", "来源"]);
    newer.evidence.calculation!.operations[0].percent = null;
    expect(changed(current, newer).find((r) => r.group === "指标")?.after).toContain("无定义");
  });
  it("按年度比原格，可比较没有计算对象的历史现金流稿", () => {
    const newer = structuredClone(current);
    newer.version++;
    newer.evidence.calculation!.services[1]++;
    expect(changed(current, newer).map((r) => r.label)).toEqual(["FY2024 服务收入"]);
    newer.evidence.calculation = null;
    expect(changed(current, newer).find((r) => r.group === "指标")?.status).toBe("移除");
    expect(changed(newer, { ...newer, version: newer.version + 1, note: "" })).toMatchObject([{ label: "研究员说明", after: "" }]);
  });
  it("原格指向另一份 PDF 指纹前缀时提示来源变化", () => {
    const newer = structuredClone(current);
    newer.version++;
    newer.evidence.citations[0].file_prefix = "a".repeat(16);
    expect(changed(current, newer)).toMatchObject([{ group: "来源", status: "改动" }]);
  });
  it("两个百分比与金额刚好都相同，期间平移仍是指标变更", () => {
    const newer = structuredClone(current);
    newer.version++;
    newer.evidence.calculation!.periods = [2024, 2023];
    const metric = changed(current, newer).find((r) => r.group === "指标");
    expect(metric?.before).toContain("FY2025 / FY2024");
    expect(metric?.after).toContain("FY2024 / FY2023");
  });
  it("局部复核即使数值未变，也标出被重新核对的指标而保留其他指标", () => {
    const newer = structuredClone(old);
    newer.version++;
    newer.evidence.metric_updates = {
      "服务对公司收入增量的贡献": { run_id: "fresh-run", tool_version: "apple-services-v1", citation_ids: ["page-39"] },
    };
    const rows = compareRevisions(old, newer);
    expect(rows.find((r) => r.label === "本次复核 · 服务对公司收入增量的贡献")).toMatchObject({ status: "新增" });
    expect(rows.find((r) => r.label === "服务收入增速")?.status).toBe("保留");
    expect(rows.find((r) => r.label === "服务对公司收入增量的贡献")?.status).toBe("保留");
  });
  it("只选当前身份可见的较早版本，不能混用其他稿件", () => {
    const events = [1, 3, 1, 3].map((version) => ({ version, digest: "digest-" + version }));
    expect(priorVisibleVersion(events, 3)).toEqual(events[0]);
    expect(priorVisibleVersion(events, 1)).toBeUndefined();
    expect(() => compareRevisions(old, { ...current, draft_id: "another" })).toThrow();
    expect(() => compareRevisions(current, old)).toThrow();
  });
});
