# 2026-10-06 S3B：源码职责核验与统一原型 API

本批已实现 Architect／Android 静态职责、PostgreSQL 持久化与幂等回放，并合并当前本地 API。正常、BOM 冲突、缺少 exported 三种输入均有真实解析和结果。补充诊断绑定既有实际硬件／MCP／sandbox 回执；不把静态检查计作新模型 Agent 或 APK 构建。

唯一原件：`/path/to/forgeflow`。当前运行入口：[http://127.0.0.1:8773/docs](http://127.0.0.1:8773/docs)。启动：

```sh
cd /path/to/forgeflow
.venv/bin/python scripts/prototype_service.py start
```

手动演示可以反复执行，无模型调用：

```sh
.venv/bin/python -m forgeflow.domain_cli review 627d36c6-ed60-444f-8c26-541a39fb452e --key manual:aligned:01 --fixture aligned
.venv/bin/python -m forgeflow.domain_cli review 627d36c6-ed60-444f-8c26-541a39fb452e --key manual:bom:01 --fixture bom-mismatch
.venv/bin/python -m forgeflow.domain_cli review 627d36c6-ed60-444f-8c26-541a39fb452e --key manual:exported:01 --fixture missing-exported
```

正常结果：Architect READY、Android static PASS／readiness UNKNOWN、handoff NEEDS_ANDROID_BUILD_EVIDENCE。后两项：DOMAIN_CONFLICT。同 key 重复执行得到同一 review ID；换 fixture 要新 key。`domain_cli show REVIEW_ID` 读取持久报告并重新授权／复算，不调用模型。已有 SRE CLI 与 S2C CLI 仍可用。

相关测试：

```sh
.venv/bin/pytest -q tests/test_domain_checks.py tests/test_domain_store.py tests/test_sre_ops.py
.venv/bin/python scripts/prototype_regression.py
```

42 项相关测试通过，其中新增源码检查 20 项、持久化／API 6 项、SRE 回归 16 项。Ruff 通过。独立静态审阅未发现可操作的 P1／P2；范围和限制见 `evidence/independent-review.json`。HTTP 回归复用五条真实执行链和其中 20 个已有本地 Qwen 调用，新增 15 份静态报告，模型和父 run／事件回放一致。跨租户 404、无效身份 401、同 key 换 fixture 409；新表为 scoped run 外键。原技术 Gate 保留 PASS／BLOCKED／UNKNOWN，各 run 仍在 PROTOTYPE，无本批业务动作。

首轮回归脚本把回执枚举误写成 CONFIRMED，实际为 DELIVERED；失败保存在 `evidence/attempt-01-failed.json`。修正断言后完成回归，不删除失败记录。最新通过证据在 `evidence/live-summary.json`，15 个完整报告在同目录。本批 0 新模型调用、0 新 sandbox 执行，不做新 ZIP／解压测试。

端口／进程整合证据在 `evidence/process-consolidation.json`：新服务验证后，仅向 PID、argv、cwd、listener 均匹配的 ForgeFlow 历史 API 发送 SIGTERM。旧端口记录保留为历史快照；其他项目和共享本地模型不关闭。

本人讲解练习：先沿 `parent_artifact_binding → hardware_android_bom → application_contract → build_device` 四个指针解释为什么正常源码仍 UNKNOWN。再把夹具复制到 `/tmp`，将复制文件的 targetSdkVersion 34 改为 35，用 `DomainArtifacts(复制目录)` 和测试中的 `evaluate` 方法观察 PACKAGE_SDK_CONFLICT；不要直接改已留证的原夹具。能手动复现与改规则是学习进度，不代表已经掌握全部后端或 ML。

当前缺口：Android SDK／Gradle／adb 不存在，APK 编译、安装、设备运行 NOT_RUN；需要新增工具／下载决定才能进入下一批。Domain SOP 尚未纳入旧 EVT Gate 强制规则；只有 Supervisor／PM 接了本地模型。完整 NPI、pgvector 混合检索、生产身份／隔离、真实 DevOps／办公写入待实现与单独授权。没有部署、外部 Issue／群消息写入或新 provider。用户“批准 prototype”用于继续本地开发，本批未替任何业务 proposal 作批准。

源码、新文档、测试和证据通过本目录 `manifest.json` 归档，并引用既有封存清单。审阅者按根目录 [CURRENT.md](../../../CURRENT.md) 的路径直接审阅即可。
