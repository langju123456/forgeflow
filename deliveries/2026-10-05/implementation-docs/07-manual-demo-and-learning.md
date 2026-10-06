# 首条本地链路：复现与练习

本文件描述开发中的 S1/S2 本地链路。最后验收结果以当日 DELIVERY 和 evidence 为准。
场景和阈值都是合成研发示例；没有模型调用，没有真实硬件认证，没有外部 Issue 写入。

## 环境与手动复现

在 ForgeFlow 原件目录执行。Python 3.12 的 `.venv` 独立于现有工程；依赖版本见 `requirements.lock`。
PostgreSQL 容器是 `forgeflow-task6-db`，仅监听 `127.0.0.1:55436`，数据在新建的项目专用 volume。
应用账号不是 superuser。任务生成的开发连接配置与 token 只存在 `.runtime/`，文件权限 0600。
数据库应用层 ACL + 全 scope FK 已实现；生产 SSO、数据库 RLS、多人部署还未验收。

```sh
.venv/bin/python -m forgeflow.local init
.venv/bin/python -m forgeflow.local submit --fixture pass --key aurora-manual-v1
.venv/bin/python -m forgeflow.local check <run_id>
.venv/bin/python -m forgeflow.local show <run_id>
```

`check` 后应该显示 technical assessment 和 WAITING_APPROVAL，项目仍是 PROTOTYPE。
读取每个 approval 的 `id`、`proposal_hash` 与当前 run `revision`，逐一决定，再 dispatch 对应 action。
每做一次决定都先重新 `show`，因为 revision 会更新。Issue 与阶段转移使用独立审批。

```sh
.venv/bin/python -m forgeflow.local decide <approval_id> <proposal_hash> <current_revision> --decision APPROVED
.venv/bin/python -m forgeflow.local dispatch <run_id> <action_id>
.venv/bin/python -m forgeflow.local show <run_id>
```

这些命令只使用明确的本地 DEV reviewer；用户手动决定才是本次手动示范的人工决定。
自动测试中的 reviewer 是 SCRIPTED_TEST_HARNESS，用来验证权限与状态机，不作为真实人类审批记录对外展示。
重复同 key/payload 返回同 run；不能对已经完成的 run 再执行检查。另一个项目或新的输入版本应使用新的请求。

FastAPI 可独立启动在 8769，拒绝公网绑定和 production mode：

```sh
.venv/bin/python -m forgeflow.api --host 127.0.0.1 --port 8769
```

Swagger `/docs` 可以提交固定 fixture、查询 run/events/report/artifact、取消和审批。
Authorization 使用 `.runtime/identities.json` 中本地任务生成的 token；身份 scope/role 从该可信映射确定，
不是 `X-Tenant-ID`、`X-Role` 或请求正文。worker 检查/dispatch 在上面的 CLI 独立执行。

```sh
.venv/bin/python scripts/verify.py
.venv/bin/python scripts/verify.py --live --output .runtime/live-evidence
```

第二条命令需要本地 Docker 和本项目 PostgreSQL；会创建独立合成测试项目，不写 GitHub/办公系统。
默认单元测试跳过实际 Docker/PostgreSQL 全链测试；不要把 skip 计为通过。

## 可以讲清楚的工程点

1. 幂等处理重复请求；lease/epoch 处理两个 worker 的先后关系。这是两种不同的问题。
2. Gate 判断证据是否满足合成规则；审批判断谁同意执行某个固定版本的动作；receipt 证明动作已经发生。
3. Knowledge 是版本化来源；Memory 是带来源的 candidate/confirmed 记录。候选结论不会自动升级为事实。
4. MCP 是独立进程间真实 JSON-RPC 协议。普通 Python 函数调用和 Skill Markdown 都不是 MCP。
5. 扫描 PASS 只表示有限规则未发现匹配；Docker 配置、资源限制、权限和清理仍需独立检查。
6. 外部请求丢结果不能当作失败随意重试。这个阶段只验证本地数据库事务中的 Issue adapter；外部 reconcile 尚未接入。

## 本人修改练习

- 在 `gate.py` 把合成 thermal 阈值改成 65，观察 68 的 fixture 从 PASS 变 BLOCKED；更新规则版本和 hash，测试旧 approval 被拒绝。
- 把 `ci_smoke` 从 fixture 删除，复跑新 run，解释为什么 UNKNOWN 而不是 PASS。
- 用另一个 project 的身份读取原 run/event/artifact，观察统一 404；不能靠修改 header 获得权限。
- 对同一个请求重复提交，再改 fixture 使用相同 key，比较 replay 与 409。
- 人工取消一个等待审批的 run，观察批准或 dispatch 被拒绝且无新 receipt。

完成这些练习说明已能复现和修改相应机制，不承诺已经掌握所有 backend/ML/Agent 技术。
