# 2026-10-10 代表性链路：实跑记录、手动演示与已知局限

## 路径与版本

- 原件：`/path/to/forgeflow`（唯一源码）。
- 实跑在 PostgreSQL 库**副本** `forgeflow_chain_rehearsal` 上（Docker 容器 `forgeflow-task6-db`，由真实库 `forgeflow` 用 `pg_dump` 复制）。真实库的行数在运行前后完全一致。
- 代码版本：`start` 在 `d7d21d3`（分支 `feat/approval-explain`）上运行；续期、决定和执行在 `902a286`（分支 `fix/renew-again`）上运行。这两个分支在写这份文档时**还没有合并到 `master`**。
- 模型：本机 `qwen3:8b`，摘要 `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`，temperature 0，没有 seed。

## 实际发生的（证据在 `representative-chain/`）

| 环节 | 结果 |
|---|---|
| 提交 | `aurora-pass-v2`，同 key 重复提交返回同一个 run（`bf8c3e81…`） |
| Skill 与权限检索 | 在检查里执行（先按 scope／角色过滤，再取内容），快照里记录了所引用的文档版本与哈希 |
| MCP 与 Docker 检查 | 真实 MCP stdio 握手（协议 `2025-11-25`），沙箱 `SUCCEEDED`，容器 `REMOVED`，Semgrep 引擎实际运行 |
| 模型分析 | Supervisor 与 PM，4 次调用，`COMPLETE`，约 82 秒，放在**同一个父 run** 上；父 run 和技术哈希不变 |
| Gate | `flow_gate=PASS`，`evt_gate=UNKNOWN`（没有 APK／设备证据，所以不推进真正的 EVT） |
| 人工审批 | 6 个动作（`TEST_STAGE`、`ISSUE`、`MEMORY_ACTIVATE`，以及三个补偿），每个都在 `explain` 看过对象之后，由用户在对话中逐一批准 |
| 执行 | 6 份回执，每份第二次执行都是重放（同一回执） |
| 中断恢复 | `TEST_STAGE` 的执行进程在写完效果、提交之前被 `SIGKILL`：没有回执，恢复后恰有一份，阶段只加了一次 |
| UNKNOWN | 两个动作都被 `FABRIC_GATE_NOT_PASS` 拒绝，没有创建审批 |
| 超时 | `TIMED_OUT`，容器清理成功，Gate 为 `BLOCKED`，动作被拒；没有遗留容器 |
| 终态 | 阶段 `PROTOTYPE`（修订号 4）、Issue `RETRACTED`、Memory 引用 `REVOKED`、底层 Memory 仍是 `CANDIDATE`；审计链有效；原 Aurora 项目摘要不变 |

## 失败与意外（都留在 `log.json`）

1. 审批 1 小时有效期，用户回复晚了，提案过期，`explain` 指出要用 `propose` 续期；续期保留旧版本和原因（`EXPIRED`、`ADAPTER_CHANGED`）。
2. 用户的第一次“批准”到达时提案已过期近 4 小时，两条 `decide` 被拒（`APPROVAL_EXPIRED`），没有记录任何决定；之后重新续期并请用户再确认一次。
3. 续期时发现同一个 key 的第二次续期什么都不做（F1 实现缺陷），已用测试复现并修复（`fix/renew-again`）。
4. 我两次删掉了失败尝试的原始输出文件，随后在日志里按事实补记；之后不再删除。
5. 一次 shell 参数错误（zsh 不按空格拆分变量）导致提案命令在参数解析阶段退出，没有写入任何东西。

## 已实现／合成模拟／待接入

- **已实现并实跑**：持久 run、租约与恢复、固定模板的 Docker 工具执行、受控模型分析（只读证据）、Gate、审批与执行分离、事务回执、补偿、审计链、版本化审批、漂移检查。
- **合成模拟**：Aurora 夹具与阈值、TEST_STAGE（本地阶段，不是 EVT 认证）、本地 Issue 与 Memory（不写外部系统）。
- **未接入**：APK 构建与设备测试、外部 Issue／聊天写入、真实 SSO、真实业务批准。

## 诚实的局限

- 模型分析只跑了 **1 个场景 × 4 次调用**，没有 seed，不能据此说模型质量或稳定性。
- 审批人身份来自测试身份文件，所以决定记录的性质仍是 `TEST_DRIVER`；区别是这次每个决定都由用户在看过对象后指示执行，不是脚本自动批的。
- 在库副本上运行，不是在 8773 的服务上；8773 上的服务运行的是更早的代码。
- 副本库 `forgeflow_chain_rehearsal` 仍在容器里，是否删除由用户决定。

## 手动演示（短命令）

在项目根目录，下面每一条都要带副本库的连接串（驱动只允许库名以 `forgeflow_chain_` 开头）。

```sh
# 建副本（只读真实库）
docker exec forgeflow-task6-db sh -c 'createdb -U postgres forgeflow_chain_rehearsal && pg_dump -U postgres forgeflow | psql -q -v ON_ERROR_STOP=1 -U postgres -d forgeflow_chain_rehearsal'

# 1 成功：提交、检查、模型分析、评审、提案，然后停下
.venv/bin/python scripts/representative_chain.py start --key demo-1 --analysis --out OUT
# 2 看：只读，显示对象、参数版本、实际效果
.venv/bin/python -m forgeflow.fabric_cli explain APPROVAL_ID
# 3 决定（你亲自决定后才运行；revision 取 explain 里的“修订号”）
.venv/bin/python -m forgeflow.fabric_cli decide APPROVAL_ID PROPOSAL_HASH REVISION --decision APPROVED   # 或 REJECTED
# 4 执行，再执行一次应看到 replayed=true
.venv/bin/python -m forgeflow.fabric_cli execute APPROVAL_ID
# 5 重试恢复：提交前被杀，再恢复
.venv/bin/python scripts/representative_chain.py crash-execute APPROVAL_ID --out OUT
# 6 拒绝：UNKNOWN 与超时
.venv/bin/python scripts/representative_chain.py start --key demo-u --fixture unknown --out OUT
.venv/bin/python scripts/representative_chain.py start --key demo-t --fixture timeout --out OUT
# 7 核验
.venv/bin/python scripts/representative_chain.py verify --state OUT/state-demo-1.json --out OUT
# 8 漂移检查（只读）
.venv/bin/python scripts/drift_check.py --public ../forgeflow-public
```

提案 1 小时过期；过期后用同一个 key 重新 `propose`（`fabric_cli propose REVIEW_ID REPORT_HASH --key … --kind …`）续期。

## 每阶段的讲解和一个小修改练习

1. **检查与分析**：工具先于模型，模型只读证据、不决定状态。练习：把 `analysis_provider.py` 里的 `temperature` 改成 0.7，思考哪些保证会失效（可复现性、冻结评测），再改回去。
2. **审批**：提案哈希绑定参数、评审哈希、技术哈希和适配器；`explain` 只读。练习：在 `fabric_store.py` 的 `_proposal` 里去掉 `review_hash`，看哪个测试第一个变红，说明它在保护什么。
3. **执行与恢复**：动作、事件、回执在同一事务。练习：在 `crash-execute` 里把 `_before_commit` 改成提交之后杀进程，预测回执和阶段会怎样，再运行验证。
