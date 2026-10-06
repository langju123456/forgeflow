# 模块契约与持久状态

状态：最终契约与当前实现映射。已有 Python/SQLAlchemy/PostgreSQL 实现；未实现的最终接口不作为运行保证。policy、skill、gate rule、输入和来源分别绑定 hash。

当前差异：DEV_ONLY 固定身份；确定性 hardware role；仅 sandbox.execute 是 MCP；Knowledge/Memory/Issue 为本地服务；没有 resume HTTP endpoint（恢复经 CLI）；没有向量通道或真实外部 reconcile。数据库实际对象以 store.py 为准，表格保留最终设计字段。审批消费、local Issue/receipt、stage CAS 与事件使用同一事务。Memory 检查完成后的 candidate 写入是独立幂等事务，重入补写而不重跑 checker。

## 统一 Scope 与身份

`Scope = tenant_id + workspace_id + project_id`；`Principal = subject_id + kind(user/agent/service) + roles + classification_clearance + granted_scopes`。scope 从经过验证的用户/服务身份推导，不能相信请求 body、LLM output 或任意 `X-Tenant-ID`/`X-Role` header。handoff 只能得到父授权的交集，不能修改 identity。sandbox action 通过短期 run/step/role 绑定的内部 capability 调用，不持有用户 token。

本地测试 principal 由测试 harness 注入，不暴露公网；若首实现采用 development identity adapter，明确标注为 DEV_ONLY，启动时拒绝公网绑定与 production 模式。它只能证明授权逻辑，不能证明 SSO/真实认证已完成。最终 authenticated mode 用批准的独立凭据或 identity provider，DB 普通账号 + RLS/复合 scope 约束；不可使用 superuser 绕过 RLS。

所有 repository 接口、FK、唯一索引、查询、cache key、artifact download、event list、approval receipt、memory citation resolution 都含 scope。即使对象 UUID 全球唯一，查询也必须与 scope 同时约束。不存在 scope 的接口不对 agent 开放。

## 模块 I/O

| 模块 | 输入 → 输出 | 持有的权限 / 禁止事项 |
|---|---|---|
| API | principal + RunRequest + Idempotency-Key → persisted RunView | 认证、输入验证；不在请求线程运行代码，不接任意 shell。 |
| Supervisor | request + scoped context + budget → structured Plan/Handoff | 分派已登记 role 与 action 模板；不批准、不拥有全工具权限。 |
| RoleRunner | handoff + activated skills → RoleReport + ToolIntent | 单独 model context/identity/budget；工具仍经 gateway 授权。 |
| SkillRegistry | trusted registry root + role + task → SkillSnapshot | 加载合法 frontmatter/按需资源；snapshot hashes；脚本只经隔离执行。 |
| KnowledgeService | principal + query + filters + context budget → cited hits | ACL 决定候选集合；版本、有效期、当前状态；不访问私人会话。 |
| MemoryService | principal + search 或 MemoryCandidate → memory refs/promotion | 候选与已确认事实分开；write 有来源、类型、scope 及审批规则。 |
| ToolGateway | trusted principal + ToolIntent → validated ToolResult/denial | schema、tool allowlist、policy、timeout、audit；不透传任意 CLI flags。 |
| SandboxProvider | approved ExecutionSpec + immutable input manifest → ExecutionResult | 固定镜像与资源配置；无 secrets、socket、任意 host mounts。 |
| GateEvaluator | typed EvidenceBundle + immutable RuleSnapshot → GateAssessment | 确定性规则；UNKNOWN 永不算 PASS；不接受 LLM 直接打 pass。 |
| ApprovalService | approved reviewer principal + proposal hash + decision → Approval | 绑定动作/规则/输入版本、有效期、reviewer scope；不得批准 DENY。 |
| IssueTracker | approved ActionIntent + idempotency identity → ActionReceipt | local 或外部 adapter；外部需目标 allowlist 与访问批准。 |
| AuditStore | scoped causal event → committed append-only sequence | 同事务记录状态与意图；不把 raw credential/prompt 全写公开日志。 |

## 持久对象与不可变字段

每个项目敏感关系对象都有composite identity `(tenant_id, workspace_id, project_id, id)`，FK引用完整scope，而非仅tenant/workspace后靠应用过滤project。run/step/approval/artifact/action/evidence/memory/event源链必须绑定同project。workspace共享文档若后续支持，使用独立共享对象/grant并显式授权桥接，不让nullable project绕过约束。可变状态用revision CAS。此表保留最终设计；首阶段实际表和字段以 store.py 为准，不能将 embeddings schema 当作向量检索实现。

