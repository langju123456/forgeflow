# 2026-10-06 S3C：绑定 EVT 评审契约

当前可验证的 Hardware、Architect／Android 与 SRE 证据已接入持久 EVT 组合契约。默认要求 APK／device，缺失是 UNKNOWN 并拒绝申请批准；显式源码策略排除这两项时为 NOT_APPLICABLE，同时保留 observed UNKNOWN／build NOT_RUN。适用性未定也不放行。所有策略是合成示例，禁止阶段变更／业务执行，不称硬件认证。

唯一源码：`/path/to/forgeflow`。当前 API 保持 [http://127.0.0.1:8773/docs](http://127.0.0.1:8773/docs)，用新入口启动：

```sh
cd /path/to/forgeflow
.venv/bin/python scripts/evt_service.py
```

启动已有服务直接复用。代码修改后用 `--restart`；仅在当前本任务 PID／模块／目录／监听／health 匹配时替换进程。旧版本的 `prototype_service.py` 与 CURRENT.md 属于 S3B 快照，当前审阅入口为根目录 CURRENT_EVT.md。没有新增永久端口或新 ZIP。

新增 `evt_contract.py`、固定 policies、`evt_review.py`、`evt_api.py`／CLI、两份测试与服务／回归脚本。`EVTReview` 有 scoped run／role 外键，持久保存 role 引用、报告 hash、技术来源、策略 hash 和审计。独立 `EVTApproval` 绑定报告／策略／purpose／父与阶段版本，支持真实 reviewer、proposal hash、revision CAS、期限与重复决定回放。它仅接受评审，不授予 Stage／Issue／Memory 执行。当前旧 HTTP 硬件 proposal 的 APPROVED 返回 409 EVT_BOUND_APPROVAL_REQUIRED；明确 REJECTED 仍可使用。

**验收：67 项相关测试通过**，新增 EVT 契约／持久化 25 项，职责回归 42 项；Ruff 通过。初次独立审阅发现 reviewer-only 会被内部 admin/supervisor 限制误挡，已修复真实 reviewer 的只读来源校验，并补测 reviewer 决定成功、创建／申请拒绝、缺来源 ACL 拒绝。其余来源、策略、参数、scope、CAS、到期、篡改和幂等检查通过。完整证据与审阅范围在 evidence。

实际 PostgreSQL／HTTP 回归覆盖五个已留证 run、19 份新绑定 EVT 评审和 2 份源码接受待定候选。默认策略对 M01／M04 即使原硬件 Gate PASS 也仍 UNKNOWN；M01-blocked 保持 BLOCKED；M02／M03 的 UNKNOWN 保留。源码策略只有 M01／M04 在其有限范围 PASS，两个候选仍 PENDING，没有自动作出人类决定。BOM／exported 冲突组合为 BLOCKED。父 run／事件、阶段 PROTOTYPE、原审批及零业务回执保持一致；20 次旧模型调用只读回放，本批新增模型／sandbox 执行均为 0。

手动演示可以读取 evidence/live-summary.json 中任一 review ID：

```sh
.venv/bin/python -m forgeflow.evt_cli show REVIEW_ID
.venv/bin/python scripts/evt_regression.py
.venv/bin/pytest -q tests/test_evt_contract.py tests/test_evt_review.py
```

创建命令先使用同一 run 已有的 Domain 和 SRE review ID：

```sh
.venv/bin/python -m forgeflow.evt_cli create RUN_ID --domain-review DOMAIN_ID --sre-review SRE_ID --key manual:evt:01
```

预期默认 UNKNOWN。以新 key 和 `--policy synthetic-source-review-v1` 比较明确 N/A；不要将源码范围 PASS 当成 EVT 阶段批准。CLI 不提供自动人类决定。API 中每个接受请求都要绑定准确 review_hash；决定要 proposal_hash 与 expected_revision。用户“批准 prototype”仍只用于开发授权。

本人练习：先解释六项 rows 的 applicability／observed_status／status，指出为什么 APK NOT_RUN 不能成为 PASS；再在复制到 `/tmp` 的策略夹具中改 limitations，观察旧评审因 policy_hash 变化拒绝回放。不要直接修改已封存策略或冒充新增成功构建。

已完成范围：现有实际证据的 Gate 组合、来源／策略／参数绑定、独立人工接受生命周期、当前 HTTP 旧批准阻断、持久化及失败回归。范围限制：Stage／Issue 动作适配器和 Memory 尚未迁移到新 EVT 契约，属于后续本地工程；不是受工具授权阻塞的已完成项。旧开发 CLI 仍是历史接口，不能把它称为新契约的执行器。

真正的新授权缺口是 APK 构建所需 JDK／SDK／Gradle／AGP 下载，以及设备验证所需 adb／自有设备操作。固定工具、官方来源、准确已知体积、费用、预算与 Mac 影响见 [ANDROID_INSTALL_PLAN.md](ANDROID_INSTALL_PLAN.md)。AGP 依赖总量和 SDK 原生架构还有明确待核项；未下载／安装任何东西。不新增 provider、凭据、付费云机，不部署或外部写 Issue／群消息。

本批文件与 evidence 封存在 manifest.json；旧 S3B/SRE/S2C 封存保留原样。可读契约最后归档在本目录 docs；代码不复制。
