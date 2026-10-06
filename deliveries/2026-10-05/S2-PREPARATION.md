# ForgeFlow S2 准备交付 · 2026-10-05

**状态：S2_PREPARATION_COMPLETE；MODEL_NOT_RUN / EMBEDDING_NOT_RUN。** 唯一代码原件继续保留在 [forgeflow](../../README.md)；本轮只新增 [preparation/s2](../../preparation/s2/README.md) 和本交付归档。未改动其他本地工程。没有推理、模型下载、付费 API、凭据配置、全局服务/网络设置变更、外部写入或部署。

## 接入范围已经具体化

首批拟接入 Supervisor + PM 的四次有界生成：plan → PM context proposal → PM report → Supervisor summary。hardware 仍提供 S1 可信确定性检查，模型只分析来源化事实，不填 Gate、不自批或写 Issue。SRE 待独立来源 ACL/policy review；当前 whole-run/source 权限不能通过 Supervisor 复制结果绕过。

[接入契约](../../preparation/s2/01-integration-contract.md) 包含严格 output schema 边界、独立角色 context、source read 与 provider export 两层授权、预算 reservation、lease/cancel/publication fence，以及 dispatch 后失联保持 OUTCOME_UNKNOWN、未知费用不归零且不自动重发。它是待实现的契约，没有把新 adapter、analysis persistence 或 vector schema port 称为已实现。

## 已完成的真实词法基线

输入在首次检索前冻结：26 个 immutable document version rows、4 个完整 scope、47 个问题。37 个非空 gold 包含7个权限阳性对照，另外有3个无答案题与7个 ACL negative；核心内容为排除对照的30题。corpus/gold 为预先编写的合成研发例子，用户未独立标注；不是硬件认证或真实企业数据 benchmark。

使用未修改的 S1 KnowledgeService/Store、临时 SQLite 和 fixture principals，共执行235次 warm search；不接触现有 runtime secrets、Postgres、Docker 或模型。

| 指标 | 真实结果 | 解释 |
|---|---:|---|
| Recall@5 / MRR@5，37题 macro | 78.38% / 78.38% | 正例分母不含空 gold |
| Recall@5 / MRR@5，core30 | 73.33% / 73.33% | 不由7个权限对照抬高内容成绩 |
| micro relevant reference recovery | 32/40，80% | exact scope/id/version/hash |
| citation exact + authorized | 58/58 | 可解析来源与授权，不证明相关性或解释正确 |
| citation temporal CURRENT | 53/58 | 其余5个为 EXPIRED；CURRENT 不保证无冲突或可进入 Gate |
| ACL negative / adversarial / all query leaks | 0/7、0/8、0/47 | 仅本冻结数据观察；不构成完整安全证明 |
| 同 query 权限阳性对照恢复 | 7/7 | 防止把词法不匹配误当权限保护 |
| synonym / cross-language recovery | 各0/4 | 明确保留词法缺陷 |
| required conflict flag coverage | 0/2 | 两组相关文档已检出，但 S1 无 cross-document subject_key/语义冲突推理 |
| 无答案题返回非空合法 context | 1/3 | 检索误报，不是越权；未运行 LLM，不能算回答幻觉/拒答率 |
| warm search P50 / P95 | 1.703ms / 2.729ms | 本机 SQLite，含服务 ACL/引用校验；不是模型延迟或生产吞吐 |

[冻结协议](../../preparation/s2/evaluation/protocol.md)、[输入 freeze](../../preparation/s2/evaluation/freeze.json)、[首轮完整 report](../../preparation/s2/evaluation/runs/20261005T170825-lexical-v1-891375a5/report.json) 与同目录 questions.jsonl 保留逐 query、来源、权限 trace 与测量样本。不能看到结果后改 gold 再声称同一 held-out 评估提升；用于调参后需另一份未触碰的 held-out 集。

## 模型评估仍未运行

[模型输入冻结包](../../preparation/s2/model-evaluation/README.md) 覆盖正常/阻塞/未知事实、冲突/陈旧、注入、PM sandbox/agent approval 拒绝，以及格式、refusal、truncation、timeout、budget、unknown outcome。行为 prompt 和 TEST_HARNESS_ONLY policy/adapter faults 分开。全部 model_result=NOT_RUN，output/trace/metrics 为 null。

完整性校验只证明输入与 gold、scope/grant/hash/profile 一致；没有调用模型、运行新 gateway/adapter 或证明未来 provider 的故障/费用行为。真实调用前仍须完成并另行冻结 system/phase prompt、strict output schema、provider/export/budget profile 和契约 revision，不能以准备文件冒充真实 trace。

## 独立复核与验证

读审发现并在首次词法运行前修正：重复 gold status 计数、仅起始验冻结，以及 citation origin 不可从期望 scope 推造；CURRENT 指标已限定为时效与来源。模型输入的冲突 overlay/gold 不一致、缺 BLOCKED 行为场景、模型 role 范围与 variant 完整校验也在预运行准备中修正。没有根据模型质量结果调答案，因为模型调用为0。

最终 **62 tests passed（0.28s）、0 failure/error/skip，Ruff PASS**；这是22项检索评测器与40项模型输入完整性测试。冻结 hash 与 S1 seal 复核结果见新增 [verification-summary.json](../../preparation/s2/verification-summary.json)；只读审查结论见 [preparation-review.json](../../preparation/s2/preparation-review.json)。本报告不重复或扩大 S1 的136个真实集成测试范围。本次未重跑 S1 live suite，直接核验其95项封存文件未变。

## 最小决定与下一次交付

本机 metadata 已确认 M4/24GB、Ollama0.32.1 和已安装固定 digest qwen3:8b；未测推理性能、实时可用内存或工具/schema能力。推荐先允许本项目调用该 local endpoint，再单独决定是否下载/使用 qwen3-embedding:0.6b。Ollama 当前库页列 embedding约639MB；未安装或下载。[官方库页](https://ollama.com/library/qwen3-embedding:0.6b)

付费 API 是备选，需要独立 API 项目/访问权、secret 注入、synthetic 数据出口与明确总/每run预算；聊天订阅不当作 API 额度。拟议$5/$0.10尚未获批，也没有读取私人 key。详情及官方核验出处见 [模型决定](../../preparation/s2/02-model-decision.md)、[decision-proposal.json](../../preparation/s2/decision-proposal.json)。父任务已经向用户询问路径选择，此处不重复提问。

决定后完成 capability/preflight → 单角色真实 conformance → source/export 与 budget/lease/cancel/unknown 持久实现 → normal/blocked/unknown Supervisor+PM 模型链 → 独立获准后真实 embedding/vector/RRF对照。未经选择保持 NOT_RUN，不使用脚本模板代替模型、不自动 fallback、不新增执行权限或外部副作用。

## 归档与复现

可读 S2 文档快照在本日 `s2-preparation-docs/`；唯一评估器/数据/结果保留 preparation 原件，不另建第二套 backend。新增 [s2-preparation-manifest.json](s2-preparation-manifest.json) 固定本轮原件与文档快照，旧 [S1 manifest](manifest.json) 原封保留。runtime secrets、数据库、.venv、cache、测试临时目录不进入交付。

从工程根执行 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/verify_preparation_seal.py` 可检查两份记录 inventory；手动复现和本人修改练习见 [准备包 README](../../preparation/s2/README.md)。引用精确不等于解释正确，练习完成也不自动证明本人已熟练掌握。