| 对象 | 关键字段与约束 |
|---|---|
| projects / stage_transitions | project scope identity、current_stage、stage_revision、rule_set_ref；transition id、from/to stage、expected_stage_revision、assessment/approval/action refs、status；stage CAS与transition receipt/event同事务，不能只存各run的stage字符串。 |
| runs | id、scope、request_hash、request_json、state、current_step、revision、cancel_requested_at、created_by、trace_id；unique(full scope, submitting_subject_id, idempotency_key)。回放也重新验证对象读取权限，不因同key返回他人/其他project run。 |
| steps | id、run_id、step_key、state、input_hash、output_hash、attempt、lease_owner、lease_token、fencing_epoch、lease_expires_at、heartbeat_at、sandbox_handle；unique(scope,run,step_key)。 |
| events | id、run_id、step_id、sequence、kind、actor、causation_id、payload_hash、redacted_payload、created_at、prev_hash；unique(scope,run,sequence)。 |
| artifacts | id、run/step、content_sha256、size、mime、classification、storage_key、manifest_hash、provenance、collected_at、retention；下载再次授权。 |
| evidence | id、run、source_type、source_ref、source_version、content_hash、observed_at、valid_until、status、related_build/BOM revision；missing/expired/mismatched 不可提升为有效。 |
| approvals | id、run、kind、proposal_hash、parameters_hash、input_manifest_hash、policy/skill/rule_version、proposal_revision、expected_stage_revision、reviewer、decision、expires_at、consumed_by_action；unique(scope,run,kind,proposal_hash)。approval decision的expected_run_revision是并发CAS guard，批准后普通状态变化不改material proposal_revision。 |
| action_intents | id、run、action_key、tool、canonical_parameters、parameters_hash、approval_id、status、attempt、dispatch_owner/token/fencing_epoch/deadline、reconcile_state；unique(scope,action_key)。 |
| action_receipts | intent_id、target、external_id/url、request_digest、response_digest、executed_at、status；一个逻辑动作最多一个已确认结果。 |
| documents / versions / chunks | doc identity、scope、ACL/classification、version、source hash、observed_at、validity、supersedes/deleted_at；chunk 必须引用精确 doc version，不引用可变 latest。 |
| embeddings | scope、chunk_version_id、model_id/revision、dimension、normalization、vector、created_at；更换 model 触发新 index generation。 |
| memories | id、scope、type、subject_key、content、sources(event/evidence refs)、confidence、status、valid_from/to、supersedes、created_by、human_confirmed、classification；source 引用不得跨 scope。 |

Knowledge current 资料和 Memory active 事实由明确规则挑选。相同 subject_key 的未决相互矛盾事实输出 conflict，不能默取最后一条；confirmed supersession 在事务中关闭旧有效区间，再激活新记录。retracted/deleted 资料引用仍可在受控审计中显示其已失效状态，但不可作为新的 Gate 正面依据。

## API 草案

- `POST /runs`：project + stage + objective + fixture/input IDs；202 persisted，不接受 code/host path/env/任意 role 权限。same caller/full scope/key + same payload replay，同命名空间key不同payload 409；其他tenant/project/caller同key不相互影响。input IDs也必须在提交时及读取时授权。
- `GET /runs/{id}`、`/events`、`/artifacts`、`/report`：scope-aware，未授权对象使用统一 404，不通过错误内容泄露存在性。
- `POST /runs/{id}/cancel`：持久取消意图；worker/provider ack 后才 report cancelled。不能把 API 接受取消当作进程已死。
- `POST /approvals/{id}/decision`：reviewer identity、decision、proposal_hash、expected revision；不接受替换 parameters。stale/expired 409/410，role agent 无此 capability。
- `POST /runs/{id}/resume`：重新授权 + 当前状态 guard；安全可重试步骤恢复，未知外部结果走 reconcile，不盲重发。

## State machine

Run：`CREATED → QUEUED → RUNNING → WAITING_APPROVAL → RUNNING → COMPLETED`，另有 `FAILED / CANCELLED / NEEDS_REVIEW`。COMPLETED 表示执行链报告完成，不等于 Gate PASS。WAITING_APPROVAL 不长期占 lease。

Step：`PENDING → CLAIMED → RUNNING → SUCCEEDED` 或 `DENIED / FAILED / TIMED_OUT / CANCELLED / OUTCOME_UNKNOWN`。heartbeat 使用 DB 时间；成功写回需 lease token、未过期和 fencing epoch 全匹配。旧 worker 可有迟到结果，绝不能覆盖新 worker 状态。

GateAssessment：`PASS / BLOCKED / UNKNOWN`；rule逐项产生 `SATISFIED / VIOLATED / UNKNOWN`。任一明确 blocking violation → BLOCKED；没有 violation 但有必需 unknown → UNKNOWN；全部 satisfied 才 technical PASS。stage transition 状态独立为 `NOT_REQUESTED / PENDING / APPROVED / EXECUTED / REJECTED`。仅technical PASS +有效stage approval才允许当前NPI状态沿合法边执行，不能越级或靠批准覆盖缺失事实。执行前重验当前stage revision与必需证据版本/有效期；stage update/receipt/event同事务CAS，资料已变不能拿旧PASS/approval进入下一阶段。

