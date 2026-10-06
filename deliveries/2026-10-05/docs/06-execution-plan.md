# 分阶段可执行计划与可行性

当前：设计评审阶段，不启动以下实现步骤。争取Oct6真实可用，不保证未验证工期。时间为工程工作量的粗估区间，包含实现、集成与meaningful验收，不是依据历史项目测出的速度；AI生成速度不能消除依赖或review成本。

## 交付路径

| 阶段 | 必须产物 / 进入下一阶段条件 | 估计有效工程时 |
|---|---|---|
| S0（本轮） | 来源、复用盘点、contracts、威胁模型、acceptance、并行packet；父Review确认可实施契约 | 设计评估，已形成文档 |
| S1 | FastAPI/Pydantic/SQLAlchemy新项目、独立PG database、scoped run/steps/events/approvals/artifacts、lease/fencing/outbox；policy与Gate engine；核心normal/deny/replay/cross-tenant/restart | 8–14h |
| S2 | 真实DockerProvider +隔离Semgrep +真实MCP gateway；Knowledge keyword最小链、Memory持久链、EVT Skill、local IssueTracker；A01–A14相关测试可复现 | 10–18h |
| S3 | 授权真实model、embedding+pgvector/keyword/RRF、ACL-before-retrieval、citation/conflict lifecycle；Supervisor+PM/SRE实际分派；真实generated code→hash/scan/policy/Docker/独立checker；held-out与LLM错误/budget/injection验收 | 12–22h |
| S4 | Architect/Android/Hardware独立SOP与权限；全部五role；initiation/prototype/EVT/DVT/PVT/RELEASE合成rule/evidence/approval/transition真实执行；真实业务执行与合成manifest区分 | 10–18h |
| S5 | 经批准真实GitHub read/write、一个办公integration；receipt/readback/reconciliation；correlation logs、metrics、review和manual demo | 8–16h，外部access等待另计 |
| S6 | provider硬化：Linux gVisor或批准的microVM、egress allowlist、全攻击回归、retention/delete、运行负载与真实场景 | 16–32h，Linux/access等待另计 |

S1–S5约48–88有效工程小时，S6另计；估计只用于排依赖与识别风险，不转为完成承诺。可让S2 sandbox/MCP与S3 data/model接入部分并行，但合同、scope底座、最终integration和验收仍串行。单写手不可靠在一夜做完全部；三路独立执行能压缩elapsed time，却无法证明Oct6全项完成。若后续实测工速快、依赖全部就绪可以前移，不能通过mock/省测试/关policy来兑现日期。

Oct6的冲刺候选是S1+S2完整深度链，模型和embedding前置条件就绪可争取并入S3。最终五role/全NPI/办公/硬化没有删除，按S4–S6继续做。若用户坚持“全功能”指S1–S5甚至S6，应按这个定义报告not complete，不能把较小交付改名全功能。

## 实施前最小决定/访问缺口

| 项 | 具体缺口 | 不依赖它可推进什么 |
|---|---|---|
| 实施授权 | 最新指示本轮仅设计；父review后切实施。明确不是增加一次无依据的确认仪式 | 所有本轮docs/review；不写py/schema/service |
| LLM | exact provider/model、authorized endpoint/secret方式、费用与data export级别、token/time/tool预算 | 持久链、安全、确定性Gate、真实MCP/Docker测试；LLM仍NOT_RUN |
| embeddings | model/revision/license、CPU可运行方式；若download需批准联网/磁盘，或API批准费用/出境数据 | keyword/provenance/ACL；不叫vector/hybrid已实现 |
| pgvector/runtime dependencies | 新venv Python兼容版本、lockfile/scan artifact；本地pgvector image/extension版本尚未核 | contracts；安装仅在实施授权后，不复用其他项目的库 |
| GitHub | 具体owner/repo、允许操作、proposal内容、访问机制；connector read grant不自动成为新Runtime token | local真实IssueTracker/outbox测试，不能作为外部验收 |
| 办公 | 从Jira/飞书/多维表格选首个真实target；具体project/channel/table、权限/内容/credential方式 | schema/policy/local delivery records；不得真发群 |
| 网络/运行时升级 | egress allowlist目标与控制方式、Linux/runsc环境、批准daemon/VM改变 | Docker默认none；不买云机/GPU/E2B |
| 实际研发资料 | 当前只有合成Aurora SOP/BOM/build/lab fixture；无真实硬件/Android运行环境证明 | 真实框架/校验代码运行；不称真实产品认证 |

