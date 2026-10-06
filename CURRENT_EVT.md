# ForgeFlow 当前 EVT 原型入口

唯一开发原件：`/path/to/forgeflow`。当前 [API 8773](http://127.0.0.1:8773/docs)，启动 `.venv/bin/python scripts/evt_service.py`；需要装依赖的操作尚未获授权。

审阅时先读 [S3C 交付](deliveries/2026-10-06/s3-evt/DELIVERY.md)、[EVT 契约](docs/16-evt-bound-contract.md)、[实际回归](deliveries/2026-10-06/s3-evt/evidence/live-summary.json)、[安装决定清单](deliveries/2026-10-06/s3-evt/ANDROID_INSTALL_PLAN.md)，再审阅 `evt_contract.py`、`evt_review.py`、`evt_api.py` 和对应 tests／scripts。校验最新 manifest.json；追溯来源看 [S3B](deliveries/2026-10-06/s3-domain/DELIVERY.md)、[SRE](deliveries/2026-10-06/s3-sre/DELIVERY.md)、[S2C](deliveries/2026-10-06/s2c-fixes/REVIEW_ENTRY.md)。旧 CURRENT.md 是封存的 S3B 快照。

67 项相关测试通过；新 Gate 对默认缺失 APK／device 保持 UNKNOWN，显式 source-only N/A 只能接受源码评审，不能推进阶段。当前 HTTP 阻断旧 hardware-only 批准。真实本地模型角色仍仅 Supervisor／PM；其余职责与 Gate 确定性执行。Stage／Issue 执行和 Memory 未迁移到新契约；完整 NPI／生产／硬件认证未完成。安装决定只阻塞真实 Android 构建／设备，不能被拿来掩盖尚待实现的本地工程。

源码一份，文档与证据每日归档，无新 ZIP。`.runtime/identities.json` 与 dev.env 不用于公开审阅或 Git。