ActionIntent：`PREPARED → WAITING_APPROVAL → APPROVED → DISPATCHING → SUCCEEDED`，或 `REJECTED / FAILED / OUTCOME_UNKNOWN / NEEDS_RECONCILIATION`。Issue blocker correction action 在 BLOCKED/UNKNOWN 的 review 中也可以单独获批；不要求 gate 已过才能创建整改任务。

## 幂等与中断恢复

创建run与首事件同事务；key命名空间包含tenant/workspace/project/submitting subject，回放重验run授权。semantic hash采用固定canonicalization，包含输入版本与目标，不把timestamp混进重试identity。相同step输入的持久output可复用；资源配置或输入改变则新attempt/hash，关联旧证据。

审批批准→outbox intent→audit事务提交后才准备发送。实际dispatch必须再做一个原子fenced claim：锁定/比较run cancellation与状态、step的fresh lease token/epoch、action状态、approval的proposal/input/policy版本与expiry；同事务消费approval到该action、记录dispatch owner/token/deadline、intent=DISPATCHING与audit。任何guard失败零新dispatch；heartbeat/续租不允许旧token复活。不能用“读一下approval再发请求”的散开逻辑。

local IssueTracker可在同DB事务insert issue +receipt，重复由unique约束保证。外部平台不接受本项目fencing token，因此本地claim不能保证一个已进入DISPATCHING的请求绝无迟到副作用。cancel与claim有明确排序：cancel先提交→禁止claim；claim先提交→取消标记为可能in-flight、等待停止/回执或reconciliation，不能报告“取消成功且零副作用”。失效dispatch不由新worker盲重发，也不得用新token把旧执行成功覆盖为新attempt。

外部GitHub不宣称exactly-once：持久action marker、目标限定读取对账与bounded retries。网络结果丢失/5xx/进程在POST后死掉，一律OUTCOME_UNKNOWN；查询到匹配结果才绑定receipt，没有可靠确认不能盲重POST。reconciler用独立scoped lease记录结果；receipt可以记已发生的真实事实，但迟到worker不能提升当前run状态或绕过stage CAS。人工reconciliation的授权与记录也持久化。

lease recovery 先撤销失效 attempt 能力、检查并终止/清理其 sandbox，确认残留停止后才能重跑纯检查。provider handle 需在容器启动前持久化可确定 identity，容器 label 包含 namespace/run/step/attempt；重启只清理本项目精确标签资源。无法证明清理完成 → NEEDS_REVIEW，不能“恢复成功”后仍有旧进程执行。

付费 model call 在 dispatch 前记录可能调用边界；调用结果未知不可无限重试。LLM 结果、tool intent 和校验理由也持久化，以便解释失败；不把 model rationale 当作授权。

## MCP 协议决定

实际首实现采用官方 Python SDK **1.30.0**，锁定在 requirements.lock；独立 stdio client/server 实际协商 **2025-11-25**。测试记录传输边界的 initialize → notifications/initialized → tools/list → tools/call。该事实取代设计时的 v2 选型建议；没有声称当前采用 v2、modern discovery 或 2026-07-28 协议。升级需独立 conformance，不能混用两代 API 或只写版本常量冒充真实通信。

MCP transport 不自动提供业务身份。每个 stdio session 固定一个可信 principal、run、role 与完整 scope，不靠模型参数切换身份。当前 server 验证 0600 task-local bootstrap、lease deadline、spec/hash、同源码 scan receipt 与 tool grants，并拒绝 body 替换身份；provider 生命周期撤销阻止旧 attempt 迟到创建。生产持久 capability/revocation service 仍待实现。内部 bootstrap 不进入 sandbox 或模型 context，env 不带 provider/connector 私钥。敏感 dispatch 仍经数据库原子 fenced claim。后续 public HTTP 需 authenticated service channel、origin/auth 检查；隐藏 tools/list 不代替 tools/call 授权。

工具schema示例：`knowledge.search(query, project, top_k)`、`memory.search(subject, types, as_of)`、`code.propose(source, language)`、`sandbox.execute(check_template_id OR code_artifact_id, input_manifest_id, resource_profile)`、`issues.create(proposal_id, approval_id)`。sandbox的两种code来源严格oneOf，code.propose只注册scoped immutable blob不执行；generated-code仅允许授予该capability的role和固定language/interpreter，随后同样scan/policy/approval/isolation。tenant/role、image、mount、env、shell、目标repo、Issue body不能由未验证参数自由替换。所有server操作再次enforce；长执行返回durable handle；不默认用SDK Tasks替本项目worker。

