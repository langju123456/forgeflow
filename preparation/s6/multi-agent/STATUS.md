# S6 多角色编排：进度

按 [README.md](README.md) 的四步走；每做完一步在这里记一条。登记见 [PREREGISTRATION.md](PREREGISTRATION.md)。

## 第一步：初标留出题（draft）— 2026-11-26 完成

- 命令：`bash scripts/mac_multi_agent_eval.sh draft`，年报 PDF 在 `~/code/finreports`（四份，哈希核对通过）。不检索、不调用模型。
- 提交：`b414564`（`heldout-draft.json`、`heldout-candidates.json`、`heldout-checklist.md`）。
- 76 条候选里 70 条在页面文字里定位到；没定位到的 6 条：MH09、CH01、CH02、CH21、CH22、TH14。它们不在草稿里，构成不受影响。
- 草稿 40 题，与登记一致：有答案 28（其中跨语言 8），无答案 12；每份报告 7 道有答案（2 道跨语言）+ 3 道无答案。

| 报告 | 有答案（同语言） | 有答案（跨语言） | 无答案 |
|---|---|---|---|
| 贵州茅台 | MH01 MH02 MH03 MH04 MH05 | MH21 MH22 | MH31 MH32 MH41 |
| 宁德时代 | CH03 CH04 CH05 CH06 CH07 | CH23 CH24 | CH31 CH32 CH41 |
| 腾讯 | TH02 TH13 TH15 TH09 TH11 | TH21 TH22 | TH31 TH32 TH41 |
| 苹果 | AH01 AH02 AH03 AH04 AH05 | AH21 AH22 | AH31 AH32 AH41 |

- 40 题全部是“待核对”。下一步：逐题对着 PDF 原页核对答案的数和页码。

## 第二步：核对留出题的答案页 — 2026-11-26 完成（模型核对，不是人工）

- 没有用 `heldout-checklist.html` 人工点选。答案页由模型逐题对照 PDF 原页文字核对，见 [PREREGISTRATION.md 的修订 1](PREREGISTRATION.md#修订)。
- 有答案题：打开 gold 页，确认答案的数、标签、期间列和单位都在这一页；再在全文里查同一事实印在别处的页。无答案题：全文查相关词，确认没有写出题目问的那个数。
- 结果：40 题全部通过，没有剔除，题目和初标答案都没改。12 题补了 gold 页或同一事实的另一种写法：
  - 同一个数在别页又印了一次：MH01（+33、34、62、107）、MH21（+59）、MH22（+143）、AH04（+50）、AH05（+39）、AH21（+50）、TH02（+130、194、196）、TH21（+130、194、196）；
  - 同一事实换了单位再印：MH03（+16，万元 2,227,467.87）、TH13（+8、130，百万元 136,127）、TH15（+8、130、204、205、263，百万元 47,448）、TH09（+199、209，百万元 130,699）。
  - MH01、MH21 漏页的原因在初标脚本：同一个数在别页印的那一行恰好是首页那一行的子串时，那一页被去重条件漏掉了。初标的去重逻辑没改（改了会重出草稿），冻结的是逐题改好的这份草稿。
- 每题依据的原文片段和结论：[heldout-review.md](heldout-review.md)；给冻结用的标记：`heldout-review.json`（核对人“模型（非人工）”，`how` 写明核对方式）。
- `write_heldout.py freeze` 原来把核对方式写死为“从 heldout-checklist.html 导出”。改成：核对文件里有 `how` 就照它记，没有才用原来的写法；加了一个测试。
  `tests/test_multi_agent_eval.py` 和 `tests/test_report_orchestration.py`：141 通过，8 跳过（本机没装 grpc）。装完 reportlab 后的第一次运行有 1 个失败，没记下是哪一个；之后连跑 5 次都全部通过，没能复现。

## 第三步：冻结（freeze）— 2026-11-26 完成

- 命令：`bash scripts/mac_multi_agent_eval.sh freeze`（用的是第二步提交的 `heldout-review.json`；`~/Downloads` 里没有别的核对文件）。
- 提交：`81c314a`（`heldout.json`、`heldout-freeze.json`、`heldout-draft.json`）。冻结时间 2026-11-26T08:43:49Z，早于在这批题上的任何检索或模型调用。
- `heldout.json` sha256 `ad74e3ef0563a7a0c2547cf96557acd90d397c000f14f21b35f83089b0d251d4`；40 题，有答案 28（跨语言 8），无答案 12。
- `heldout-freeze.json`：`verified_by` 为“模型（非人工）”，`verified_how` 写明是模型对照 PDF 原页核对，不是人工。
- 运行脚本的 `load_heldout()` 已接受这批题（哈希、核对标记、构成都对得上）。

## 第四步（一）：开发集运行（dev）— 2026-11-26 完成

- 开跑前 `ollama ps`：`evidenceloop-judge` 的 `el judge run` 正在用 `qwen3:8b`，等它结束、Ollama 连续一分半没有加载任何模型后才提交。
- 经 mac-bridge 排队，任务 `20261126-091535-s6-arms-dev`，`timeout_minutes` 240（README 要求一个题集 240 分钟）。09:16–10:03 UTC，47 分钟，退出码 0，没有超时。
- 代码 `225b0db`；模型 `forgeflow-qwen3-8b-ctx16k`；结果提交 `256f109`：`runs/arms-20261126T091623Z/report.json`。
- 模型调用 220 次（各不相同），单次中位 7.9 秒、p95 31.7 秒；没有超上下文、没有截断、没有返回思考内容、没有重试。权限计数全部为 0。
- 开发集只用来查流程，按登记不参与判断，作为探索性结果报告：

| 组 | 有答案题答对（同语言 / 跨语言） | 无答案题答对 | 自信答错 | 待核（有答案 / 无答案） | 每题调用（平均） | 每题耗时中位 |
|---|---|---|---|---|---|---|
| single | 13/28（8/20、5/8） | 7/12 | 14 | 0 / 0 | 1.00 | 18.2 秒 |
| self_check | 13/28（8/20、5/8） | 7/12 | 13 | 0 / 0 | 1.73 | 24.5 秒 |
| verify（A） | 12/28（7/20、5/8） | 9/12 | 6 | 4 / 1 | 1.98 | 26.2 秒 |
| plan_verify（B） | 15/28（11/20、4/8） | 9/12 | 5 | 3 / 1 | 2.95 | 29.3 秒 |

- single 与 S4 的单次回答（13/28、7/12、自信答错 14）完全一致。
- 提示词和编排没有改（`pins.json` 未动），下一步直接跑留出题。

## 收件箱

- 2026-11-26：已接入收件箱（mac-bridge `inbox/line5.md`，克隆在 `~/.mac-bridge/work/report-orchestration`）。当时的内容是“目前没有新的决定，照原计划跑完 heldout、按预登记判定并写进 STATUS.md”，与原计划一致，没有新的条目要处理。
