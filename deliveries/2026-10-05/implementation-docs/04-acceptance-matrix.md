# 验收矩阵与需求映射

2026-10-05 S1 实测封存：**本地确定性主链已运行，最终功能范围仍未全部完成**。本轮使用非 superuser 的 PostgreSQL 应用账号、真实 Semgrep CE 1.179.0、官方 Python MCP SDK 1.30.0 的独立 stdio 进程与 `2025-11-25` 协议、真实 Docker 受控模板。四条合成链分别得到 PASS、BLOCKED、UNKNOWN、TIMED_OUT；审批、实际动作、stage 状态各自持久化。

本文核验的最终 [pytest.xml](../implementation-evidence/pytest.xml) 为 **136 tests、0 failures、0 errors、0 skipped**；后续重跑以该文件为准。测试总数包括单元、故障注入与明确 opt-in 的 PostgreSQL/Docker/MCP 集成用例，并非 136 项生产或硬件能力。具名证据与缺口见下面的 S1 映射；原 A/F 验收目标和需求对齐保留。

身份适配器仍是 DEV_ONLY；自动测试中的 reviewer 是 `SCRIPTED_TEST_HARNESS`，审批通过独立 reviewer API 显式完成，不冒称真实人类已经批准项目。输入与规则为合成研发示例，IssueTracker 是本地数据库 adapter。Knowledge、Memory 和本地 Issue 操作为普通服务调用；目前真实 MCP 工具只有 `sandbox.execute`。编排标记为 `DETERMINISTIC_NO_LLM`，尚无真实 LLM 多 Agent、embedding/hybrid retrieval、外部 connector 或生产 RLS。

## 完成等级

L0 = 设计与契约；L1 = 真实本地确定性安全/持久链；L2 = 授权 LLM + hybrid knowledge + 真实多角色链；L3 = 全部五角色与 NPI/DevOps/办公能力的独立验收；L4 = 硬化/真实场景/运行可靠性。等级累积，不得把 L1 冒充 L2 或生产。

`PASS / FAIL / BLOCKED_BY_ACCESS / NOT_RUN` 记录在 deliverable，每个测试保留环境、版本、输入 hashes、命令、exit/result、artifact 及 scope。某 adapter 用本地 synthetic fixture 时，明确 SOURCE_SYNTHETIC/ADAPTER_LOCAL；不遮掉 external Not Run。

## 第一条深度链路（必须一起交付）

以下是保留的验收目标，不表示每个编号已全部通过。S1 映射中的“已验证”仅覆盖明确列出的本地能力；“部分”仍需后续实现与验收；“待实现”对应本轮 NOT_RUN。测试报告中的 PASS 是具名用例结果。