## Sandbox 契约

`ExecutionSpec`固定image digest、模板script hash或generated code blob hash、input manifest hash、受控interpreter/argv、resources、network policy、artifact allowlist、run attempt。S1/S2仅批准模板；S3真实model产生source并由code.propose存不可变scoped blob，走scan/policy/必要批准/isolation，绝不在host运行。execution必须引用扫描的同一code hash，改code要重scan/重批。generated产物再由独立可信checker核验，不能靠model生成程序自报“test pass”进入Gate。

已实跑 checker 固定 profile：UID/GID10001、read-only rootfs、drop ALL capabilities、no-new-privileges、默认seccomp、network none、0.5CPU、256MiB RAM/同额swap上限、32PIDs、20s wall timeout、64MiB workspace tmpfs、有限/tmp tmpfs、stdout/stderr各256KiB、单个 result.json 最大2MiB；通用多文件产物上限仍是后续设计。scanner独立resource profile，经benchmark锁定，仍有wall/RAM/PID等上限。每项从inspect/运行核实，不以argv存在代替验证。

容器设置per-container `log-driver=none`，controller通过bounded attach逐chunk接收stdout/stderr并限额保存到scoped evidence；不得用unbounded communicate()/Docker默认json-file吞无限日志。溢出标OUTPUT_LIMIT并停止任务，保留限额内partial日志；control plane中断造成未接收部分丢失必须显示unavailable。inspect验证logging配置；不改daemon全局设置。这项依据[Docker logging](https://docs.docker.com/engine/logging/configure/)，none+attach 已有真实 provider 测试和 inspect 证据。

input为只读精确挂载的合成manifest或container copy；workspace/tmp为有size限额tmpfs，与host工程隔开。不挂Docker socket/home/env/config/数据库；不publish ports，不privileged，不host namespaces。

固定artifact lifecycle是“模板runner结束check子进程但保持容器活着→controller确认结果并pause/freeze→限额导出allowlisted regular files→核hash/收集receipt→stop/kill→remove→cleanup receipt”。实际 Docker cp 不能复制 tmpfs，故 S1 经 review 改为精确单文件受限 bind；freeze 后 O_NOFOLLOW/regular/nlink/inode/size/hash/JSON 核验，见 08。通用 tmpfs 多文件导出仍待实现；不盲docker cp整个目录，不一般extract tar，只从bounded stream解析允许的regular entry，拒绝traversal/link/special/depth/size违规。冻结后拷贝避免同UID恶意进程改源的TOCTOU。若freeze/export失败，阻止success并cleanup；若timeout/OOM/crash已导致container stop，tmpfs内容消失，artifacts明确UNAVAILABLE，不虚造失败文件。bounded attach已接收日志仍可留存。此生命周期依据[Docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/)，上述单文件修正已实跑；不声称任意生成代码或多文件导出已跑通。

generated code的stdout/artifacts视为不可信数据，不能因其自称pass成为Gate事实；只接受已审查模板/独立检查器和可验证原始来源的typed结果。保持runner可用不代表其与同UID恶意code之间有第二层隔离；runner被杀/结果不可信时失败，不能推断成功。

ExecutionResult 有 provider_handle、spec/input/image/script hashes、start/end、exit_code、timeout/cancel/OOM、cleanup_state、truncated logs 与 artifact manifests。kill/timeout/cancel 都等待停止证据，cleanup fail 是可见失败。normal checker exit 0 只证明 checker 成功，不直接证明所有 Gate required evidence 满足。

## 角色最小权限（最终目标）

| Role | Knowledge/Memory | Sandbox | DevOps/办公 | Stage approval |
|---|---|---|---|---|
| Supervisor | 任务级 summary/允许的共享资料 | 无直接执行 | 无直接写 | 无 |
| PM | PM 项目 scope | 无 | propose Issue/report，经批准 adapter 执行 | 无 |
| Architect | 架构 scope | 批准的 read/test profiles | PR/commit read | 无 |
| Android | Android/build scope | build/test profiles | PR/CI read；写 action 另批 | 无 |
| SRE | SRE/CI scope | health/log/test profiles | Actions read；写 action 另批 | 无 |
| Hardware/NPI | BOM/lab scope | manifest validator profiles | propose Issue / submit evidence | 无 |
| Human reviewer | 按本人 scope，不自动全租户 | 无 agent 任意代码权 | 确切 proposal 的副作用批准 | 对指定 stage 的批准 |

Role report 只有已授权可共享字段才能汇总。跨 role 共享不因“同一 run”失去 classification/ACL；Supervisor 和最终人类 report 无权看到的 source excerpts 保持 redacted。知识密级不因 summarize 降级。
