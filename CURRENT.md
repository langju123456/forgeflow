# ForgeFlow 当前入口

唯一开发原件：`/path/to/forgeflow`。

当前本地原型 API：[http://127.0.0.1:8773/docs](http://127.0.0.1:8773/docs)。使用现有 `.runtime/identities.json` 中的任务本地 DEV token；不要将该文件或 `.runtime/dev.env` 交给审阅者／放进 Git。

最新交付与手动演示：[S3B DELIVERY](deliveries/2026-10-06/s3-domain/DELIVERY.md)。职责契约、边界和验收：[docs/15-domain-prototype.md](docs/15-domain-prototype.md)。真实回归证据：[live-summary.json](deliveries/2026-10-06/s3-domain/evidence/live-summary.json)。可直接从这里审阅的路径，无需 ZIP 或全新解压测试。

当前 API 包含 runs／审批、S2C 本地模型证据选择、SRE 回执审查和 Architect／Android 静态诊断。旧 8769–8772 是历史版本，已验证归属后关闭；旧交付中的端口属于交付时快照。Beauty 8766、其他项目、数据库和已有 Ollama 不由此合并命令关闭。每次启动可运行 `.venv/bin/python scripts/prototype_service.py start`，健康检查已有服务时直接复用。

实现状态：真实本地 PostgreSQL、MCP／Docker／扫描回执、本地 Qwen Supervisor／PM 调用已留证。Hardware／SRE／Architect／Android 是确定性 SOP；Android 只做源码核验，构建和设备测试 NOT_RUN。规则与 Aurora 资料合成，Domain SOP 尚未强制加入旧 EVT Gate。没有生产部署、外部 Issue／群消息写入、完整硬件认证或完整 NPI 覆盖。

审阅顺序：

1. 最新 DELIVERY 和 `docs/15-domain-prototype.md`；核对 `evidence/live-summary.json` 与 `manifest.json`。
2. `src/forgeflow/domain_checks.py`、`domain_store.py`、`prototype_api.py`；对照新 Skills、XML 与 contract。
3. `tests/test_domain_checks.py`、`test_domain_store.py`；再看 `scripts/prototype_regression.py` 和 `prototype_service.py`。
4. 需要追溯时读 [S2C 审阅入口](deliveries/2026-10-06/s2c-fixes/REVIEW_ENTRY.md)、[SRE 交付](deliveries/2026-10-06/s3-sre/DELIVERY.md)。初始计划／模块契约／威胁模型／验收／复用分别在 `docs/01-design-review.md`、`02-contracts.md`、`03-threat-model.md`、`04-acceptance-matrix.md`、`05-reuse-and-sources.md`、`06-execution-plan.md`。

每日文档与证据归档在 deliveries；源码保留一份。旧封存保持原样，不把旧 ZIP 当成当前版本。