这些为具体缺口清单，不要求用户现在把私人密钥贴聊天。已有明确本地实施授权若在后续review结束生效，就按既有批准继续，不重复问例行创建文件/测试权限。

## 并行路线：最多三路，独立写目录

先由一个owner冻结02契约、fixtures IDs、error codes、policy版本与测试expected结果；共享models/DDL只一个writer。此后使用三个独立worktree（若尚无Git则独立staging目录），禁止多writer同改canonical forgeflow目录。

| Workstream | 唯一写范围 | 收敛输入/输出 |
|---|---|---|
| W1 durable/control（integrator owner） | API、全部table/ORM models/DDL/migrations（含knowledge/memory）、storage/session基座、orchestrator state、approval/outbox/audit、Gate engine | 交付scoped interfaces + fault injection；它唯一写/合并shared schema |
| W2 runtime/tools | SandboxProvider、Docker、scan、MCP server/client adapters、artifact collector及tests | 实现冻结Spec/Result，不自行改Scope/Approval contracts |
| W3 context/model | Knowledge/Memory context repositories/query/service代码、SkillRegistry、model adapter、role runners、retrieval/model evaluations；不写table/ORM models/DDL/migrations/storage-session基座 | 基于W1冻结models与Repository interfaces实现context查询；schema变更提交W1，不自行改shared storage |

S4按同一role contract继续；S5 adapter可独立，危险副作用由W1批准/outbox。integrator运行full acceptance+同线程review；新的安全边界由fresh read-only reviewer检验。先集成可运行slice，再扩role/stage；不要让每路各自造一套权限/schema。

## 每个阶段手动复现与本人修改练习

下列为实施后demo脚本应提供的体验，目前命令脚本尚未实现，不伪造可运行路径。

| 阶段 | 手动演示 | 简短讲解目标 | 本人可改练习 |
|---|---|---|---|
| S1 | 提交run，重复key回放；另一tenant读不到；worker中断后状态可解释 | run/step/lease为什么分开；scope来源是什么 | 增加一个合法request field，修正schema/hash与一项冲突测试 |
| S2 | EVT request→MCP→Docker check→Gate；批准localIssue→receipt→Memory；再跑deny/timeout | 函数与MCP通信区别；scan和隔离边界；approval绑定内容 | 修改一个合成Gate required项，观察UNKNOWN和旧approval失效 |
| S3 | 真实model分派PM/SRE，hybrid检索引用，第二session读Memory；冲突与无答案 | RRF/embedding不是事实验证；LLM提议不授权；持久Memory非聊天缓存 | 添加一条过期资料/同义问句，保留held-out集并比较三通道排名 |
| S4 | 全角色执行合法NPI边，missing lab/illegal stage jump拒绝 | 角色权限与职责贡献；合成SOP和认证区别 | 新增一个DVT合成规则，写entry/exit/evidence/approval及negative test |
| S5 | 批准指定targetIssue，真实回读；办公发送receipt；response丢失进入reconcile | local/外部事务边界、幂等与结果未知 | 添加一个只读adapter方法/response validator，不能直接写外部 |
| S6 | 资源耗尽、path/link、network等隔离测试与cleanup报告 | 测试证明的范围与剩余内核/host风险 | 收紧artifact quota并解释failure evidence，保证normal case仍有用 |

练习完成由本人操作与讲解记录证明。每次demo能说清请求/权限/工具/原始证据/失败/下一步即可逐步积累，不提前承诺能力。

## 归档与完成判据

唯一代码原件留forgeflow；不每日复制多份backend。每日`deliveries/YYYY-MM-DD`保存可读design/status/manual demo/commands/test summaries与manifest/hash，索引指向代码原件及run evidence。失败产物也保留；未来Git commit/push需已有具体批准，不默认发布。

本轮停止条件：review包完整、材料边界清楚、父Review可直接评估并分配下一阶段；没有工程代码、外部写、付费或服务运行。父Review若发现新设计问题，在docs中修复后再开写。
