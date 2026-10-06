# S3D：本地 Fabric 闭环实际交付

当前唯一原件在 `/path/to/forgeflow`，API `http://127.0.0.1:8773/docs`，当前进程 PID69967／`forgeflow.fabric_api`。启动器验证前进程 PID65879 的模块／目录／监听／health 后替换，未操作其他本地工程；再次启动返回 ALREADY_RUNNING。

本批将 Stage／Issue／Memory 的本地链路做成可执行结果。独立合成 scope 是 `synthetic-lab/engineering/aurora-fabric-e2e`；同一新请求的真实 Docker/MCP 检查、组合技术报告、独立审批、效果、回执、审计及受控 Memory 引用形成完整确定性链。没有新增 LLM 调用，未声称这是全职责 LLM 多 Agent。新 flow PASS 只允许模拟 TEST_STAGE，EVT 仍 UNKNOWN，APK／device NOT_RUN。

| 验证 | 实际结果／出处 |
| --- | --- |
| 新 contract／store／API 测试 | 44 项；合并原角色／EVT 回归共 **111 passed**；`evidence/related-tests.xml` |
| 新真实检查 | pass run `85b429cb-ca4b-4831-8172-116a1919b6f3` → flow PASS；unknown run `92ef0ce1-079e-4ced-aa1e-350cca05ce2d` → UNKNOWN；两者 SUCCEEDED、REMOVED；`checks.json`／两份 raw parent |
| MCP／辅助扫描 | 两次实际 stdio 独立 server、SDK1.30.0、protocol2025-11-25、sandbox.execute；Semgrep engine_executed=true；原始 `parent-*.json` |
| 本地执行 | TEST_STAGE、ISSUE、MEMORY_ACTIVATE 分别提案／独立 TEST_DRIVER reviewer 决定／service 执行；决定不改变效果；`effects.json` |
| 补偿 | MEMORY_REVOKE、ISSUE_RETRACT、STAGE_REVERT 分别批准并执行；保留6个不可变 receipt，项目回 PROTOTYPE，stage_revision2；Issue RETRACTED，Memory reference REVOKED |
| Memory 来源 | 激活时引用组合评审及两条实际执行事件；底层保持 CANDIDATE。撤销后 active search为空；`effects.json` |
| 中断恢复 | 自身事务子进程在 flush 后／commit 前 SIGKILL，exit -9；Stage／receipt 完全回滚；fresh Store 恢复一个提交 receipt；已提交重试返回原 receipt |
| HTTP／schema | **15**项实际验证，另进程的 API 重放6份 receipt 无新效果；401／404／403／UNKNOWN409；4表外键含完整scope；`http-schema.json` |
| 原项目保护 | 所有原 Aurora run／events digest、project、EVT approvals hash 前后相同；原两份 SOURCE_ACCEPTANCE 仍PENDING；`checks.json`与`effects.json` |

计数：本批 **2** 个新 sandbox check、**6** 个测试身份决定、**6** 个本地效果／补偿、**0** 个真实用户业务批准、**0** 个新模型调用、**0** 个远程写入／下载／设备／部署。当前原项目仍 PROTOTYPE revision0；不会消费旧 hardware-only proposal。旧 local CLI 适配器作为历史代码保留，不自称它已完成生产迁移；当前 HTTP 阻断旧硬件批准，新 Fabric scope 自动 core proposals 已禁用。

独立静态审阅发现两项 P2：Memory 自身有效期未过滤、服务 launch OSError 可能跳过旧模块恢复，均已修复；Memory expiry／future／revoked 已补测。服务失败恢复本批为静态校验，未声称实际故障注入测试。HTTP verifier 首次误传 execute body wrapper 收到422，保留 `http-attempt-01-failed.json`；更正 harness 后15项通过，无动作重做。

本批代码：`fabric_contract.py`、`fabric_store.py`、`fabric_runtime.py`、`fabric_api.py`、`fabric_cli.py`，固定 `fixtures/fabric/policy.json`，3个脚本及2个测试文件。没有新依赖或第三方代码拷贝；复用本项目此前封存模块，出处／许可证继续保留。模块、威胁、手动操作与本人练习见 `docs/17-fabric-local-contract.md`；全项目矩阵和具体 Android 工具／下载量／费用／资源影响见 `docs/18-capability-and-next-phase.md`。

真实剩余缺口：Android SDK／JDK／Gradle／AGP 下载与依赖网络、实际 APK builder 的隔离边界／原生架构核验、设备取证，以及业务定义的真正 EVT／后续 NPI SOP；远程 Issue／办公需要目标参数和写入授权。新本地 Stage／Issue／Memory 已完成，不能把这些工程说成被 SDK 授权阻塞。现有本地模型可继续用于后续增量，没有必要新购 GPU、E2B 或云机。

审阅顺序：本说明 → `docs/17` → `docs/18` → 真实 evidence → 新源文件 → 两个 tests。当前 seal 只包含新文件和每日文档档案；S2C／S3A／S3B／S3C 封存内容均继续校验。不做新 ZIP 或全新解压测试，不发布。
