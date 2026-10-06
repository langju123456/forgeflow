# ForgeFlow — Enterprise Agent Runtime & Workflow Fabric

当前交付：**S1 首条完整本地执行链可运行，136 项实跑验收通过**。这是确定性企业工作流底座，身份为 DEV_ONLY，研发资料和 Stage Gate 规则为合成示例。完整项目仍在分阶段建设；没有 LLM 多 Agent、向量检索、真实硬件认证或生产部署。

唯一代码原件：`/path/to/forgeflow`。目录没有 Git root/branch/HEAD/remote。没有修改其他本地工程，没有外部 Issue/群消息、云机或付费模型调用。新 PostgreSQL 16 使用任务专用容器、volume 和非 superuser 应用账号，监听 `127.0.0.1:55436`。

实际链路：Aurora EVT 请求 → 持久 run 与有 fencing 的 lease → 确定性 Supervisor 选择 hardware Skill → ACL 在候选选择前的 Knowledge 与独立 Memory → 同源码 Semgrep 扫描 → 官方 MCP SDK 独立 stdio 握手与 `sandbox.execute` → 非 root、只读 rootfs、默认无网络的 Docker 检查 → 带 hash/来源/时效的 PASS、BLOCKED 或 UNKNOWN → 两种独立审批 → 本地 Issue receipt／项目阶段 CAS → 审计与有来源的 Memory candidate。

MCP 实装为官方 Python SDK **1.30.0**，实际协商协议 **2025-11-25**；Semgrep CE **1.179.0**。Knowledge、Memory、Issue 目前是普通本地服务，只有 `sandbox.execute` 是真实 MCP 工具。扫描无发现不能作为安全证明。只接受可信固定模板，任意生成代码尚未开放。

在本目录运行：

```sh
.venv/bin/python -m forgeflow.local submit --fixture pass --key my-aurora-v1
.venv/bin/python -m forgeflow.local check <run_id>
.venv/bin/python -m forgeflow.local show <run_id>
.venv/bin/python scripts/verify.py --live --output .runtime/live-evidence
```

检查后停在 WAITING_APPROVAL；技术 PASS 不会自动批准或推进阶段。逐项审批与 dispatch、FastAPI 8769、重复提交及本人修改练习见 [手动演示](../../../docs/07-manual-demo-and-learning.md)。任务生成的连接配置和本地身份 token 仅保留于忽略的 `.runtime/`，不要提交或复制到报告。

| 阅读顺序 | 文档 |
|---|---|
| 1 | [设计评估与范围](../../../docs/01-design-review.md) |
| 2 | [模块契约与持久状态](../../../docs/02-contracts.md) |
| 3 | [威胁模型](../../../docs/03-threat-model.md) |
| 4 | [验收矩阵与需求映射](../../../docs/04-acceptance-matrix.md) |
| 5 | [只读复用盘点与官方核验](../../../docs/05-reuse-and-sources.md) |
| 6 | [阶段执行包、并行路线与本人练习](../../../docs/06-execution-plan.md) |
| 交付 | [当日交付与实际证据](../DELIVERY.md) |
| 手动 | [可复现命令与本人练习](../../../docs/07-manual-demo-and-learning.md) |
| Runtime | [真实 MCP、限额与 artifact 修正](../../../docs/08-runtime-conformance.md) |
| 复核 | [独立实现复核](../../../docs/09-implementation-review.md) |
| 许可证 | [第三方说明](../../../docs/THIRD_PARTY.md) |
| 来源 | [材料清单与授权边界](../../../sources/MANIFEST.md) |

确定性编排的运行证据、真实 LLM 多角色执行证据、外部集成回执分别记录。合成研发资料可用于真实代码运行测试；它不能证明真实硬件认证、客户生产部署或本人已经掌握。

后续保留五角色和立项、Prototype、EVT、DVT、PVT、RELEASE 全流程。下一阶段接入真实模型与 embedding，需要决定 provider、准确 model/revision、endpoint、凭据注入方式、费用与数据范围。没有自动读取私人密钥。多角色、pgvector 混合检索、全 NPI、GitHub／办公适配和更强隔离分别验收，不靠创建空壳宣布完成。
