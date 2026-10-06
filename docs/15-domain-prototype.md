# Architect／Android 本地原型契约

本批在同一 Aurora run 上新增两份可复算的职责报告，并将当前 API 合并到 8773。输入是既有真实执行回执、合成硬件结果以及单独的受控 AndroidManifest XML。XML 没有经过 APK 编译，也不是硬件 sandbox 的输出；报告分别绑定两类来源。

| 职责 | 当前实际执行 | 本批工具／证据 |
| --- | --- | --- |
| Supervisor | 本地 Qwen 计划与总结；服务端决定状态 | 已持久化 PLAN／SUMMARY 调用及引用 |
| PM | 本地 Qwen 证据选择与报告 | 已持久化 CONTEXT_PROPOSAL／FINAL_REPORT；来源目录与服务端投影 |
| Hardware | 固定检查的确定性 SOP | 实际 Semgrep、MCP stdio、Docker、结果工件与 Gate 回执 |
| SRE | 回执审查的确定性 SOP | 审计、fencing、资源、扫描与清理证明 |
| Architect | 源码契约核对的确定性 SOP | 绑定硬件 BOM 与 XML／合成接口契约；无模型调用 |
| Android | Manifest 源码核验的确定性 SOP | 实际 XML 解析与属性核对；无构建／安装／设备测试 |

前两种职责复用同一个已授权的本地 Qwen 模型。后三个新增职责和 Hardware 不能算成 LLM Agent，更不能据此宣称五种工作角色均已接入模型。当前覆盖一个合成项目的局部流程；完整立项／Prototype／EVT／DVT／PVT 尚未交付。

`DomainArtifacts.load(scope, fixture_id)` 只接受本地合成 scope 和三个固定 selector：`aligned`、`bom-mismatch`、`missing-exported`。返回 XML、contract、版本和原始字节 hash。HTTP 不接受路径、代码、角色、结果、时钟或凭据。`DomainOperations.review` 先复用父 run 的完整 ACL 和事务锁，再读取来源、动态加载两个 Skill，执行双方检查并写入 `domain_reviews`。每份报告绑定 scope、父 run／事件、fixture、XML／contract hash、Skill hash 和控制面版本。主键／外键与幂等约束包含 scope；读取重新授权并复算，不能通过重写报告 hash 发布虚构的构建成功。

Architect 检查父工件绑定、硬件／Android BOM、接口和暴露值。Android 检查 XML 解析、package／SDK、application 契约及构建证据。正常源码可得到 Architect READY、Android static PASS，但 Android readiness 必须 UNKNOWN，`build_device` 也为 UNKNOWN。已观察的 BOM／属性矛盾为 FAIL；缺失、过期、未测量或冲突的硬件证据为 UNKNOWN。

Android 12 对目标 API 31 及以上、带 intent-filter 的 activity／service／receiver 要求显式 `android:exported`，依据 [Android 官方行为变更文档](https://developer.android.com/about/versions/12/behavior-changes-12#exported)。SDK 数字语义依据 [uses-sdk 官方文档](https://developer.android.com/guide/topics/manifest/uses-sdk-element)。夹具中的 min 26、target 34、BOM A3 和 protocol 1 只是合成契约，不代表最新 SDK 建议、生产配置或硬件认证。

本批威胁与边界：

| 情况 | 实现与验证 |
| --- | --- |
| 跨租户或未授权父来源 | whole-run ACL 在检索之前；scoped FK；HTTP foreign scope 返回 404 |
| 重复提交／同 key 换来源 | 原结果回放；fixture／来源变化返回 409 |
| 工件、审计或报告被改变 | 父绑定与独立来源 hash；读取复算；篡改测试拒绝 |
| Skill Markdown 扩大权限 | 固定角色工具策略取交集；不授予 approve、dispatch、sandbox.execute |
| 静态检查被说成构建／认证 | Android build 明确 NOT_RUN；未知不能判 READY；来源标识 SYNTHETIC |
| 停错其他项目进程 | 仅固定 ForgeFlow 旧 API，核对 PID、命令、目录和监听端口后 SIGTERM |

本批 Domain SOP 是补充诊断，尚未纳入旧 EVT Gate 的强制规则。旧 Gate、人工批准和实际业务动作继续分别记录；Domain SOP 不批准、不推进阶段、不写 IssueTracker 或 Memory。普通 IN_PROCESS 核验不是 MCP；只有已有 sandbox.execute 链路具有真实 MCP 协议回执。Semgrep 和静态 XML 扫描不构成安全证明。

验收采用 20 项源码检查、6 项新增持久化／统一 API 测试、16 项原 SRE 回归，以及五条真实链的统一 HTTP 回放、15 份新静态报告和实际 PostgreSQL scoped FK 核对。回放没有新模型调用或新 sandbox 执行。报告保存历史实际执行与本次静态检查的区别。首轮脚本枚举断言错误的失败证据另存，修正后重新验收。

普通 BOM 的时效用服务器当前时间；因此到期后旧报告可能被拒绝，需要新来源／新 key 重新审查。绑定的合成派生场景采用其受控评估时钟。当前没有 Android SDK、aapt、Gradle 或 adb，构建接入需要用户决定新增工具和下载；本批不做下载。后续才讨论真实构建产物、Domain Gate 强制集成、真实角色模型、混合检索或外部办公／DevOps 适配，不能提前标为完成。
