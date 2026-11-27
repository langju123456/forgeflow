# 多指标研究稿的局部复核

## 决策

Apple FY2025 的完整研究稿包含两个已核指标：服务收入增速和服务对公司收入增量的贡献。局部复核不允许手工覆盖数值；研究员必须先运行同一来源、同一期间的窄问题，再选择“复核本次指标并保留其余指标”。系统以指标名称替换本次查表返回的操作，保留父版本的其余操作、备注、原格和 PDF 指纹，并记录被复核指标对应的新运行号和原格 ID。

候选方案是：(1) 继续将窄查询作为整稿替换；(2) 接受任意 JSON 片段合并；(3) 同源结构化指标的受限合并。选择 (3)：(1) 会移除未改指标，(2) 会绕过来源及公式核对；(3) 只接受 `apple-services-v1`、相同 PDF 摘要、期间、单位及原始金额，并仍由整份新版摘要送审。

## 数据边界

公开开发数据为 Apple FY2025 Form 10-K 第 26、39 页的 Services 109,158 / 96,169 和 Total net sales 416,161 / 391,035（百万美元）。计算为服务收入增速 `12,989 / 96,169 = 13.51%`，增长贡献 `12,989 / 25,126 = 51.70%`。本轮不调用模型、不下载资料、不产生外部费用。脚本中的 author/reviewer 是两个本机脚本身份，不等于独立真人试用。

## 审批与交付

父版本摘要是新版本 `parent_digest`，但批准始终要求当前整稿 `head`。脚本明确尝试让审阅人批准旧摘要，预期 HTTP 409；随后仅批准当前摘要并登记本地交付。导出的 JSON/HTML 使用既有 schema、`draft_id`、version、digest、approved/delivered 事件和 receipt，供跨仓读取。

## 复现

在仓库根目录运行：

```text
PYTHONPATH=src python scripts/multi_metric_review_dev.py /Users/ju.l/code/finreports /tmp/forgeflow-multi-metric --cache /tmp/forgeflow-review-cache
```

`--cache` 是已由同一 PDF 指纹生成的本机解析缓存；省略它会重新解析已登记 PDF。输出 `multi-metric-manifest.json`、两份 JSON 和当前 HTML。该命令不是本轮已完成独立使用证明。