| ID | 场景 | 明确通过条件 | 可手动复现产物 |
|---|---|---|---|
| A01 | 正常 EVT check：全部必需 evidence 当前且一致 | MCP 真调用、scan已运行、sandbox停止清理、technical PASS；stage approval 前没有 transition；local批准后生成唯一 receipt 与 Memory | run timeline、check logs、container inspect、report/provenance。 |
| A02 | P1 blocker/BOM mismatch | Gate BLOCKED；有原始来源；整改 action 需单独批准且仅创建一次；阶段不前进 | blocker report、proposal/hash、IssueTracker row/receipt。 |
| A03 | 缺测试/lab、陈旧版本、冲突 Memory | UNKNOWN 或已证违反条件的 BLOCKED；不得 pass；报告指出缺口/冲突 | evidence table 与 rule逐条 verdict。 |
| A04 | 权限/policy拒绝 | PM 调 sandbox、未知tool、额外参数、任意host path/network profile 均 DENIED；零执行/外部写 | denial事件、tool-call记录、containers 无新增。 |
| A05 | 扫描辅助拒绝与扫描故障 | known finding 触发 policy；scan missing/error/timeout不能转为 pass；正常代码也仍隔离执行 | scanner version/rule hash/output与decision。 |
| A06 | 无限循环 timeout | wall timeout停止整个container；TIMED_OUT独立状态；logs限额；cleaned，Gate无pass | execution timings、kill/cleanup proof。 |
| A07 | 网络 denied | sandbox无法连 TCP/UDP/DNS/metadata，固定允许任务仍正常；未设置allowlist不假称支持 | probe exit/result、inspect NetworkMode、零secret挂载。 |
| A08 | 进程中断与恢复 | worker在checkpoint故障；新worker清理旧sandbox后恢复；旧lease无法写回或新dispatch/consume审批；无重复issue/memory | before/after state、fencing-rejection、cleanup。 |
| A09 | 跨租户/同租户不同project/同名/引用串scope | search、memory、run、events、approval、artifact下载、source resolve、cache均不泄漏；full-scope FK拒绝串写；两个并发tenant/role stdio session不能换身份，hidden tool direct call仍拒绝 | tenant/project/role test log与scoped DB断言。 |
| A10 | 重复/并发提交 | same caller/full scope/key + same input只一个run；不同input 409；其他tenant/project/caller同key独立且不能回放对方run；重复approval/action/memory不增加副作用 | row counts、race assertions、receipt。 |
| A11 | 审批绑定/拒绝/过期 | code/input/target/rule/policy/skill/revision任何变动使旧approval失效；agent不能自批；DENY不可绕过 | old/new hashes、409/deny、no execution。 |
| A12 | 外部结果未知的恢复 | 模拟 adapter 在成功写后断连；状态OUTCOME_UNKNOWN，不盲重发；reconcile绑定唯一结果或needs review | outbox/adapter transcript/receipt，标 synthetic fault injection。 |
| A13 | 跨会话 Memory | terminate/重启服务后，第二run检索第一run promoted Memory，引用event/evidence；未确认decision只pending | 两session不同run、DB记录、Memory来源。 |
| A14 | 取消 | cancel意图、provider停止确认、cleaned可区分；cancel先于原子dispatch claim禁止动作；claim先于cancel则标in-flight/unknown并对账，不假称零副作用 | cancel/claim并发测试、events与resource lifecycle。 |
| A15 | 真实Agent-generated code | 授权model实际生成source→immutable scoped blob→同hash Semgrep/Policy→必要审批→真实MCP/Docker→logs/artifacts→独立checker；unsafe/修改hash旧审批拒绝；不能用预写模板或程序自称pass代替 | model trace、source/scan/spec hash、execution与independent verifier证据；S3验收。 |

## S1 对 A01–A15 的实际映射

