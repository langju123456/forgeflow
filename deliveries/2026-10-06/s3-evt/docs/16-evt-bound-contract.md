# EVT 证据组合与绑定评审

当前 API 在既有 Hardware、Architect／Android 与 SRE 证据上生成持久 `EVTReview`。组合 Gate 属于服务端确定性契约，不由模型 prose 或自报状态决定。原 hardware-only assessment 保留为历史记录；新契约没有写回或伪装原检查结果。

固定六项是 hardware、architect、android_source、android_apk、android_device、sre。每项分别记录适用性和观测值。适用项有 FAIL 得到 BLOCKED；否则存在 UNKNOWN 或适用性未定则 UNKNOWN；只有适用项全部 PASS、其他项由登记策略明确排除才得到策略范围内 PASS。

| 登记的合成策略 | APK／设备适用性 | 缺失证据的结果 | 可授予的范围 |
| --- | --- | --- | --- |
| 默认 `synthetic-evt-apk-device-v1` | APPLICABLE | UNKNOWN，不能申请批准 | 合成 EVT 就绪评审；当前未满足 |
| `synthetic-source-review-v1` | NOT_APPLICABLE | 有效项 NOT_APPLICABLE；observed UNKNOWN 和 build NOT_RUN 保留 | SOURCE_REVIEW_ACCEPTANCE；不是 EVT 阶段资格 |
| `synthetic-applicability-unresolved-v1` | UNKNOWN | UNKNOWN，不能申请批准 | 适用性尚待业务定义 |

caller 只选服务器登记的策略 ID；不能提交 exclusion、判定、证据内容或 applicability override。三个策略全部 `full_evt_eligibility=false`、`stage_transition_allowed=false`。source-only PASS 不能授权进入 EVT，更不构成硬件认证。策略在固定夹具中，完整登记文件原始 hash 与所选策略共同绑定 policy_hash。

`EVTReviews.create` 先检查真实 caller 的 scope／创建职责和父 run 完整来源 ACL，再校验两份已持久角色报告属于同一 run。报告保存两份 role review ID／report hash、策略、完成事件、snapshot、result、assessment、readiness step／artifact provenance。`technical_source_hash` 只绑定对应 CHECK_COMPLETED 及之前事件和技术材料；后续人类接受状态不混入技术来源。完整当前事件 head 单独进入审计。读取再次授权、复算来源与角色结果。

人工接受使用独立 `EVTApproval`。proposal 精确绑定 review ID／hash、technical source hash、policy ID／hash、purpose、父 run 与阶段版本。决定要求真实 reviewer、正确 proposal hash、revision CAS、当前来源仍有效、Gate PASS 和未过期；同一 reviewer／决定可幂等回放。真实 reviewer 只要有来源 ACL 就可读取及决定；创建报告和申请接受仍要求 admin／supervisor。审计、proposal 和决定状态有持久 payload hash。

接受记录不进入父 run 的旧 approvals／events，所以决定不会使自身来源失效，也不改父 run、Memory、IssueTracker、阶段或实际业务动作。当前 HTTP 的旧 `/approvals/{id}/decision` 对 APPROVED 返回 `EVT_BOUND_APPROVAL_REQUIRED`，仍允许明确拒绝旧 proposal。这防止当前 API 用 hardware-only proposal 绕过组合评审。

真实边界：这是已经生效的当前 HTTP 评审接受契约；旧 local CLI、S1 stage／Issue 执行适配器尚未迁移到它。源码接受不能交给旧执行器当成阶段批准。执行适配器迁移属于后续本地工程，不是假称已受 SDK 授权阻塞的功能；本批没有执行它。APK／设备取证则确实需要新增工具／设备授权。原 Memory 候选仍来自 S1 检查，新评审不自动写 Memory。

模块契约：

- `EVTPolicies.load(scope, policy_id)`：固定策略与完整登记 hash；无用户路径。
- `evaluate_evt(scope, run, domain_report, sre_report, policy)`：六项结构化组合，无模型／动作权限。
- `EVTReviews.create/get`：scoped 持久评审、来源复算与幂等。
- `request_approval/decide/get_approval`：绑定报告的人工接受生命周期；无业务执行器。
- `evt_api`：保留 runs、本地模型分析和职责接口；替换旧批准入口；loopback／DEV 身份不变。

本批相关测试 67 项，其中新增 EVT 契约／持久化 25 项，原职责回归 42 项。覆盖 UNKNOWN 拒绝、显式 N/A、适用性未定、FAIL 优先、策略变化、scope／run 混用、来源／审计篡改、到期／CAS、人工接受回放以及 reviewer 权限。独立审阅发现的 reviewer 被内部 admin 限制误挡问题已修复并补测来源 ACL 拒绝。

当前完整实现与实际数据库证据见 `deliveries/2026-10-06/s3-evt/DELIVERY.md`。不新增模型或 SDK，不做新 ZIP 测试，历史封存不修改。
