# 威胁模型

状态：设计期；没有攻击测试结果。测试通过只能说明特定环境和夹具中的控制有效，不能成为“安全证明”。

## 资产、攻击面、信任边界

资产：租户资料、历史 decision、审批权限、execution resources、artifact、Docker host、connector/model credential、外部 Issue/群消息、audit 因果链。

攻击者模型：恶意/误导的用户资料、检索文档、Memory、MCP tool description/result、生成代码、skill scripts；越权 role/伪造身份；并发重复提交；断网/宕机/过期 worker。允许把 sandbox 内进程视为恶意。DB/Docker host 管理员属于 trusted operator；此阶段不声称能抵御已接管宿主或数据库的管理员。

信任边界：人类认证 → API；API/worker → role context；role → gateway；gateway → server adapter；control plane → Docker daemon；container → host artifact collector；数据 → model provider；connector → 外部平台；worker → durable state。每个边界都要验证 scope/identity/版本，不能把模型自然语言当授权来源。

## 威胁与控制 / 证据

| ID | 威胁 | 必须控制 | 计划证据与失败处理 |
|---|---|---|---|
| T01 | 请求/body/header 改 tenant/role，猜 UUID | trusted principal；scope-aware 查询与复合 FK；统一 denial/404 | 两租户 same project/name/key，用 B identity 访问 A 的所有对象与引用均拒绝；日志不泄漏 A 内容。 |
| T02 | 检索后过滤导致私有 chunk/计数/缓存泄漏 | 所有通道先 authorized candidate set；rank/cache/citation/context 重新 enforce | 禁止资料置于最强匹配/同名文档；response、tool、prompt、artifact、count 都无泄漏。 |
| T03 | 文档/Memory 注入“忽略权限/批准/发群” | untrusted data 标签；只能 structured intent；业务 gate + gateway deny | prompt injection 夹具下拒绝 action；记录理由；没有外部写回执。 |
| T04 | Skill 路径 escape、symlink、偷偷运行 script | trusted registry、相对 path resolve、目录边界、hash snapshot；script 仅 sandbox | `../`、absolute、symlink、非法 frontmatter、未知/篡改 skill 拒绝。 |
| T05 | MCP server description 提权、参数注入、任意 target | 工具登记 + schema additionalProperties false + structured policy + server enforce | unknown tool、隐藏 tool direct call、额外 flags、repo/path/role tamper 都拒绝。 |
| T06 | 任意 command/image/env/host mount 注入 | templates/resource profile allowlist；argv 无 shell；固定 image digest；拒绝 host/socket/secret mount | 容器 inspect 加 negative 参数测试；不能只用关键词黑名单。 |
| T07 | 利用 root/caps/host namespace/socket 接管 host | non-root、drop caps、no new privs、seccomp、no privileged/host mode/socket | UID/FS/network probe；准确声明容器共享内核剩余风险，后续 Linux runsc/microVM。 |
| T08 | fs escape、secret lookup、读其他项目 | 只读 rootfs，最小只读 inputs，container tmpfs workspace，无 env/key/home | attempts 不能读 host canary/凭据、写只读路径；使用合成 canary，绝不真的把私钥挂进来测试。 |
| T09 | 网络外传、DNS/IP/redirect 绕 allowlist | 初期 network none；无 published ports；allowlist 必须 proxy/firewall + DNS/IP/redirect 校验 | TCP/UDP/DNS/metadata 地址 denied；fixture local/external 不外泄。allowlist 未做不 claim。 |
| T10 | fork/CPU/RAM/disk/output/daemon-log exhaustion | PID/cgroup memory+CPU、swap、wall、tmpfs quota、artifact caps、bounded attach；per-container logging none与inspect，不用默认无限json-file | 受限fork/memory/CPU/大输出/大文件及daemon磁盘增长测试；host不跑fork bomb。 |
| T11 | scanner 被恶意源文件打崩或下载不可信 rules | scanner 也隔离限额；锁定本地 rules/version/hash；关闭非必要 metrics/network；timeout/error fail closed | scan known finding / malformed file / scanner timeout/error；没有“扫描没跑却 pass”。 |
| T12 | artifact traversal/link/tar bomb/TOCTOU或停止后tmpfs消失 | pause/freeze后bounded regular-file export，再stop/cleanup；禁blind目录cp/extract；hash/限额 | 恶意entry拒绝、host canary不变；freeze/export失败不能success；已stop的tmpfs artifact为UNAVAILABLE。 |
| T13 | approval 后替换 action 参数/code/rule/target | proposal immutable hash，包含 scope/action/input/policy/skill/rule/revision/expiry | 修改任一字段旧审批失效；拒绝、过期、重复消费、cross-run 同哈希均正确隔离。 |
| T14 | Agent 自批、Human approve 绕 DENY | reviewer 独立身份与 approver grant；DENY 不可批准；no agent approve tool | role 调 approval endpoint/gateway 被拒；blocked/unknown Gate 不能靠普通批准变 pass。 |
| T15 | worker lease过期仍执行/迟到覆盖/越权dispatch | heartbeat、fencing token、start前handle/labels、停止确认；dispatch原子claim并消费approval与cancel guard | 过期worker不能新claim/consume；kill/reclaim旧completion被拒；已dispatch外部请求可能迟到，明确unknown/in-flight而非零副作用保证。 |
| T16 | 重试导致重复 Issue/通知/费用 | scoped unique logical keys、outbox、receipt、OUTCOME_UNKNOWN reconcile、paid-call boundary | retry/concurrent submit、POST 后 response 丢失；外部不宣称 exactly-once。 |
| T17 | 错误 BOM/build/version/过期资料误判 | immutable provenance、validity、required-evidence matching、conflict resolution、UNKNOWN gate | 缺/错/陈旧/hash不符都不能 pass；LLM summary 不覆盖原始 source status。 |
| T18 | Memory 污染/自我循环引用/未经确认 decision | source refs、promotion policy、候选/confirmed 分开、supersedes有效区间、write roles | 无来源、cross-tenant source、自引用、冲突决策拒绝或 pending；第二 session 显示原始来源。 |
| T19 | 日志/audit/raw model prompt泄密或篡改 | redaction、classification、scope下载、同事务 causation、sequence/hash chain | unauthorized audit read 拒绝，hash改动可探测；hash chain 非防管理员改写保证。 |
| T20 | 清理误删其他项目资源 | namespace + exact handle + ownership label；禁止 broad docker prune/rm | unrelated canary container/目录不变；未知资源不清理，记录人工复核。 |
| T21 | connector data 传第三方模型无批准 | model egress 单独授权、classification export policy、最小 context | forbidden class 不发送；记录 model dispatch 清单/预算，不记录 secret。 |

## 不能声称的保证

Docker 配置不是 gVisor/microVM 实现；Semgrep finding-free 不是代码安全；ACL 测试不是 SSO 完成；本地 db Issue 不是 GitHub Issue；一条 hash-chain 不是独立外部 WORM 审计；一次 fork/timeout 测试不是所有攻击隔离证明。不能为了让 demo pass 关 policy、放宽 resource、使用 host subprocess fallback 或把 UNKNOWN 改 PASS。

如果隔离 provider/scan/权限判断失败，停止对应动作，报告具体 evidence/reason；其余只读/安全工作可继续。最终可用功能集由验收控制，不以 UI 上显示“enterprise”决定。