| ID | S1 范围结论 | 实际证据 | 剩余缺口 / 限制 |
|---|---|---|---|
| A01 | 已验证，本地合成主链 | [chain-pass.json](../implementation-evidence/chain-pass.json)：同 hash 扫描、MCP 协商/list/call、Docker/cleanup、technical PASS、单独 ISSUE/STAGE 审批和两个唯一 local receipts；跨服务重启可检索 candidate Memory。 | reviewer 为测试 harness；未接真实模型、外部 Issue、真实 lab。Knowledge/Memory/local action 不标作 MCP。 |
| A02 | 已验证，BOM 违反规则 | [chain-blocked.json](../implementation-evidence/chain-blocked.json)：BLOCKED、单独整改 Issue 审批/receipt，项目仍为 PROTOTYPE。 | 本轮 blocker 是合成 BOM mismatch；真实 P1 缺陷/lab 工单仍需输入适配与独立验收。 |
| A03 | 部分，有限规则夹具已验证 | [chain-unknown.json](../implementation-evidence/chain-unknown.json) 与 `test_gate_policy`：manifest 中 missing/expired/conflicted/unmeasured 不提升为 PASS；`test_context` 单独验证 Memory 冲突状态。 | Memory 冲突按明确 subject_key/content 判定，当前作为历史上下文展示；更完整的来源/Memory 冲突协调链仍待实施，不宣称自然语言语义检测或真实资料覆盖率。 |
| A04 | 已验证，当前 allowlist | `test_api`、`test_mcp_bridge`、`test_gate_policy`：PM/隐藏 tool 直调、未知或额外参数、改 identity/scope/profile、scan 缺失被拒；无 host fallback。 | 当前是固定模板与固定工具 schema；未来新增工具须重新做同类 negative 验收。 |
| A05 | 已验证，真实扫描与故障处理 | `test_scanner` 与 [TEMPLATE_SCAN.json](../implementation-evidence/TEMPLATE_SCAN.json)：真实 CE 1.179.0、eval/os.system/nosemgrep 拒绝、syntax/timeout/error/cleanup 故障 fail closed；规则/source/image hashes 留证。 | 有限本地规则无 finding 不是安全证明；资源基准是小型合成夹具，完整依赖锁仍待补。 |
| A06 | 已验证，真实 timeout | [chain-timeout.json](../implementation-evidence/chain-timeout.json)：TIMED_OUT、REMOVED、Gate UNKNOWN；[runtime-security-canonical.json](../implementation-evidence/runtime-security-canonical.json) 另有 output-flood→OUTPUT_LIMIT 与受限 attach。 | 无法取得的产物标 UNAVAILABLE；不宣称超时后仍收回 tmpfs 文件。 |
| A07 | 已验证，当前 network-none profile | 同一 runtime conformance 记录 TCP/UDP/DNS/metadata DENIED、UID 10001、rootfs 写入 DENIED、socket 不存在，inspect 确认 network none。 | 未实施 network allowlist/代理；有限 probe 不等于所有网络/内核攻击已证明隔离。 |
| A08 | 已验证，持久恢复与 fencing | [durable-paused-orphan-recovery.json](../implementation-evidence/durable-paused-orphan-recovery.json)：真实 worker SIGKILL、持久 handle、paused orphan 清理、旧 attempt 撤销、新 attempt 恢复；`test_store`/scanner 覆盖过期 token、迟到 create/completion/dispatch 拒绝。 | 扫描器与 checker 均按精确资源身份清理；host/daemon 管理员仍属 trusted operator。早期失败与修复证据保留。 |
| A09 | 已验证，当前对象与 session | `test_store`、`test_context`、`test_api`、`test_mcp_bridge`：跨 tenant/同 tenant 不同 project、来源 FK、派生 report/artifact/Memory ACL、检索前授权、并发独立 stdio session 无 identity 切换。 | 未实现检索 cache，故无 cache 性能/隔离完成声明；真实 SSO、数据库生产 RLS 待实现。 |
| A10 | 已验证，本地幂等 | `test_store` 并发提交、caller/full-scope key 命名空间、payload 冲突、审批/action/memory 去重；live chain 重复 dispatch 返回原 receipt，结果提交后 consolidation 可重放。 | 唯一 local receipt 来自同 DB 事务；不能外推成外部平台 exactly-once。 |
| A11 | 已验证，当前 material snapshot | `test_store`、workflow safety：hash/revision/expiry/agent-deny、source-version 改变、policy/skill/rule/code/input material snapshot 与两个 run 的 stage CAS；错误产物 provenance 拒绝，优化 Python 模式保留验证。 | 新模型输出、外部目标、generated-code approval 绑定仍需 A15/外部 adapter 验收。 |
| A12 | 待实现 / NOT_RUN | 设计保留 OUTCOME_UNKNOWN 与不盲重发边界；本轮只执行 `issues.create.local`、`stage.transition.local`。 | 尚无外部 adapter 的“远端成功后回包丢失→对账”真实或专用 synthetic fault 测试，不将本地幂等测试代替该项。 |
| A13 | 部分 | `test_store.test_memory_sources_dedup_confirm_and_cross_session` 验证来源、去重、人类确认 guard；live chain 重建 Store/Workflow 后检索同一 candidate，未确认 decision 不标 CONFIRMED。 | 工作流写 candidate；完整 promotion API、semantic/decision 类型及 confirmed supersession 有效区间闭环尚未交付。 |
| A14 | 已验证，当前取消排序 | `test_store` cancel/claim race 与 live mid-MCP cancel：先持久取消意图、后 provider 停止/cleanup ack；cancel 在 dispatch 前时零新 receipt；in-flight 分支保持可见。 | 外部已发送请求的取消/迟到效果与对账待 A12，不承诺零外部副作用。 |
| A15 | 待模型接入 / NOT_RUN | S1 只执行事先登记的 trusted templates；同 hash Semgrep/Policy/Docker 和独立产物校验已有基础。 | 未有授权模型生成 source、scoped immutable code.propose、generated-code profile/审批与完整独立 checker 验收；模板选择不冒称 Agent-generated code。 |

