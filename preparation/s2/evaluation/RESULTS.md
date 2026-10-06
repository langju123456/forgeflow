# 首次原始词法基线结果

运行：`20261005T170825-lexical-v1-891375a5`；完整证据为该目录的
[report.json](runs/20261005T170825-lexical-v1-891375a5/report.json) 和
[questions.jsonl](runs/20261005T170825-lexical-v1-891375a5/questions.jsonl)。
固定数据 digest：`2d559eb4a66bcfbe2202142426bff8cb69222c635c59ed51aefcec24b0509ee3`。
冻结前只验证数据/评测器并进行 read-only review；首检索后没有修改 corpus、gold、
protocol 或 scorer。report 的 benchmark/S1 unchanged 标志均为 true；95 个 S1 files
全部核对。评测器 22 tests、Ruff 均通过。

| 指标 | 实测值 / 分母 | 解释 |
|---|---|---|
| 全部正例 macro Recall@5 / MRR@5 | 78.38% / 78.38%，37 queries | 29 个找到直接相关来源，8 个漏检。 |
| 内容正例 macro Recall@5 / MRR@5 | 73.33% / 73.33%，30 queries | 排除 7 个权限正向 controls，避免其简单精确查询拉高内容质量。 |
| 相关 exact refs 找回比例 | 32/40 = 80% | 多来源问题按真实 gold refs 计数；macro 与 micro 分开。 |
| citation resolves / exact authorized | 58/58 / 58/58 | actual trace origin 与 immutable UUID/full scope/hash/classification 均核验。 |
| returned citation CURRENT 时效状态 | 53/58 = 91.38% | 另外 5 次返回为精确 EXPIRED 来源；此指标不代表相关、无冲突或 Gate 可用。 |
| returned status 正确 / gold status 覆盖 | 58/58 / 32/40 | 条件成功率不掩盖漏检；EXPIRED 状态保持可见。 |
| ACL negatives / adversarial exposures / all-query leaks | 0/7 / 0/8 / 0/47 | 同时 7/7 paired positive controls 找回目标，unknown returned sources = 0。 |
| 测量中的 forbidden candidate / citation observations | 0/3165 / 0/290 | 覆盖每题全部 5 次 measured repetition，不只保留第一次安全 trace。 |
| unanswerable nonempty context | 1/3 | 合法但无关的检索 false positive；另外 2/3 无结果。没有 LLM，不报告回答幻觉率。 |
| required cross-document CONFLICT flags | 0/2 | 两对任务各自找回两个矛盾来源，但真实 Store 没有跨文档 subject_key。 |
| warm search median / nearest-rank p95 / max | 1.703 / 2.729 / 2.989 ms，235 calls | 含服务内 ACL/citation，排除 seeding 与独立 evaluator 核查；新 SQLite/local 单线程。 |
| 重复结果一致 / 预期 scope denial | 47/47 / 3/3 | 这是有限夹具结果，不证明所有权限或生产可靠性。 |

8 个正例 misses 是 Q11–Q14 的英文同义表达和 Q19–Q22 的跨语种表达：synonym 0/4、
cross-language 0/4。它们的语义目标预先写入 gold，但 S1 scorer 只认词面 overlap、
exact phrase/ID，无法把 `startup endurance` 对应到 `boot cycles`，也无法中英转换。
这不是理由去重写 gold 或加入 hash 假语义 vector。

Q31 `EVT antenna accreditation` 无对应来源，但一般词 EVT 会返回合法相关领域资料。
Q26/Q27 找回 100 与 60 cycles 两条 current labnote，却都标 CURRENT；引用可解析与
检测矛盾是不同能力。五次 EXPIRED 返回也与越权泄漏不同，不能把这些数字合并成
“100% 正确回答”或“全部 evidence 可过 Gate”。

该集合较小、合成、预先指定且未经用户独立逐题标注，tags 有重叠；不报告统计
代表性、生产吞吐、真实硬件质量或语义模型效果。下一步需要独立解决语义检索、
显式来源/冲突结构与 unanswerable context 策略，再以相同冻结任务比较，并为调优
后的结论另保留未使用过的 heldout。模型/embedding 权限与接入由父任务另行决定。
