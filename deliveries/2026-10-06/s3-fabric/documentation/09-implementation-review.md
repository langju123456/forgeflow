# S1 实现审查 — 2026-10-05

## 结论与审查边界

**当前可接受的交付是 trusted-template、DEV_ONLY 的真实本地确定性执行链。** PostgreSQL 持久状态、独立进程 MCP、隔离 Semgrep、Docker 检查、确定性 Gate、审批接口、本地 Issue/阶段回执和来源化 Memory 已有代码及实跑记录。本轮报告的原 P1 已在 canonical 代码中修正；最终差量审查也未发现新的未解决 P1。这是限定范围的实现审查结论，不是任意代码安全或生产可用证明。

本审查读取 `src/forgeflow/`、已有设计文档及归档证据，没有重跑测试、启动服务、访问凭据或进行外部写入；唯一新增文件为本文。运行结论来自已保存的验收记录，不把 reviewer 的静态阅读描述成独立运行验证。

本次交付简称 S1，实际包含原 [06 执行计划](06-execution-plan.md) 中部分 S2 的本地能力；这不将 S3–S6 改名为已完成。设计阶段的“尚未实现”快照保留于当日 `docs/` 归档；integrator 已更新 canonical README 和 docs/01–06 的当前状态。**当前实现状态以 DELIVERY、本文、docs/04、07–08 和最新验收证据为准；最终需求仍由 docs/01–06 保留。** reviewer 只新增本文，后续状态更正由 integrator 完成。

## 已有证据能够支持的事实

| 证据 | 可支持的结论 | 不能扩大的结论 |
|---|---|---|
| [最终 pytest.xml](../deliveries/2026-10-05/implementation-evidence/pytest.xml) | XML 记录 `tests=136, errors=0, failures=0, skipped=0`；suite 开始时间为 `2026-10-06T00:29:28.987200+08:00`，即 UTC 的 Oct 5。 | 测试数量不等于所有最终功能完成；也不证明负载、所有攻击或长期运行。 |
| [pass](../deliveries/2026-10-05/implementation-evidence/chain-pass.json)、[blocked](../deliveries/2026-10-05/implementation-evidence/chain-blocked.json)、[unknown](../deliveries/2026-10-05/implementation-evidence/chain-unknown.json)、[timeout](../deliveries/2026-10-05/implementation-evidence/chain-timeout.json) | 真正本地 PG→Semgrep→MCP→Docker→Gate→审批 API→本地动作/Memory 的合成场景记录。 | reviewer 标为 `SCRIPTED_TEST_HARNESS`；不是本次用户真实人工批准、真实硬件认证或外部 Issue。 |
| [canonical runtime conformance](../deliveries/2026-10-05/implementation-evidence/runtime-security-canonical.json) | 实际 Semgrep、MCP、Docker 与已记录的网络/文件系统、timeout、输出限额、取消和进程中断用例。 | 配置检查和有限探针不能证明所有资源耗尽/逃逸情形；不是 gVisor/microVM。 |
| [durable paused orphan recovery](../deliveries/2026-10-05/implementation-evidence/durable-paused-orphan-recovery.json) | `REAL_PG_DOCKER_WORKER_SIGKILL`：持久句柄定位 paused orphan，清理旧 attempt 后重新检查，留下新 attempt 的结果。 | 不是任意外部副作用的 exactly-once 或所有 crash checkpoint 已穷尽。 |
| [首轮集成导入失败](../deliveries/2026-10-05/implementation-evidence/pytest-first-integration-import-failure.xml)、[恢复首败与修复](../deliveries/2026-10-05/implementation-evidence/recovery-first-failure-and-fix.json) | 失败记录未被删去；namespace 集成和 Docker 大小写错误经过修复与后续验证。 | 历史失败文件、早期 scanner 模块 [EVIDENCE.json](../deliveries/2026-10-05/implementation-evidence/EVIDENCE.json) 都不是最终全项目验收结果。 |

实际 MCP 依赖锁定为 **1.30.0**，stdio 记录协商 **2025-11-25** 并包含 initialize/initialized/list/call；见 [08 runtime conformance](08-runtime-conformance.md)。设计文档推荐的 v2 和 modern 协议没有被本实现冒称为已安装或已验收。升级 SDK/协议时须重新做实际 conformance。

## 已闭合的关键审查问题

下列是本轮原 P1 及关联恢复/安全差量的 closure；“已修正”仅适用于当前受控模板与本地 adapter 边界。

