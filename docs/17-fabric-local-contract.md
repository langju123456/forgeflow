# 本地 Stage／Issue／Memory 持久链路

本阶段完成了一个实际运行的确定性闭环：请求 → Supervisor 固定计划 → Hardware Skill → 受控 Knowledge／候选 Memory → MCP stdio → Semgrep 辅助扫描和 Docker 检查 → 组合技术评审 → 独立测试审批 → 本地 Stage／Issue／Memory → 回执、审计和补偿。没有新增模型调用，也没有将多个确定性职责称为 LLM 多 Agent。

隔离项目是 `synthetic-lab / engineering / aurora-fabric-e2e`。其 policy 明确为 `LOCAL_SYNTHETIC_FLOW_VALIDATION`，只允许 `PROTOTYPE → TEST_STAGE` 的本地模拟。真实研发示例、APK、设备与 EVT 资格分别记录；本地 flow PASS 时 EVT 仍 UNKNOWN，`full_evt_eligibility=false`。原 Aurora 项目、5 条已记录模型实验父链、两份 S3C PENDING 源码接受候选均没有改变。用户批准 prototype 是本地研发授权，不是对这些业务候选的批准。

## 模块契约

| 模块 | 输入与输出 | 权限／边界 |
| --- | --- | --- |
| `fabric_runtime.FabricStore` | 复用原持久运行、lease、check、audit | 仅新 scope 禁用 hardware-only 自动提案；旧 scope 不变 |
| `fabric_contract.evaluate_fabric` | 同 scope 的 run／events、固定 XML fixture selector → 稳定技术报告 | 复算 Hardware、SRE、BOM／interface／XML；不执行动作；不接受用户路径或自报 PASS |
| `FabricOperations.create/get` | run → 持久 `FabricReview` | 全 run／文档 ACL、event clearance、当前版本／有效期和完整 audit；创建为 user admin／supervisor |
| `propose/decide` | 报告 hash、固定动作 → `FabricApproval` | 参数版本、技术来源、adapter hash、expiry、独立 reviewer 和 revision CAS；决定不等于执行 |
| `execute` | 已批准 proposal ID → `FabricReceipt` | service `fabric_executor`；重新授权和校验；本地效果、回执、审计同一事务 |
| `memory` | 当前 caller scope／query → ACTIVE 引用与原始 citation | ACL 先于内容评分；原候选保持 CANDIDATE；校验来源、hash、状态和 Memory 自身有效期 |
| `fabric_api`／`fabric_cli` | HTTP／手动命令 | DEV_ONLY／TEST_DRIVER；本地函数为 IN_PROCESS，只有 sandbox 使用真实 MCP |

技术 hash 包含 scope、run、request、CHECK_COMPLETED 之前的事件、对应 step／attempt／epoch／输出、snapshot、result、assessment、artifact bytes／provenance，以及当前 code、rule、policy、Skills 和 XML hash。动作、审批、项目阶段和父 revision 不属于这个技术投影，因此合法后续动作不会使自己的批准失效。完整当前审计链仍单独验证；文档 ACL／版本／时效在读取、批准和执行时再次检查。

四个新表是 `fabric_reviews`、`fabric_approvals`、`fabric_receipts`、`fabric_items`，实际 PostgreSQL 检查确认所有外键含 tenant／workspace／project。原 Knowledge、Memory、Run、Artifact、Event 表继续复用。`fabric_items` 保存真实本地数据库 Issue 及已批准 Memory 引用；不是 Jira／GitHub 工单。

## 动作与补偿

Stage、Issue、Memory 各自申请批准，不共享一张万能 approval。提案只接受六个固定 kind，不接受 caller 自定义路径、工具、执行参数或身份。每条 run 的同类前向动作最多一份提案；同 creator／key 重试必须匹配原 proposal。跨进程执行重试读取唯一 receipt，不重复生效。

Stage 使用项目版本 CAS。Issue 建立 OPEN 本地记录。Memory 激活要求同一 run 的 Stage 和 Issue 回执；候选引用组合评审事件及两个实际执行事件，保留 source hashes。底层候选不被伪装成 CONFIRMED；ACTIVE 的是独立批准的引用，且 provenance 是 TEST_DRIVER。

`STAGE_REVERT`、`ISSUE_RETRACT`、`MEMORY_REVOKE` 各自需要另一份批准，绑定原 receipt hash 和当前对象版本。它们保留所有证据，分别恢复原阶段、标记撤回、撤销 Memory 引用。补偿可在技术来源后来过期／变化时减少既有影响，但当前 ACL、完整审计、原回执及目标 CAS 必须有效；这不是让 UNKNOWN 获得前向授权。

