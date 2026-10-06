# S2 词法检索基线：复现与学习

本目录是独立 preparation，不属于已封存的 S1 deliverable。仅调用未改动的
[KnowledgeService](../../../../src/forgeflow/context.py) 与
[Store](../../../../src/forgeflow/store.py)，使用新建后清理的临时 SQLite。
没有读取现有 runtime 配置、私人 key；没有 Docker、网络、LLM 或 embedding 调用。

[dataset.json](../../../../preparation/s2/evaluation/dataset.json) 为 预先指定的合成语料和 gold，尚未经用户逐题
独立标注；它是从 S1 scorer 调优中留出的题集，不是真实硬件或独立采样 benchmark。
47 题及 26 个版本行在第一次检索前已记录 [freeze.json](../../../../preparation/s2/evaluation/freeze.json) hashes。
[protocol.md](protocol.md) 冻结分母、引用/权限/时效指标与 latency 边界。
请保留 misses 和 false positives；不要看到结果后修改 frozen gold。

首次结果见 [RESULTS.md](RESULTS.md)；完整逐题结果和环境见
[runs/20261005T170825-lexical-v1-891375a5/report.json](../../../../preparation/s2/evaluation/runs/20261005T170825-lexical-v1-891375a5/report.json)。
[validation.xml](../../../../preparation/s2/evaluation/validation.xml) 记录 22 个评测框架测试全部通过，包含同 hash 跨 scope
来源、重复 gold、后续 repetition 的越权 trace、伪造更低 classification 等回归。
它们是 S2 evaluator 的测试，不加入 S1 的 136 数字。

在 ForgeFlow 根目录执行：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/evaluation/run_baseline.py validate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/evaluation/run_baseline.py freeze
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/evaluation/run_baseline.py run --label manual
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider preparation/s2/evaluation/test_evaluator.py --basetemp=preparation/s2/evaluation/test-temp -q
.venv/bin/ruff check --no-cache preparation/s2/evaluation
```

`validate` 不执行检索；`freeze` 已存在时只核验，拒绝更改；`run` 总是写新的带时间和随机
后缀的 runs 子目录，保留原结果。脚本检查 S1 manifest 的 95 个文件及 scorer hashes，
在执行前后再次核验冻结输入。冻结后不要格式化 evaluator；如果发现实质 bug，建立新
明确版本并保留旧结果。临时数据库在结束时移除，不接原有 PostgreSQL。测试只使用本
目录的 test-temp；完成后可移除此 task-owned 目录，保留 XML/结果/freeze。

一次查询的执行链是：可信 fixture Principal/full Scope → Store 先判角色与密级并选择
当前版本 → KnowledgeService 词法排序 → 同一来源 citation 重新授权 → evaluator 按实际
返回的 full scope/version/hash/UUID 检查 gold。评测没有把预期 scope 强贴到结果上，
也没有给原始 Store 注入它尚不具备的 conflict metadata。

本人可做三个小练习，不修改 frozen inputs 或 S1 source：

1. 打开 Q02 的两个 gold 与实际 hit ranks，手算 Recall@5 和 MRR@5；再为 Q11 的空结果
   算分。Recall 是找到的相关来源比例，MRR 是第一个相关结果名次的倒数，没找到则为零。
2. 对比 Q33/Q34 与 Q37/Q38：解释为何相同查询在不同 role/scope 中可读或 404；再检查
   Q28 的 EXPIRED 为什么仍能精确引用，却不能成为新的 current 正面依据。
3. 在本目录新建自己的 `practice_summary.py`，只读已有 report.json，汇总 synonym 和
   cross-language 的分母及 misses。修改练习代码前后保留自己的输出，不改变 gold 或
   把本题集当训练集后再宣称它仍独立 heldout。

完成练习说明能手动复现和解释这条检索链，并不自动证明已掌握全部 ML/RAG、Agent
系统设计或生产运行经验。后续 keyword/vector/RRF 比较需要真实、获批的 embedding
接入与相同冻结数据；如果这些题被用于调优，应另设未见过的 heldout 集。