| 边界 | canonical 实现与审查结论 |
|---|---|
| 完整 scope 与对象关联 | [store.py:59](/src/forgeflow/store.py:59) 的 tenant/workspace/project 组合键和 scoped FK 保留 project，不能把同租户不同 project 合并授权；Project 保存 current stage/revision。 |
| 幂等、回放与 approval 消费关系 | [store.py:84](/src/forgeflow/store.py:84) 将 full scope、created_by、key 纳入唯一约束；[store.py:485](/src/forgeflow/store.py:485) 按可信 subject 查询并拒绝不同 payload，回放仍重验对象授权。[store.py:175](/src/forgeflow/store.py:175) 让 consumed action 关系保留完整 scope。 |
| 检索前授权、派生读取与引用 | [context.py:84](/src/forgeflow/context.py:84) 先筛授权候选再排名，[context.py:143](/src/forgeflow/context.py:143) 对精确引用重授权并校验 version/hash；[store.py:533](/src/forgeflow/store.py:533) 对 run 及其 context 来源施加 ACL，受限来源不会通过 report/event/artifact 等派生对象绕过。 |
| MCP 的可信角色权限 | [mcp_bridge.py:33](/src/forgeflow/mcp_bridge.py:33) 从 central POLICY 派生 execution roles；[mcp_bridge.py:286](/src/forgeflow/mcp_bridge.py:286) 在直接调用时重新检查角色、完整 scope、绑定参数、同 source scan、execution policy 与 lease deadline。隐藏 tools/list 不代替授权；当前 execution 仅 hardware。 |
| Gate、审批和实际动作分离 | [gate.py:23](/src/forgeflow/gate.py:23) 独立规则产生 PASS/BLOCKED/UNKNOWN；[store.py:1066](/src/forgeflow/store.py:1066) 只允许 user reviewer 决定。Stage 还要求 technical PASS、原阶段及 revision 匹配、证据当前有效；批准不覆盖 UNKNOWN/BLOCKED。 |
| 审批绑定、fencing 和取消次序 | [store.py:688](/src/forgeflow/store.py:688) 用 DB 时间、token/epoch 防旧 worker 写回；[store.py:1145](/src/forgeflow/store.py:1145) 在本地事务中锁 run/action/approval/project，校验 cancel、lease、proposal/expiry，一次消费 approval，并写 effect/receipt/event。 [workflow.py:223](/src/forgeflow/workflow.py:223) dispatch 前另核当前 policy/rule/skill/code/input hash。 |
| durable handle 与恢复 | [store.py:613](/src/forgeflow/store.py:613) 在 create 前确定 ff 句柄，新 attempt 清空 scanner handle；[store.py:727](/src/forgeflow/store.py:727) 先持久化同 scope/run/step/attempt 的 ffs 句柄。[workflow.py:263](/src/forgeflow/workflow.py:263) 覆盖 expired lease 和 retained-handle/no-lease 的清理；失败维持 NEEDS_REVIEW/cleanup_required，不放行 successor。 |
| 清理与迟到启动 | [sandbox.py:317](/src/forgeflow/sandbox.py:317)、[scanner.py:559](/src/forgeflow/scanner.py:559) 在 lifecycle lock 前写 revoke，按 exact handle/ownership label 清理，阻止旧 attempt 迟到 create。最终 [sandbox.py:356](/src/forgeflow/sandbox.py:356)、[scanner.py:595](/src/forgeflow/scanner.py:595) 接受大小写不同的 exact-object absence，拒绝 handle 前缀、daemon/socket error 和中断/超时等不确定结果。 |
| 日志与 artifact 的真实生命周期 | [sandbox.py:133](/src/forgeflow/sandbox.py:133) 只准备 task-owned 私有 scratch、只读 inputs 和一个固定 result 文件；[sandbox.py:412](/src/forgeflow/sandbox.py:412) bounded attach、冻结后精确读取，再停/删容器与 scratch。使用 per-container logging none，未修改 daemon 全局配置。tmpfs 的 docker cp 实验失败已记录，实际使用经审查的单文件 S1 amendment。 |
| 独立结果验证与失败传播 | [workflow.py:22](/src/forgeflow/workflow.py:22) 用显式条件验证 artifact 原始 bytes hash/size、schema、原输入和 runtime，Python -O 不会去掉验证；[workflow.py:170](/src/forgeflow/workflow.py:170) 将 scanner cleanup failure 传入持久结果，[store.py:822](/src/forgeflow/store.py:822) 拒绝 unverified PASS、持久化 byte hash，并让清理失败进入复核。 |
| Memory 来源与 crash gap | [store.py:1461](/src/forgeflow/store.py:1461) source event 必须同完整 scope 与同 run，不允许分类降级；candidate/confirmed 权限分开，写入幂等。[workflow.py:59](/src/forgeflow/workflow.py:59) 对已有 assessment 只重试 [consolidate:201](/src/forgeflow/workflow.py:201)，从持久 CHECK_COMPLETED 和结果生成 candidate，不重放 resource execution，也不使用被 verifier 拒绝前的成功状态。 |

