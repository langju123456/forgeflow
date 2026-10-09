# 审阅发现与修复记录（2026-10-06 至 10-08）

主要实现完成后，独立审阅确认了 10 个问题，全部已修复：每个修复先提交一个在旧代码上失败的测试，再提交修复，并在另一台机器上独立复跑。仍未解决的限制列在最后。

## 方法

- **先红后绿。** 每个修复对应一对提交：`test: …（fails before the fix）` 和 `fix: …`。复核时先在修复前的提交上确认测试失败、而且失败原因正确，再在修复后的提交上确认通过。
- **独立环境复跑。** 复核不在开发机上做，而是在一台隔离的 Linux aarch64 虚拟机里，用 Python 3.12.15 从 git 克隆后重跑全量测试；对开发机上的仓库只读。
- **变异检查。** 把关键实现或断言改坏，确认测试真的会失败。
- **伪造系统时钟。** 用 libfaketime 把整个进程（包括 SQLite 的时间函数）设为 2026-11-06、2027-03-01、2027-03-22、2027-06-01 复跑。

## 问题与修复

提交按提交说明的开头列出，可以用 `git log --grep` 查找。

| # | 问题 | 怎么确认 | 修复提交 | 结果 |
|---|---|---|---|---|
| 1 | 旧 CLI 和旧 HTTP 路由能直接把“仅硬件”审批设为 APPROVED 并派发本地动作，绕过 EVT 绑定评审 | 在基线上写测试复现，6 项失败 | `fix: block legacy hardware-only approvals at the Store`；`fix: HTTP legacy decision route defers to the Store` | 所有生产入口都用 `EVTBoundStore`；被拦截的尝试写入 `LEGACY_APPROVAL_BLOCKED` 审计事件 |
| 2 | 冻结实验的 50 个源文件哈希按工作区校验，任何后续修复都会让历史证据校验失败 | 修复 1 之后冻结校验立即失败 | `fix: anchor the S2C frozen-source check to the baseline git commit`；`test: run the frozen replay with the anchor commit's own code` | 锚定到基线提交；重放从该提交导出代码运行；冻结文件一字未改 |
| 3 | 测试读真实时钟：夹具证据 2026-11-05 到期后，大量测试会变红 | 伪造时钟复现；统一时钟的第一版在 2026-11-06 仍有 123 项失败，原因是测试夹具里的 Gate 调用读了真实时钟 | `test: one shifted clock for the test session`；`test: fixtures pass the test clock to gate.evaluate (F3)` | 整个测试会话用一个平移时钟（T0 = 2026-10-05T12:00Z）；有检查禁止直接读系统时间 |
| 4 | 修改 `fabric_store.py` 会改变适配器哈希，升级后历史审批读不出、待批提案无法续批（F1） | 测试复现 | `fix: Fabric history survives a code upgrade and stale pending proposals can be renewed (F1)` | 追加式版本表 `fabric_approval_versions`；失效原因 `ADAPTER_CHANGED`、`EXPIRED`、`REVIEW_STALE`；替代过程有审计 |
| 5 | 续批之后，早期版本的提案人仍能批准最新版本（A 提案，B、C 依次续批，B 可以批准） | 复核时构造 A→B→C 场景复现；回归测试在修复前报 `DID NOT RAISE` | `fix: every proposer of any version of an approval is excluded from deciding it` | 审批人排除创建人、当前提案人，以及所有版本的提案人与替代人 |
| 6 | 重评记录把执行时的代码和实验锚定的源码混在一起（F2） | 测试复现 | `fix: a regrade records the code it executed apart from the experiment's anchored sources (F2)` | 执行依赖与锚定源码分开记录 |
| 7 | 冻结重放继承调用方的 `PYTEST_ADDOPTS` 等选项，可能少跑用例仍报通过（F4） | 测试复现 | `fix: the frozen replay scrubs inherited pytest options and requires the fixed case set (F4)` | 清除 `PYTEST_*`，要求固定的 26 个用例，按 JUnit 结果核对；另加子进程导入来源断言 |
| 8 | 服务替换失败时恢复旧模块的路径没有测试 | 审阅发现 | `test: fabric_service restores the verified prior module when the new one cannot start` | 8 个场景测试 |
| 9 | 演示夹具和 SOP 在 2026-11-05 过期；快照有效期是写死的常量，之后新动作会因证据过期被拒 | 伪造时钟复现，读代码确认 | `fix: renewed -v2 fixtures, one fixture family and -v2 defaults, so demos do not expire on 2026-11-05`；`fix: SOP renewal is planned, rehearsed on a copy and applied only on request` | 见下文“到期修复的验证” |
| 10 | 子进程导入来源断言的对照部分依赖可编辑安装，用 `PYTHONPATH` 运行时误报失败 | 独立环境复跑时报 `ModuleNotFoundError` | `test: the import-origin control no longer depends on an editable install` | 两种安装方式下都通过 |

编号 F1–F4 来自 2026-10-07 的一轮复核，原始记录在 `deliveries/2026-10-07/`。

## 到期修复的验证

- `-v2` 夹具与原件逐字节比较，只有 `valid_until` 不同（每个夹具 4–5 处）。
- 用真实的 Store、Gate 和 Workflow，沙箱、扫描器和 MCP 用测试驱动，把测试时钟设为 2026-11-20 跑完整检查：`aurora-pass-v2` 得到 Gate PASS，快照有效期 2027-04-05，新鲜度检查通过；原件 `aurora-pass` 得到 Gate UNKNOWN，新鲜度检查返回 `EVIDENCE_EXPIRED`。
- 到期提醒：2027-03-01 通过；2027-03-22 起失败（剩 13 天）；2027-06-01 失败。
- SOP 续期：默认只输出计划；必须显式给出 `--database-url`，不读 `.runtime/` 或已配置的数据库；构造 Store 不建表、不写库；`--apply` 写入 v2 后 v1 保留，引用 v1 的 run 因 `SOURCE_CHANGED` 不能推进，引用 v2 的通过；重复执行返回 `ALREADY_CURRENT`。

## 最终复跑（隔离环境，2026-10-08）

| 条件 | 结果 |
|---|---|
| 真实时钟 | 945 项：932 通过，13 项实机测试跳过 |
| 系统时钟伪造为 2026-11-06 | 同上 |
| 系统时钟伪造为 2027-06-01 | 另有 4 项到期提醒按设计失败 |
| 公开历史（移除 `sources/` 后） | 与原历史结果相同；冻结重放在新锚点上通过 |

把测试时钟起点改到 2026-11-20（环境变量 `FORGEFLOW_TEST_CLOCK_T0`）会让约 120 项测试失败。这些测试自己的数据写死了原始夹具和 2026-11-05 的 SOP，是测试数据过期，不是产品行为；这个开关只适合做定点检查。

## 仍未解决

- 审批人身份来自本地测试身份文件，没有独立发放；一个操作者可以同时持有提交、审批、执行三种身份，互斥只在 Principal 层面。
- 本地 API 用静态 token，没有过期、轮换和吊销；非 development 模式拒绝启动。
- 检索只有词法；同义词和跨语言查询全部漏检；向量检索与混合检索没有实现。
- 只有 Supervisor 与 PM 使用模型；Architect、Android、SRE 是确定性检查器；Android 构建与设备测试没有运行。
- 实机链路（Docker、PostgreSQL、本机模型）最近一次完整运行在修复之前（2026-10-06），当前代码待复跑。
- S2C 与 grounded 场景脚本按设计使用原始夹具；docs/07 里直接 APPROVED 的旧步骤现在会被拒绝。
