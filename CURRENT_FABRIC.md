# ForgeFlow 当前唯一原件与审阅入口

原件：`/path/to/forgeflow`。当前 API [8773/docs](http://127.0.0.1:8773/docs)，模块 `forgeflow.fabric_api`；启动 `.venv/bin/python scripts/fabric_service.py`。

审阅时先读 [本批交付](deliveries/2026-10-06/s3-fabric/DELIVERY.md)、[模块与验收](docs/17-fabric-local-contract.md)、[全项目能力／下一阶段](docs/18-capability-and-next-phase.md)，再审 `fabric_contract.py`、`fabric_store.py`、`fabric_runtime.py`、`fabric_api.py` 和两个 tests。真实运行证据是 `s3-fabric/evidence/checks.json`、`effects.json`、`http-schema.json`；按新 manifest 校验。旧 CURRENT／CURRENT_EVT 是封存的阶段快照。

本批 111 项相关测试通过，2 次真实 MCP／Docker 检查，6 个 TEST_DRIVER 审批及本地动作／补偿回执，真实 PostgreSQL 提交前中断回滚、恢复和独立 API 进程重放。原 Aurora 和其 PENDING 候选保持不变。本地 TEST_STAGE 与本地 DB Issue／已批准 Memory 引用已跑通；EVT／APK／device 仍 UNKNOWN／NOT_RUN，不是生产多 Agent 或硬件认证。

无新下载、模型调用、外部写入或 审阅 ZIP。代码只有一个原件；本批文档与证据每日归档。`.runtime` 凭据／Bearer fixture tokens 不用于公开审阅。
