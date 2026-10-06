# S2 最小模型与 Embedding 集成契约（准备稿）

状态：**设计准备，MODEL_NOT_RUN / EMBEDDING_NOT_RUN**。本文只新增 preparation 文件；不修改或重建 S1 源码、文档、证据或 manifest，不调用 API、读取 secret、下载模型或启动推理。S1 能力与限制以 [09 实现审查](../../docs/09-implementation-review.md) 为基线。这里的 S2 指下一次有限模型集成，不把原计划的后续全功能范围改成已完成。

## 1. 首次接入的有限范围与事实权威

建议分两次收敛：**S2A 先真实 Supervisor + PM，hardware 仍是确定性 checker；S2B 在独立 ACL/policy review 后加入真实 SRE。** 真 Embedding/hybrid 是独立的授权与验收增量，可并行准备，既不借 SRE 授权出境，也不因 SRE 尚未激活而伪造向量结果。S2A 中 Supervisor 规划有限 PM 任务，可信控制面通过已经授权的 S1 hardware 路径或已有当前结果提供 immutable CHECK_COMPLETED/result/snapshot，PM 分析可读证据，Supervisor 汇总。模型计划不是新的执行授权。新产物是来源化 analysis report 和 provider trace，不新增或自动触发 Issue、stage transition、外部写入、代码执行或 confirmed Memory。现有硬件检查、Gate、审批和 receipt 仍各自承担权威职责。

PM 必须检查需求/验收项与现有证据的覆盖、冲突、缺口，并形成风险和整改草案；Supervisor 必须产生有限任务分派、处理缺失/冲突并保留角色证据贡献。不能给同一段模型输出贴多个 role 名称。S2B 的 SRE 才分析已批准向其提供的 CI/执行/timeout/cleanup/provenance 信号，明确“测试通过”与“系统可靠性已证明”的区别。每个已激活 role 第一轮可提出最多两条只读检索请求，再看真实 tool result 形成最终报告；S2A 不声称 SRE 实跑或所有角色已齐全。