本地效果与事件在一个 PostgreSQL transaction 中提交，没有远程不确定副作用。因此 SIGKILL 发生于 flush 后、commit 前时，数据库回滚效果、回执和事件；恢复后可执行一次。这个结果不能推广为外部 API 的 exactly-once 保证。

## 增量威胁与验收

| 失败／威胁 | 控制与实际证据 |
| --- | --- |
| 用硬件 PASS 推进 EVT | 默认 APK／device UNKNOWN；新 policy 禁止 EVT，模拟 TEST_STAGE 单独标识 |
| prompt、agent 或 service 自批 | 服务器固定结构化参数；user reviewer、独立 subject；service-only executor |
| 重试重复动作／串租户 | scoped FK、key／动作唯一约束、Project → Run → Approval 锁顺序、receipt 重放；实际 HTTP cross-scope 404 |
| 审批后来源／参数变动 | 复算稳定报告、当前文档版本／expiry、proposal hash 和目标 revision；单测拒绝 |
| Memory 成为未经审批的事实 | 候选与批准引用分离；引用组合证据；过期、未生效、撤销候选不参与 ACTIVE 检索 |
| 提交前中断 | 实际自身 worker SIGKILL -9；事务完全回滚，恢复一个提交回执 |
| 补偿覆盖较新状态 | receipt-bound Stage／Item CAS；独立审批，不删除历史 |
| 扫描通过被当安全证明 | Semgrep 是辅助；原 Docker 非 root、只读 rootfs、network none、资源限制和清理继续生效 |

新测试 44 项（contract 23、store／API 21），加原职责／EVT 67 项共 111 项通过。实际运行是 2 次新 MCP／Docker 检查、6 次明确 TEST_DRIVER 决定、6 条本地执行／补偿回执、15 项 HTTP／schema 检查；另验证原启动与存储重放不创建新执行。首次 HTTP harness 把 execute 的直接 Scope body 写成了 wrapper，收到 422，失败记录保留；修正的是 harness，执行器未变。

审计链与 payload hashes 检测记录变动，不抵抗拥有数据库管理员权限的人重写整条链。DEV bearer fixture 身份不是生产 SSO。这里只验证固定合成夹具，没有生产硬件认证、实际 Android build／device、远程 Issue／办公写入或全面 NPI。

## 手动复现与本人修改练习

在唯一原件目录执行：

```sh
.venv/bin/python scripts/fabric_service.py
.venv/bin/python scripts/fabric_e2e.py checks
.venv/bin/python scripts/fabric_e2e.py effects
```

已有实验只校验或重放封存 IDs，不再调用模型／sandbox 或制造效果。审阅地址 `http://127.0.0.1:8773/docs`。查看已有报告：

```sh
.venv/bin/python -m forgeflow.fabric_cli get ecbc1e1f-4920-4575-8015-4fba1144c41b
.venv/bin/python -m forgeflow.fabric_cli memory --query Aurora
```

第二条应为空，因为演示已经显式撤销引用。激活前候选与引用证据在 `evidence/effects.json`，不是丢失数据。

完整手动新实验用 `fabric_cli submit --key <你的新key> --fixture pass` 获得 run ID，然后依次 `check <run>`、`review <run> --key <reviewkey>`。复制 report_hash，用 `propose <review> <hash> --kind TEST_STAGE --key <key>` 获得 approval；执行 `decide <approval> <proposal_hash> 0 --decision APPROVED` 后阶段仍未变，只有 `execute <approval>` 才改变阶段。随后分别做 ISSUE 与 MEMORY_ACTIVATE；补偿时 `propose ... --kind STAGE_REVERT --target-receipt <stage receipt>` 等也要批准。`--fixture unknown` 的提案应拒绝；XML selector `bom-mismatch` 应 BLOCKED。

可先解释三件事：为什么 UNKNOWN 不通过、为什么 approve 后没有动作、为什么提交前崩溃能重试而远程 API 不能直接套此结论。本人修改练习：在后续增量中修改本地 Issue 标题常量，运行相关 tests，观察 adapter hash 改变如何使旧 approval 不可用于新执行，再以新 run 申请和批准。保留本次 seal，记录后续变更；不因此声称已经掌握 Agent／ML。

复用出处：本批只复用此前 ForgeFlow 的 Store／Workflow／MCP／Sandbox／Knowledge／Memory／Skills／职责纯检查；继承封存 manifest 引用。XML 是 PUBLIC_SYNTHETIC_TEMPLATE，新 scope 有自己的 policy，没有重标旧合同。没有新增第三方代码或依赖；许可证和初次项目盘点仍见 `docs/05-reuse-and-sources.md`、`docs/THIRD_PARTY.md`。来源策略都是合成示例。
