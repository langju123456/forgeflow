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