当前 [policy.py:12](../../src/forgeflow/policy.py#L12) / [policy.py:21](../../src/forgeflow/policy.py#L21) 仅 hardware 可 sandbox.execute；[Workflow.service:47](../../src/forgeflow/workflow.py#L47) 持有 hardware/worker，**不得作为 PM/SRE/Supervisor 身份复用**。S2A 的待审查增量仅为 Supervisor 的 bounded analysis.delegate(PM) 和 Supervisor/PM 的 model.generate 内部 capability。S2B 再审查 SRE 的 knowledge.search/memory.search/model.generate、delegate(SRE) 及其具体可读 sources。模型不能授予这些能力；文档没有将增量批准。

S1 的 SOP/source ACL 不含 SRE，且 [store.py:533](../../src/forgeflow/store.py#L533) 对 whole run/result 的 context 重新授权。SRE 默认不得读它们，不能让 Supervisor 或 hardware service 复制 whole result、放宽为 admin 或先取全量再删字段。S2B 需新增有限 ReadinessSignal view（如 CI_RESULT/EXECUTION_STATUS/CLEANUP_STATUS、typed value/status、observed_at、精确可解析 signal citation），逐字段审查来源权限、classification、reader roles 与 export grant；signal 的来源关系在服务端保留。只有经明确允许向 SRE 分享的信号及可解析来源才能发布，原资料若仍禁止任何对应衍生细节则信号也不发布，SRE 保持 PENDING_POLICY_REVIEW。新 view 不修改封存 S1 的 ACL 或假称 source redaction 自动授予读取权。

SRE/PM/Supervisor 都不获得 sandbox.execute。可信模板的原 hardware 路径继续独立；模型建议执行时只生成非执行建议，不能把它转成硬件 principal。当前 MCP 仅 sandbox.execute；本次 Knowledge/Memory/analysis delegation 可先用真实受控服务接口，并记录 transport=IN_PROCESS_SERVICE，不冒称新增 MCP tools。**不新增 code.propose、source execution、shell、任意 path/image/env 或 generated-code capability。** 模型生成的代码文字也只是不可信文本。

技术 Gate 仍由 [gate.py:23](../../src/forgeflow/gate.py#L23) 与独立可信检查产生。模型 COMPLETE 只表示分析任务完成；它不能设置技术 PASS、审批决定或 receipt。若模型/Embedding 不可用，保留原始技术 assessment 的实际值，同时 S2 analysis 显示 INCOMPLETE/FAILED/OUTCOME_UNKNOWN，不把 S1 确定性结果伪装成 S2 成功，也不自动切换成“模型已运行”的 fixture。

## 2. 拟议 Provider profile 与执行上限

父任务只读 metadata 报告：Apple M4/24GB，Ollama 0.32.1 的 127.0.0.1:11434 响应正常；已有 qwen3:8b，digest 为 `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`。这不是推理、速度、质量或无云路由的验收。建议首选该**本地固定 digest**；app 不共享角色会话，不接受模型输出中的 provider/model/URL，不允许云 fallback。cloud_mode 配置尚未核验，真实模型调用授权也未获得。

Embedding 尚未安装。qwen3-embedding:0.6b（父任务估计约 639MB）仅为待批准下载的候选；exact digest、license、维度、tokenizer、归一化和 adapter 行为须在批准与核验后锁定，当前不填假值。API 备选默认 disabled，本文不引入未核验的 API 版本、价格或密钥位置。

与父任务 provider decision 对齐的**拟议上限，不是能力实测或批准**：模型并发 1；每 call 独立 context，配置 context 8k；输入最多 3,500 token、analysis 输出最多 800 token（比 provider 候选 1,024 cap 更紧）、单 call wall 最多 60s，analysis 总 wall 180s。实际 call deadline=min(60s, analysis remaining deadline)，预算不足则不 dispatch；不是允许每次调用都花满 60s 后再突破全局上限。S2A 最多 4 次生成（Supervisor plan、PM proposal/report、Supervisor summary），S2B 最多 6 次（另加 SRE proposal/report）；每个已激活 role 最多一轮检索、每轮最多 2 个 proposal、每次 top_k<=5；零次自动 schema-repair、网络重试或额外思考循环。输入计数必须包含 system/skill/schema/tool result；输出/预算必须涵盖 provider 的实际生成开销，不能忽略隐藏推理或把字符数伪称精确 token 数。能力未核验或请求超过 profile 时拒绝 dispatch。

首次 index 只取一个批准 full scope 的 <=32 个当前合成 chunk，每 chunk <=256 token、每 batch<=8；query embedding S2A 若单独获准则每 analysis<=2 次，S2B<=4 次。Embedding timeout/维度等从已核验 profile 取值，不能假定候选模型支持某 API 参数。API 备选的 total $5 / per-run $0.10 仅为父任务建议，未批准也未启用；付费 profile 如以后选择，必须另锁 currency、price revision、最大费用和 worst-case reservation，未确定费用上限时禁止调用。已核验的本地无云 profile 可标 LOCAL_NO_BILLING，仍记录 token/时间/资源；不能因为 endpoint 在 loopback 就推断无远程费用。

## 3. 最小接口（拟议，不是已有实现）

Provider adapter 提供 `async generate(AuthorizedCall) -> ProviderReply`、`async embed(AuthorizedEmbeddingBatch) -> VectorBatch`。它只执行已批准固定 profile 的 bounded 请求，不自行取资料、调用 tools、重试、发布 output 或修改 workflow。control plane 负责身份、context、budget、lease、cancel、状态和校验。严格 schema、字段限长、有限数组、UTF-8、有限数值与 additionalProperties=false；拟议 raw response cap 128KiB，拒绝重复 JSON keys、截断输出和多余正文，不能用宽松字符串提取伪造 structured success。

ProviderReply 最少为 `{delivery_status,content_utf8,provider_request_id?,actual_model_revision,finish_reason,usage:{input_tokens?,output_tokens?,cost_microusd?,state}}`，provider metadata/usage 来自 adapter 可核验响应，不能取模型正文里的自报值；未知值保留 null+UNKNOWN。actual model/profile 不符、不能核验 pinned digest 或 finish_reason 表示截断，都不能产生 validated report。RoleReport/RolePlan 是 content 的独立严格校验结果，不是 adapter 自行发表的事实。

生成调用的内部 envelope 示例；这些 identity/limit/grant 字段由 control plane 填写，不是 model 或用户可选的 provider 参数：

```json
{
  "schema": "model-call-v1",
  "call_id": "call-opaque-id",
  "scope": {"tenant_id": "test-lab", "workspace_id": "engineering", "project_id": "aurora-s2"},
  "run_id": "scoped-run-id",
  "analysis_step_id": "pm-report-step",
  "role": "pm",
  "phase": "FINAL_REPORT",
  "provider_profile_id": "local-qwen3-8b-pinned",
  "context_snapshot_id": "authorized-context-id",
  "output_schema_id": "role-report-v1",
  "export_grant_id": "trusted-grant-id",
  "budget_reservation_id": "reservation-id",
  "limits": {"input_tokens": 3500, "output_tokens": 800, "deadline_ms": 60000}
}
```

Provider 只收到该角色已批准导出的必要消息与 schema，不能拿到 principal/token/连接串、其他角色 session、整个数据库或宿主路径。context snapshot 固定完整 scope、原请求/输入 hash、source version/hash/status、classification、ACL/export policy revision、role/skill/schema/model profile；資料、Memory、tool result 都标为不可信证据。source status 为 EXPIRED/CONFLICT/CANDIDATE 时必须保留，不通过 summary 自动升为事实。

Role 的第一轮输出为 `ContextProposal{requests:[{tool,arguments}]}`，tool 只允许 knowledge.search 或 memory.search，arguments 仅 query 和 top_k；第二轮输出为下列有限 RoleReport。role、scope、call_id、model/digest、上下文及 Skill hash 由可信 envelope 包装，禁止让模型自填后当权威。正常 JSON 示例是结构说明，不是推理记录：

```json
{
  "status": "COMPLETE",
  "facts": [{"claim": "CI smoke 的来源记录标为 MEASURED", "source_refs": ["c1"]}],
  "risks": [{"code": "RELIABILITY_NOT_PROVEN", "severity": "medium", "source_refs": ["c1"]}],
  "unknowns": ["尚无长期稳定性证据"],
  "recommendations": [{"kind": "LOCAL_ISSUE_DRAFT", "text": "补充长期稳定性验证计划", "source_refs": ["c1"]}]
}
```

RoleReport 的 status 仅 COMPLETE/INCOMPLETE；facts/risks/unknowns/recommendations 各<=10，单条文本<=500字符，每条 refs<=5，总输出按 profile 限额。refs 必须来自本角色 snapshot 的 opaque citation map，解析到精确 source/version/hash 并重授权；未知 ID、cross-scope、已变更/不可读来源拒绝报告。claim 文本是模型解释，只有原始 typed evidence 才能进入 Gate；存在 citation 不自动证明 claim 正确。

Supervisor 输出 RolePlan={assignments:[{role,task_question,source_refs}]}：S2A 仅允许一次 PM；S2B 审核通过后要求一次 PM 和一次 SRE。缺必需角色、重复/未知/未激活角色或增加 tool/budget 属 INVALID_PLAN。handoff 的 scope/clearance/export 由原授权和目的角色 grant 交集得到。SupervisorSummary={analysis_status,report_refs,facts,risks,unknowns,recommendations}，analysis_status 仅 COMPLETE/INCOMPLETE；report_refs 只能是该 session 已授权的 opaque report IDs，其余数组采用 RoleReport 相同约束。最终 analysis 状态由控制面按必需角色/验证结果决定，模型 COMPLETE 不覆盖缺失或失败。

Supervisor summary 只能读取向其重新授权的 report/context，不能自动读取 PM/SRE 私有来源的并集；派生报告继承来源分类及访问限制，不能借删 citation 降级。不可见报告只返回安全缺口状态，不泄漏正文、source ID、hash、计数或私有冲突。gate_status、approval_decision、scope/role 自报授权、source execution 等不属于 model output schema，应在 negative tests 中拒绝。

## 4. Tool proposal、async 调用与持久恢复

模型只提 proposal。gateway 在调用前再次验证：可信 role/完整 scope、capability+Skill 交集、schema/allowlist、source freshness、剩余 budget、cancel、当前 lease/epoch；任何 model identity、approval、URL/path/额外 flags 或 sandbox proposal 均拒绝。检索 query 也是 derived data，继承其上下文分类，不因改写成短问题而自由出境；tool result 只能加入同角色已批准 context。真正工具调用及 denial 都记录来源和 transport，模型声称“已调用”不算 receipt。

新 S2 实现另设 `analysis_steps` 和 `provider_calls(kind=GENERATE|EMBED)` 的 scoped 持久记录，由单一 schema owner 编写/审查。它们关联原 Run/immutable source snapshot，不覆盖 S1 result/assessment/approval。logical call 唯一键至少含 full scope、run、analysis step、phase、input/context hash；重复请求复用已验证结果时必须重授权，不靠全局 prompt cache。source、policy/skill/profile/schema 改动要求新的 logical call/material revision。

dispatch 前以一个本地事务原子完成：新鲜 lease/epoch + cancel 检查、当前 ACL/export grant/material 校验、run/role/provider 并发槽及 worst-case budget reservation、immutable call intent 和 DISPATCHING 记录。事务提交后才让 async adapter 发请求；不得持 DB transaction 等待网络。adapter 禁止 SDK 隐式 retry，受 monotonic wall deadline、response byte cap 和有界取消控制。回复持久化须再次校验 lease/epoch/material；迟到旧结果可作 restricted reconciliation evidence，不能覆盖新 step 或发表 role report。

adapter 不得 implicit heartbeat/lease renewal，等待网络不延长 lease、call deadline 或 analysis deadline。若后续实现需要续约，只能由可信 orchestrator 显式执行受审查的 DB renewal：当前 lease 尚未过期、epoch/owner 匹配、cancel 未请求、material/export grant 仍有效且仍有剩余 wall/token/cost 预算；续约不能复活旧 lease 或延长全局上限。每个 call 的 deadline 同时不晚于当前 lease、grant expiry、analysis remaining deadline，剩余时间不足就停止新 dispatch。

最终 publication（RoleReport、handoff、Supervisor summary 或派生 candidate）前，在最新锁定/CAS 状态再次核 cancel、lease/epoch/owner、source/ACL/export/material revision 与剩余 deadline；不能只使用 dispatch 前 snapshot 或“回复已到”的判断。cancel/过期/换代/来源撤权后，已付费有效回复也不得发布或把 analysis 标 COMPLETE；按原 call 保留 usage/unknown 和受限迟到证据，费用不会因为 publication 被拒而变 0。

| call / usage 情况 | 必须行为 |
|---|---|
| dispatch 前 DENIED、budget exhausted、cancel 或 export revoked | 不发送 provider 请求，不产生模型报告；记录安全原因。 |
| 已收到有效回复且 schema/ref/context 校验通过 | 持久化 validated reply/hash、usage 与实际 model/profile；只有该 role 完成，不设置 Gate/approval/action。 |
| 回复截断、JSON/schema/ref 校验失败 | OUTPUT_INVALID，保留已发生调用及费用状态；不伪造 JSON、不自动修复重试，也不把旧 fixture 当此次模型结果。 |
| 连接失败/timeout/cancel/crash 后请求可能已到达 provider | OUTCOME_UNKNOWN；保留 reservation 与请求时间/可用 request ID，停止该 logical call 的自动再发，显示 analysis 不完整。取消 async task 不证明远程生成停止或没有计费。 |
| provider 明确返回 error | 与网络投递和计费分别记录；只有明确确认未受理/未计费的证据才释放相关 reservation。error 文本不能推导“零费用”。 |
| reconcile | 若 provider 有可核验 request/usage lookup，绑定原 call 后收敛；否则保持 unknown/人工决定是否批准新的费用风险与新 logical call，不标 exactly-once。 |

Call outcome 与 `usage_state=CONFIRMED|UNKNOWN|NOT_DISPATCHED` 分开；未报告 token/cost 不能填 0。未知费用占用至少 worst-case 预算，不能先释放再无限调用。in-flight/unknown 也保留 provider 占用标记；未确认原调用完成或停止前不能假称并发槽已释放并继续该 profile 的新请求。模型和 embedding 共用 run 预算 ledger，但分别有 profile/次数/数据出口授权。provider 没有 fencing 时，dispatch 后取消或授权撤销无法保证“零迟到请求”；必须记 in-flight/unknown，并拒绝继续发布或自动重发。

## 5. Embedding 出口授权与检索权限是两个边界

**拥有 source read ACL 不等于可以将其发送给 Embedding 或 LLM provider。** 本地 provider 也必须具备目的明确的处理授权；外部 provider 另需目的地/数据分类/保存地域等明确许可。trusted ExportGrant 至少绑定 full scope、actor/service/role、exact destination+model revision、purpose（MODEL_ANALYSIS/QUERY_EMBED/INDEX_EMBED 分开）、允许的分类/source 范围、expiry/revocation revision 与 budget；未知分类或 grant 缺失一律 DENY。授权对象包括 system/Skill 私有材料、用户 query、检索 chunk、Memory 及派生 tool query，不能只过滤最终 answers。

IndexEmbedding 输入只来自具有 index capability 的独立 service 的授权当前 source version，不借用户 role 或全局 admin 扫全库。每 batch dispatch 前对每个 chunk 核 full scope、read/index ACL、source hash/version/validity、destination export grant；任一不合格先分离拒绝项，不能混在同 batch 发送。dispatch 前拒绝/过期/撤销须有 zero-provider-request 证据；dispatch 后 source/ACL 变更不能“收回已发送内容”，记录事实并禁止新 index generation 激活，按已批准 retention/retraction 策略处理。

EmbeddingBatch 内部输入至少为 `{job_id,scope,model_profile_id,index_generation,export_grant_id,items:[{chunk_id,version,source_hash,text}],limits}`；每项 hash 必须对应 chunk 原始 bytes 与可解析来源，不能用截断文本配原文件 hash。query embedding 同样受 query 的来源分类和 QUERY_EMBED grant 控制。输出 VectorBatch 返回按输入 ID 对齐的有限 float[locked_dimension]、exact model/revision、usage/request ID；数量/维度/hash mismatch、NaN/Inf、选定 metric 不合法的向量拒绝整批，不填 hash/随机/零向量冒充真实 embedding。

现有 [store.py:297](../../src/forgeflow/store.py#L297) 只是 JSON vector 的 schema port，不等于 pgvector。后续新实现须锁 model/digest、dimension、normalization、metric 和 index generation，再由 schema owner 设计实际 pgvector storage；同 generation 不混 model/revision/维度。source 更新/删除/权限变更使对应旧 generation/entries 不能充当新事实。向量和缓存本身也按来源保密，cache key 保留 full scope、授权/ACL revision、model revision/generation 与 query/source hash；不能跨用户/角色无条件复用。

Retrieval 与 outbound embedding 各自执法：keyword 和 vector 通道**分别**在 ranking/top-k 前建立 live full-scope+role/classification+current-source 的授权候选集合，初期采用该集合上的 exact vector ranking，不做全库 ANN 后过滤。再 RRF（拟议 k=60）融合，最后按 [context.py:143](../../src/forgeflow/context.py#L143) 的精确 citation 重授权/version/hash/status 校验；模型 context export 还有最终独立授权检查。Embedding export 合格不授予检索权，检索权合格也不授予 export 权。模型/向量相似度都不解决事实冲突或将 UNKNOWN 改 PASS。

## 6. Trace、日志与发布

默认持久 trace 只记录 scoped call/step ID、role、model/profile/digest、schema/prompt/context/source hashes、export/budget policy revision、timing/status、request ID、真实 usage/unknown、tool proposal/decision 和验证结果。Authorization、cookie、API key、连接串、宿主路径、provider error headers、原始全量 request/response 不能进入日志；错误只取 allowlisted code 与已清理短原因，不能直接 repr(exception)。不要求也不储存隐藏思维链；保存可核验的短解释与 citations。

验证后的 report/context 如需保存内容，进入继承来源 ACL/classification 的 scoped storage；原始 prompt/raw output 默认不落盘，必要诊断须另有导出/保存许可、最小 retention 与受限入口。不能用“已做 redaction”替代 export grant，也不能把敏感正文 hash 当作公众可见的无害元数据。API report、events、artifact、Memory 和 source resolve 对派生内容重复授权；不在公共 health/log 暴露 source IDs、prompt、private counts。所有日志使用合成 canary 来验收，不通过挂载真实密钥来测试。

## 7. Decision gates 与验收包

| Gate | 必须决定 / 证据 | 未满足时 |
|---|---|---|
| D1 Provider/data authority | 人类批准首个 local generation 调用范围；核验 pinned digest、固定 loopback、无 cloud fallback、真实 adapter 语义与可 enforce 的 caps。Embedding 下载/使用独立批准，核 exact artifact/license/profile；API 保持 disabled。 | 只继续 preparation 与明确 TEST_DRIVER 的本地 contract tests；不调用、下载、读取 secret。 |
| D2 Policy/context | S2A 审查 Supervisor delegate(PM)、两角色 model.generate 和各自 context/read/export matrix；S2B 另审查 SRE-readable signal schema/具体source ACL 与 SRE read/model grant。绝不新增 sandbox/generate-code grant。 | SRE 保持未激活；无授权角色/目的地返回 DENY，不能复用 hardware service 或复制 whole report 绕过。 |
| D3 Durable implementation | 后续明确授权 S2 实现时，由唯一 writer 在 canonical 唯一代码原件继续演进 scoped call/analysis/budget persistence、async adapter、验证器与 embedding/pgvector repository；S2 另记新 hash 和 regression 结果，S1 archive manifest/evidence 不重写。 | 本轮 preparation 不修改 S1 任何封存项，不复制另一套 S2 backend，也不把本契约称为空壳代码已实现。 |
| D4 Real acceptance | 下列真实模型/embedding traces、negative/invariant checks 和 fresh read-only review 全部可核验；本地 fixture/TEST_DRIVER 与真实 provider evidence 分开。 | S2 保持 NOT_RUN/INCOMPLETE；S1 有限完成状态不变化。 |

最小验收计划：

S1 无 Git 历史是当前事实；未来实施所需 source history/versioning 策略单列决定，本轮不 init/commit。这里的“封存”保护旧 archive/evidence，不禁止今后授权后的 canonical 代码演进。

1. **真实职责链**：至少 normal、blocked、unknown 三个 Aurora 输入；S2A 保存最多 4 次真实独立 model call、Supervisor plan、PM query/tool result/report、Supervisor summary，hardware 的确定性事实另记。S2B 在 signal/ACL/policy 审核后才验收 SRE 的额外两次真实调用与职责贡献；此前 SRE model cases 为 PENDING_POLICY_REVIEW，不能计为成功。每个事实引用可解析，当前固定 hardware 检查仍是唯一执行路径。
2. **授权与出口**：跨 tenant/同租户不同 project/role；禁读 source 为最强 keyword/vector 命中；检索和引用都不泄漏。read-allowed/export-denied 的 query/chunk/Memory、过期或撤销 grant 均在 dispatch 前 zero requests。PM 私有 report 不能经 Supervisor summary 泄漏。
3. **模型失效**：未知 tools、SRE sandbox.execute、role/URL/path 注入、超预算、invalid/截断/重复 keys JSON、假 citation、prompt injection、provider error/timeout/cancel；无自动 retry/fallback、無假 PASS、无新业务副作用，原技术事实与 analysis 状态可区分。
4. **付费/恢复**：有界 TEST_DRIVER 精确模拟请求受理后掉线及 worker crash；unknown 保留预算、旧 lease 不能发表/再发、重复 logical key 不重复 dispatch。真实 provider 验收另验证其实际 usage/取消行为，不把模拟 unknown 等同真实 billing lookup 已实现。
5. **真 hybrid**：锁定合成语料与 held-out >=30 问，含同义/中英/精确ID/版本/无答案/冲突/权限 negatives；比较 keyword、真实 vector、RRF 的 Recall@5/MRR@5/citation validity/latency，报告分母与失败，不为达标修改答案。source 变更/删除及 dimension/model revision mismatch 均使旧结果失效。
6. **保留边界**：后续实现的 S1 regression 与新 S2 tests 均执行并归档环境/profile/输入 hashes/命令结果；S2 不声称任意生成代码、全部五角色/全 NPI、外部 GitHub/办公、SSO/RLS或生产完成。

本文交付的是可实施的接口与安全决定边界。首次真实推理、Embedding 下载和 policy 增量仍在 decision gate 前；没有任何模型或向量运行成功 claim。