成功路径的 artifact profile 实际为 `checker-v1-fileartifact`：controller 创建单个受限 task-owned `result.json` 文件绑定，模板 child 完成后 pause，再限额读取和核验该文件，随后 remove/cleanup。它是当前 trusted-template profile 的明确实现选择，**通用 tmpfs 多文件冻结导出及任意生成代码场景尚未验收**。workspace/tmp 仍是限额 tmpfs；不挂 home、Docker socket、私人凭据或其他工程。CPU/RAM/PID/caps 等已由 inspect 核验，全面 fork/MEM/disk 压力与内核 hardening 属后续验收。

## 最终功能与需求对齐

| ID | 需求 / 来源 | 最终验收（保留范围） | 阶段 |
|---|---|---|---|
| F01 | Sandbox templates：R1 | >=2受控模板，真实Docker UID/FS/CPU/RAM/PID/timeout/network/artifact+daemon log限制；freeze/export-before-stop与failure UNAVAILABLE；attack tests T07–12；统一Provider | L1，L4硬化 |
| F02 | Knowledge索引：R1/P1/P2 | ingest/parse/chunk/index/version/update/invalidate/delete闭环；来源引用可解析；当前资料选择；不可访问旧删资料作新依据 | L2，L4 |
| F03 | Vector/Hybrid：R2/P1/P2 | 真实选定embedding模型，不用hash假语义vector；keyword与vector各自rank，RRF融合；ACL候选隔离；model/revision维度固定 | L2 |
| F04 | 企业Memory：R1 | episodic/semantic/decision；candidate→promotion→persist→cross-session→supersede/expire；conflict测试；source event/evidence绑定 | L1最小，L2完善 |
| F05 | MCP：R1/R2 | 锁定兼容SDK/协议；独立client/server；协商/list/call/error/timeout；knowledge/memory/sandbox/issues及后续github/office；server端policy生效 | L1，L3扩展 |
| F06 | Skills：R1/R2 | EVT/PM/SRE先验，后全部7类；规范frontmatter、progressive加载、resources hash，scope/tool交集；scripts在sandbox执行 | L1→L3 |
| F07 | LLM/Prompt/Tool use：R1 | 授权model真实tool intent/structured RoleReport；prompt/context/model/token/cost trace；invalid output/budget/provider fail受控 | L2 |
| F08 | 多Agent：R1/R2 | Supervisor +5role独立上下文/工具权限/持久handoff；角色实质贡献；无权角色工具调用失败；汇总source-safe | L2先PM/SRE，L3全 |
| F09 | SOP与NPI：R1/P2 | initiation/prototype/EVT/DVT/PVT/RELEASE都有entry/exit/action/evidence/approval与transition guard；项目current_stage/revision持久化；演示多run竞争CAS、合法/非法/unknown边 | L3 |
| F10 | DevOps：R1/R2 | 经批准目标的真实GitHub issue/PR/commit/Actions读取；创建Issue得到实际ID/URL，回读确认；outbox对账 | L3，访问批准后 |
| F11 | 办公生态：R1/R2 | 选一个真实Jira/飞书/多维表格integration完成schema/scope/approval/audit/receipt；其余adapter有独立backlog不空称全接入 | L3，访问批准后 |
| F12 | Security governance：R2 | authenticated identity、RBAC、ACL/classification、before-retrieval、DENY不可绕过、审批version、secrets隔离、scope audit | L1底座→L4 |
| F13 | 工程与系统设计：R1/R2 | API/DB contracts、meaningful tests、lease/retry/cancel、幂等、故障报告、限额、structured traces/metrics；可解释权衡 | L1→L4 |
| F14 | 硬件/IoT：R2 | 合成NPI规则明确标注；真实硬件/lab输入才可另算证据；不能以stage枚举替代 | L3框架；真实硬件另证 |
| F15 | 平台二开：R2 | 若选择WorkBuddy/Dify/Coze需单独实际改造、差异/集成与验收；读README不满足 | Bonus backlog |
| F16 | 手机GUI Agent：R2 | 若选择需真实设备/模拟器行为与安全验收；Android firmware manifest validator不满足 | Bonus backlog |
| F17 | （公开版已删除：个人背景项，不属于工程验收） | — | — |
| F18 | 规模化落地：R1/R2 | 阶段小步交付记录、故障处理与后续真实运行证据；单机测试不能claim规模化 | L1记录；L4实际运行 |
| F19 | Agent-generated code：P1 | 真实model生成而非选择模板；A15全路径，通过独立可信checker而非自报结果；policy/scan/isolation仍强制 | L2 |

