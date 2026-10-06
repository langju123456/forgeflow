# 独立设计 Review

Reviewer：独立 read-only 审阅。review对象为ForgeFlow设计/来源/需求，不是代码、部署或生产安全审计。

首轮Decision Packet结论：方向可行，但以下契约须修正后才implementation-ready。Primary修改文档，reviewer没有写文件。

| 优先级 | 问题 | 已采用修正 |
|---|---|---|
| P1 | lease只guard completion，过期worker可能dispatch/consume approval | 原子fenced dispatch claim、approval版本/expiry消费、cancel guard；已in-flight外部动作unknown/reconcile，不假称zero side effect |
| P1 | 多run NPI没有project current_stage/revision | projects/stage_transitions，合法边与stage CAS，receipt/event事务 |
| P1 | scope与FK忽略project矛盾 | tenant/workspace/project完整FK，same-tenant different-project tests |
| P1 | stdout截断不限制Docker daemon log | per-container none+bounded attach，overflow处理，inspect与磁盘增长测试，provider conformance仍待运行 |
| P1 | tmpfs在stop后失去artifact，导出时序未闭合 | check完成保持alive→pause/freeze→bounded regular export→stop/remove；已stop的失败artifact明确UNAVAILABLE |
| P2 | MCP身份如何进入server不明确 | 每session固定principal/run/role/scope，用trusted短期bootstrap capability，不接受model参数换身份 |
| P2 | prewritten template不满足Agent-generated code | code.propose immutable blob、scan/policy/approval/realMCP/Docker、同hash与独立checker；A15/F19 |
| P2 | P2原文RELEASE被设成optional | RELEASE恢复最终NPI验收 |
| P2 | W1/W3 storage写范围冲突 | W1独占全部ORM/table/DDL/migrations；W3context repositories/query/service |

Reviewer最终复核原文：

> 复核结论：设计现在具备有条件实施准备度。此前报告的 P1/P2 均已在文档中修正，没有剩余设计阻断项。
>
> 已确认完整 project FK、持久阶段 CAS、原子 fenced dispatch/cancel、固定身份 MCP session、日志限额和 tmpfs 导出生命周期；真实生成代码验收、RELEASE 与 W1/W3 所有权也已补齐。
>
> 实施仍须验证锁定 SDK release、Docker `none+attach`、pause/freeze 后安全导出、资源限额与清理，并落实模型/embedding/外部目标的访问决定。验证失败时按既定失败或 UNAVAILABLE 状态处理。
>
> 这是文档复核通过，尚无运行测试结果，也不支持承诺 Oct6 全项完成。Changed: none。

Review确认需求可见内容覆盖充分、未发现完成事实夸大。48–88工程小时是粗估，不转为Oct6承诺。父任务可据此进入已授权后续实施；本轮按指定边界停止在设计交付。