独立 API/context/skills 读取审查也未发现新的 P1。[api.py:23](/src/forgeflow/api.py:23) 拒绝 production mode 并限制 loopback；[skills.py:145](/src/forgeflow/skills.py:145) 只取 Skill 声明与 trusted role grants 的交集，资源读取不授权 host 执行。

## 关键残余与下一阶段放行条件

这些仍属于已保留需求，不因 S1 跑通而删除，也不要求现在扩大授权、购买 GPU/云机、取得其他项目密钥或写外部系统。

| 未完成范围 / 当前限制 | 放行前必须有的独立证据 |
|---|---|
| **LLM 与多 Agent 尚未接入。** [workflow.py:184](/src/forgeflow/workflow.py:184) 明确是 DETERMINISTIC_NO_LLM；当前固定 hardware 路径不算 Supervisor + 多角色模型执行。 | S3 先锁 authorized provider/model、context export 与 token/time/tool/cost 预算；真实 Supervisor/PM/SRE handoff 和 structured reports，覆盖 invalid output、tool-deny、injection、预算耗尽、provider failure。实际 trace 与本地确定性记录分别保存。 |
| **Knowledge 是 lexical 检索，不是向量/hybrid。** Embedding 表定义不代表生成、存储和检索向量已验收；完整 ingest/update/delete 生命周期与 Memory promotion/supersession 也需继续完善。 | 固定 embedding model/revision/dimension 与 pgvector；各通道先 ACL 候选，再 keyword/vector/RRF；按 [04 评价计划](04-acceptance-matrix.md) 的 held-out 至少 30 问报告真实 Recall/MRR/citation/latency，包含跨租户/角色、冲突、过期与无答案。 |
| **任意 Agent-generated code 仍禁止。** 当前 [templates.py:1](/src/forgeflow/templates.py:1) 是已审查固定模板；单 result bind + RLIMIT_FSIZE 不提供任意代码磁盘配额，同 UID runner 也不是第二隔离层。 | 先完成适合该 threat model 的 provider/真实配额及恶意代码验收，含预分配、进程/CPU/RAM/disk/output、路径/link/artifact、取消/崩溃和清理。再做 A15/F19：真实 model source→scoped immutable blob→同 hash scan/policy/必要批准→真实 MCP/isolation→独立可信 checker；不能以模板选择或生成程序自报 pass 替代。 |
| **MCP 工具、角色和 NPI 范围不完整。** 当前只有 sandbox.execute 是 MCP tool；Knowledge/Memory/local Issue 是普通服务调用。实际 stage 仅 PROTOTYPE→EVT；五角色及后续 stage 未完成。 | 逐个注册/验收 Knowledge/Memory/Issue 等 MCP 操作，server 端独立授权；PM/架构/Android/SRE/硬件须有实际职责贡献、独立上下文、工具权限与持久 handoff。立项/Prototype/EVT/DVT/PVT/**RELEASE** 均须有 entry/exit/evidence/approval、合法/非法/unknown transition 与并发 CAS 证据。 |
| **身份、隔离与运营仍限 DEV_ONLY。** 本地 token 映射、0600 MCP bootstrap、应用 ACL/全 scope FK 不证明 SSO、RLS、多人部署或生产安全；Docker 共享内核和有限 Semgrep rules 留有边界。 | 向 authenticated/multiuser 模式放行前落实既有契约中的真实身份、服务 capability 生命周期、普通 DB 账号/RLS 与全通道隔离验收；生产/任意不可信代码放行前另验批准的 hardened provider、retention/delete、运行负载和恢复。SDK 升级、镜像/规则改版都要重做对应 conformance。 |
| **外部 DevOps/办公未接入；自动 reviewer 不代表真实人类操作。** local Issue 和 project receipt 只证明本地事务。 | 先具备已授权的准确目标与操作范围，再验证真实 GitHub 与所选办公 adapter 的请求、实际 ID/URL、回读及 unknown/reconcile。对于不能远程 fencing 的已 dispatch 请求，保留可能迟到副作用，禁止未查明结果就重发；不承诺取消后零副作用或外部 exactly-once。手动演示须单独记录用户决定，不能拿 SCRIPTED_TEST_HARNESS 充当人类批准。 |

当前没有未解决的 **S1 范围内已确认 P1**。继续开发应遵循上述 release criteria 和原 acceptance matrix；不能把 136 项通过、合成 EVT Gate 或项目中的 enterprise 名称提升为完整产品、LLM 多 Agent、向量系统、真实硬件认证或生产部署完成。