P1中的E2B可选；P2中的gVisor升级保留为L4 Provider验收，需要合适Linux runtime；network allowlist保留L4且默认none。pgvector保留明确功能，不因第一步keyword可运行而删掉F03。

## S1 对 F01–F19 的交付状态

| ID | S1 已实现并验证 | 尚待交付 |
|---|---|---|
| F01 | 四个登记模板/profile 路径、non-root/read-only rootfs/network-none/资源配置、bounded attach+log-driver none、单文件 artifact 生命周期、timeout/cancel/output/recovery、精确清理。 | 通用多文件 tmpfs export、generated-code profile、完整压力与 gVisor/microVM hardening。 |
| F02 | 合成文本 ingest、immutable version/hash、chunk、当前版本选择、expiry、精确 citation 与 ACL-before-content。 | 多格式 parse/chunk/index、公开 update/invalidate/delete 完整服务闭环与检索评测。 |
| F03 | 仅真实词法检索；没有假 embedding 或 hash vector。 | 授权真实 embedding、pgvector、keyword/vector 各路排序与 RRF、model/index generation、held-out 指标。 |
| F04 | event-sourced candidate、来源同 run/full scope、classification 防降级、去重、数据库持久化与跨服务重启检索、确认权限 guard、显式冲突处理。 | promotion/supersede 完整 API、semantic/decision 类型与失效有效区间闭环。 |
| F05 | SDK 1.30.0、2025-11-25 initialize→initialized→tools/list→tools/call 真 stdio、独立进程、固定 session principal/run/role/scope、server policy。 | knowledge/memory/issues 的 MCP adapter、GitHub/office、HTTP authenticated transport；不将普通函数包装说成协议调用。 |
| F06 | `hardware-evt`、`pm-readiness` 两个有效 SKILL.md；动态 snapshot/hash、frontmatter、受限 progressive resources、trusted role-tools 交集与路径/symlink/FIFO 测试。 | SRE 与其余职责 SOP/Skill、Skill scripts 真隔离执行与全七类验收。 |
| F07 | 确定性安全编排及结构化输入/报告已可作为接入底座；未调用模型。 | 授权 provider/model/data/budget，真实模型意图与 trace、token/cost/budget/provider fault 验收。 |
| F08 | 确定性 Supervisor 路由、hardware role principal、Skill/context/tools 最小权限。 | 真实 Supervisor+五角色独立模型上下文、持久 handoff 与实质协作；不称 LLM 多 Agent。 |
| F09 | projects.current_stage/stage_revision、PROTOTYPE→EVT 的 synthetic rules、独立批准/执行与并发 stage CAS。 | initiation/prototype/EVT/DVT/PVT/RELEASE 各阶段完整 entry/exit/SOP/evidence/adapters；保留 RELEASE 范围。 |
| F10 | 仅本地 IssueTracker proposal/approval/dispatch/receipt、幂等底座。 | 经批准目标的真实 GitHub 读写、回读 URL/ID 与外部结果对账；无外部写入。 |
| F11 | 沿用 full-scope/审批/receipt 合约，未接办公工具。 | 首个获批 Jira/飞书/多维表格 adapter 的实际能力与验收。 |
| F12 | DEV_ONLY identity、RBAC、ACL/classification、复合 scope FK、审批版本、DENY、sandbox secrets 隔离与 audit；PG 应用账号非 superuser。 | 真实认证/SSO、生产 RLS、key/capability 生命周期 hardening、数据出境/模型导出策略验收。 |
| F13 | FastAPI、PG 持久对象、leases/fencing、幂等/恢复/取消、审批动作排序、证据报告、meaningful tests 与故障修复记录。 | 正式迁移/完整依赖锁、持续 worker 运维、指标/告警、负载/可靠性/部署；未上线。 |
| F14 | 清楚标注 synthetic NPI/EVT 规则与输入，真实 engine 检查合成 manifest。 | 真实硬件/lab 输入证据，项目夹具不代替真实 IoT 场景。 |
| F15 | 未实施，Bonus backlog 保留。 | 平台真实深度使用/二开与差异验收，需单独范围决定。 |
| F16 | 未实施，Bonus backlog 保留。 | 真实设备/模拟器 GUI Agent 场景与安全验收，需单独范围决定。 |
| F17 | （公开版已删除：个人背景项，不属于工程验收） | — |
| F18 | 设计→独立实现→review→失败修复→实测证据与每日交付记录。 | 真实持续运行、用户/负载与规模化证据，单机测试不能声称生产落地。 |
| F19 | 同 hash 扫描/受控执行/独立产物验证的基础已有。 | A15 真实模型 generated-code 全链；当前 NOT_RUN。 |

