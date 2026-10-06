# S2A 契约、威胁与验收增量

基线模块契约、威胁模型、复用盘点和分阶段计划仍见 Oct5 的 `docs/01` 至 `docs/06` 及当日封存。本文件只描述 Oct6 新增的本机模型分析，未声称完整企业平台或五角色量产流程已经交付。

## 模块契约

| 模块 | 输入与可信决策 | 持久产物／明确边界 |
| --- | --- | --- |
| `analysis_contracts` | 固定四阶段 JSON Schema；拒绝额外 authority 字段、重复key、非有限数字 | PLAN问题≤100；最多2查询/top_k≤5；报告3事实/1风险/1未知/1建议，每句≤80/单一引用；汇总≤500 |
| `analysis_context` | caller与服务器铸造的supervisor/pm角色、精确合成ExportGrant、已完成S1来源 | ACL交集在正文加载/排名前；版本/hash/有效期/whole-run ACL；opaque引用；分享和发表事务内重核；context_revision绑定 |
| `analysis_provider` | 固定本机endpoint、qwen3:8b/digest、授权后的messages/schema | Ollama HTTP、think=false、零回退/重试；前后检查模型元数据；unknown不等于确认失败；没有请求级密码学模型revision证明 |
| `analysis_store` | 与S1共用Store/事务/DB时钟，全scope FK、creator与key、source/material版本 | 新增runs/calls/events/provider-slot四表；intent先commit；lease/fence、最坏预算预留、known usage和发表分开；unknown跨进程占槽 |
| `analysis_workflow` | 动态职责Skill和固定角色工具交集；固定四步 | 规划→检索提议→实际服务→报告→汇总；实际输入/schema hash；限额和引用校验；不改S1业务对象 |
| `analysis_api` / CLI | 复用S1 DEV_ONLY身份与原Store；固定loopback8769 | 新分析、查询、events、取消／恢复；无任意provider、角色或批准入口；旧S1接口和批准材料不变 |

模型的 tool proposal 是数据，不是执行权。Knowledge/Memory 的真实读收据为 `IN_PROCESS`，Ollama为HTTP；独立MCP握手与工具调用仍由S1记录。MCP来源投影要求匹配scope/run、请求/响应ID/顺序、消息hash、工具参数和持久ExecutionResult，合格仅标 `RECORDED`，不是沙箱安全证明。

## 威胁与约束

| 威胁 | 当前控制 | 实际限制 |
| --- | --- | --- |
| 来源prompt injection/伪角色/伪工具 | 结构化输出、最小角色工具、Skill允许项交集、固定调用次数 | 提示词本身不是安全证明；自由文本仍可能错 |
| 跨租户、私有报告共享、检索候选侧漏 | 全scope FK、caller∩role预检索ACL、whole-report来源重核、审计来源绑定 | DEV_ONLY应用层授权；未交付生产SSO/RLS/多人部署 |
| 合法引用支持错误结论 | 精确source版本/hash、typed硬件事实、独立Gate与批准/receipt | 语义蕴含尚无完整验收；实际PASS报告有unsupported未知声明，保留失败 |
| 网络/daemon配置/模型被替换 | loopback固定、trust_env=false、不redirect、local metadata及digest前后检查 | 依赖可信本机daemon；不能防有主机管理权的替换/TOCTOU；模型进程不是Docker沙箱 |
| 超时、崩溃、HTTP500后重复发起 | 持久dispatch intent、保留unknown槽/预算、晚到reply拒发表、无自动重试 | 本次模型unknown故障用TEST_DRIVER验证，未假称真实Ollama请求级远端停止证明 |
| 扫描成功被误当安全通过 | S1固定模板及Docker隔离独立验证，source保留其状态 | 不执行任意LLM生成代码；Semgrep PASS只表示有限扫描结果 |
| 验收脚本优化模式跳过检查 | HTTP/证据collector用显式条件，`python -O`坏响应负例拒写结果 | 测试`assert`只用于pytest断言；不作为运行时权限／证据验收控制 |

预算为四次调用、输入保守上界3500、单次输出800、单次60秒、整链180秒、profile并发一。UTF-8字节数加余量是保守估计，实际Ollama token用量另存；没有精确tokenizer或计费金额确认。取消本地等待不能证明远端停止；unknown不得自动释放成可重试。

## 验收矩阵

| 项目 | 证据类型 | Oct6结果 |
| --- | --- | --- |
| 旧S1正常/阻断/未知/真实timeout/worker SIGKILL恢复/拒绝/幂等/跨scope | 真PostgreSQL、Semgrep、stdio MCP、Docker；审批者为SCRIPTED_TEST_HARNESS | 136通过，0skip；新目录保存，不覆盖旧证据 |
| 四阶段正常、伪引用/authority/工具、JSON错误、越界、取消、中断/HTTP500unknown槽、撤权 | 新S2 TEST_DRIVER，真实Store/SQLite、Mock HTTP | 测试XML与review标注具体覆盖；不计真实模型质量 |
| 台账并发、租约、source重新授权、scope FK及幂等 | 真PostgreSQL＋TEST_DRIVER材料 | 43通过；不调用模型、不改变实际Aurora业务状态 |
| MCP来源投影与异常顺序/消息语义 | 真实历史receipt离线重校验＋有效hash腐化 | 15通过；不声称本次新增15次真实MCP执行 |
| PASS/BLOCKED/UNKNOWN模型分析 | 真qwen3:8b、实际S1检查与检索服务、PostgreSQL | 各4次VALIDATED；分析state分别COMPLETE/INCOMPLETE/INCOMPLETE；Gate仍PASS/BLOCKED/UNKNOWN |
| 跨进程同key重放 | 实际PASS03新CLI进程 | 相同analysis及4条call，无新增生成 |
| localhost API、匿名／伪headers、版本批准和零receipt边界 | 实际HTTP，最终服务PID及`python -O`证据 | 200读取，未认证均401；PROTOTYPE/pending/0receipt |
| 完整模型质量、冻结12cases/13scenarios、47题语义检索改进 | 尚未执行 | 不因三条链完成而冒称全测通过；原词法失败保留 |

旧context版本会报 `ANALYSIS_CONTEXT_CHANGED`：它不能被当前版本当作仍有效的运行材料继续发表。早期attempt01的输入超界失败、attempt02的错误MCP投影引出的INCOMPLETE报告保留JSON归档，原记录未删除或改写。

## 下一阶段的可执行顺序

1. S2B先把关键模型断言改为可对照typed证据的字段，增加实例未知/规则未知、候选Memory/确认事实的区分；冻结正负案例，报告实际准确率和失败。批准材料变更另立版本，仍由用户决定。
2. 再进行冻结12cases/13场景评估与既有47题检索比较，决定是否需要embedding/混合检索。当前无已安装embedding，不擅下载、购买或接私人云密钥。
3. 按业务必要性增加有实际source/policy和可验收任务的架构、Android、SRE等角色，逐步覆盖Prototype/EVT/DVT/PVT；不预先堆空壳角色。
4. 真实GitHub/办公写入适配必须先有具体权限、目的地、参数版本和未知结果reconcile方案；生产身份/RLS/部署单独验收。

每一步继续提供手动演示、短讲解、本人修改练习和明确的已实现/模拟/待接入状态。
