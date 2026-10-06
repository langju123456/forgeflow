# 验收矩阵与需求映射

2026-10-05：**全部为待实现/待测试**。下面是实现后的检查计划，不是现有测试结果。仅“官方资料核验/本机只读环境检查”已经完成。

## 完成等级

L0 = 设计与契约；L1 = 真实本地确定性安全/持久链；L2 = 授权 LLM + hybrid knowledge + 真实多角色链；L3 = 全部五角色与 NPI/DevOps/办公能力的独立验收；L4 = 硬化/真实场景/运行可靠性。等级累积，不得把 L1 冒充 L2 或生产。

`PASS / FAIL / BLOCKED_BY_ACCESS / NOT_RUN` 记录在 deliverable，每个测试保留环境、版本、输入 hashes、命令、exit/result、artifact 及 scope。某 adapter 用本地 synthetic fixture 时，明确 SOURCE_SYNTHETIC/ADAPTER_LOCAL；不遮掉 external Not Run。

## 第一条深度链路（必须一起交付）

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

## 质量与评价

检索建立冻结合成语料与 held-out 问题集：至少30问，含同义/中英、精确ID、版本、unanswerable、conflict、tenant/role negatives；训练/调prompt用集合与验收集合分开。比较 keyword / vector / RRF 的 Recall@5、MRR@5、citation validity、context budget与latency。阈值是待review预设目标，不是当前结果：所有positive answer正确引用、所有negative无越权泄漏、unknown不编造；retrieval指标报告真实分母与差异，不为达标更改held-out答案。

LLM至少跑正常、blocker、unknown、tool-deny、prompt-injection、budget exhausted、provider error；多次运行记录variation，不以一次幸运成功宣称稳定。Gate和权限 invariant 必须100%满足测试夹具，模型解释质量单独评级。不让LLM作为自己的唯一评判者。

安全扩大测试放在隔离容器中：受限fork、CPU/MEM、FS/path/link、secret canary、network、output/disk、resource cleanup。保留FAIL和residual risk。没有已批准的网络/外部target就不“临时放开”来补PASS。

## 每阶段交付最低内容

真实状态表、测试命令/退出码、failure evidence、环境与版本、manual demo步骤、本人修改练习、未接入项、父Review意见。截图/录屏可补充，不能替代protocol transcript、DB receipt、container停止/cleanup证据。可读文档按每日deliveries快照归档；source code只保留唯一原件。