以上缺口继续保留在分阶段计划内。模型/embedding 接入需要具体 provider、允许的数据范围与预算决定；外部 GitHub/办公写入需要具体目标、动作和身份授权；生产认证、RLS、Linux hardening/egress 则分别验收。未自动读取私人 key，未购买云机/E2B/GPU，未部署。

## 质量与评价

以下检索/模型指标属于 L2 及后续阶段的验收计划，S1 未输出 30 问 held-out benchmark，也没有 LLM 质量分数。当前安全证据覆盖上面具名的有限夹具；未跑的压力或 hardening 项保持待验收。

检索建立冻结合成语料与 held-out 问题集：至少30问，含同义/中英、精确ID、版本、unanswerable、conflict、tenant/role negatives；训练/调prompt用集合与验收集合分开。比较 keyword / vector / RRF 的 Recall@5、MRR@5、citation validity、context budget与latency。阈值是待review预设目标，不是当前结果：所有positive answer正确引用、所有negative无越权泄漏、unknown不编造；retrieval指标报告真实分母与差异，不为达标更改held-out答案。

LLM至少跑正常、blocker、unknown、tool-deny、prompt-injection、budget exhausted、provider error；多次运行记录variation，不以一次幸运成功宣称稳定。Gate和权限 invariant 必须100%满足测试夹具，模型解释质量单独评级。不让LLM作为自己的唯一评判者。

安全扩大测试放在隔离容器中：受限fork、CPU/MEM、FS/path/link、secret canary、network、output/disk、resource cleanup。保留FAIL和residual risk。没有已批准的网络/外部target就不“临时放开”来补PASS。

## 每阶段交付最低内容

真实状态表、测试命令/退出码、failure evidence、环境与版本、manual demo步骤、本人修改练习、未接入项、父Review意见。截图/录屏可补充，不能替代protocol transcript、DB receipt、container停止/cleanup证据。可读文档按每日deliveries快照归档；source code只保留唯一原件。
