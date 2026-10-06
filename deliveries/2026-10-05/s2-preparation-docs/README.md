# ForgeFlow S2 准备包

这是 S1 之后的安全准备交付。S1 的独立本地真实执行链已封存；本目录新增接入契约、模型选择依据和冻结评估输入，不是 S2 模型运行完成。没有启动推理、下载模型、调用付费 API、配置凭据、修改全局 Ollama/网络设置或执行外部写入。

## 现在可检查什么

- [接入契约](01-integration-contract.md)：先真实 Supervisor + PM 分析，hardware 保留可信确定性检查；SRE 等待来源 ACL/policy 增量审查。模型计划、分析完成、技术 Gate、人工批准与执行 receipt 各自独立。
- [最小模型决定](02-model-decision.md)、[拟议参数](../../../preparation/s2/decision-proposal.json)：推荐复用本机已有的固定 digest `qwen3:8b`。embedding 尚未安装，可单独批准后接入。API 项目、预算和数据出口授权仍未确定。
- [检索评估协议](evaluation/protocol.md)：冻结预先编写的合成 corpus/gold，使用未经修改的 S1 KnowledgeService/Store 实测词法检索；没有 embedding、vector、LLM 或答案质量评分。
- [模型评估输入](model-evaluation/README.md)：Supervisor/PM 行为案例与 policy/adapter 故障输入分开，全部模型结果保留 NOT_RUN。完整性测试通过不代表真实模型或新 adapter 通过。
- [本机只读 metadata](../../../preparation/s2/machine-readiness.json)、[官方资料核验记录](../../../preparation/s2/official-source-checks.json)：机器与已安装模型的事实，不是推理性能或模型能力实测。

评估数量、真实指标、验证命令与独立审查结论见本轮 [每日交付报告](../S2-PREPARATION.md)。S1 的旧 manifest 和证据不重写；新准备包有独立 manifest。

## 手动复现

从唯一工程根目录 `forgeflow/` 执行。只用当前 `.venv`，不读 `.runtime` 配置，不启动数据库、Docker 或模型服务；禁用 bytecode/cache，避免改动 S1 文件。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/verify_s1_seal.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/verify_preparation_seal.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/evaluation/run_baseline.py validate
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/model-evaluation/checkfixtures.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider preparation/s2/evaluation/test_evaluator.py preparation/s2/model-evaluation/test_checkfixtures.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python preparation/s2/evaluation/run_baseline.py run --label manual
```

第二条检查 S1 与本轮准备包的记录 inventory；后续手动运行新添 results 不属于旧 snapshot。最后一条重跑冻结词法基线，每次使用新的 timestamp/UUID results 目录和临时 SQLite；临时数据库完成后清理，保留逐 query 结果。它不更新 gold 或原始结果。不要重新运行 `freeze`、格式化冻结评估器或修改冻结文件来让分数变高；新输入/方法需另立版本并保留旧结果。

## 下一步只需的决定

1. 允许本项目在既有 loopback endpoint 调用本地 `qwen3:8b`，还是选择单独授权的付费 API。
2. 首批数据建议仅限本项目 synthetic；本地模型同样需要限定处理范围。不要粘贴私人 key 或导出私人会话/旧项目资料。
3. 若走本地，embedding 下载/推理可独立批准或延后；若走 API，明确专用项目、secret 注入方式和总/每 run 预算。

父任务已向用户提出模型路径选择，本准备包不重复索取决定。批准后仍按契约完成 capability、source/export ACL、bounded call、schema/citation、预算/lease/cancel/unknown 恢复验收，再报告真实模型职责贡献；不因模型接入自动授予 generated code、Issue/办公写入、阶段推进或部署。

## 简短讲解与本人练习

先将词法检索理解为“有权限的当前来源中，按字词找到证据”；它会漏掉同义表达，也可能返回合法但不相关的材料。精确引用验证只证明来源和授权，不能证明模型解释正确或硬件认证通过。

任选一个 miss，逐条核对 query、gold exact ref、实际 hits，解释为什么没有命中；任选一对 ACL negative/privileged control，解释为何相同 query 的两个 principal 得到不同结果。再解释模型集 M02 缺证据与 M03 冲突/陈旧为何必须保留 UNKNOWN，以及 M10/M12 的费用 UNKNOWN 为何不能填零。可在测试的内存副本修改 PM grant 观察拒绝，勿改冻结原件。这些是练习任务，不是对本人已掌握能力的承诺。
