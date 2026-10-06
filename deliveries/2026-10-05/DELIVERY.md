# ForgeFlow 2026-10-05 首条实现交付

**S1：首条完整本地确定性链已实现并实跑；136 tests 通过，0 failures/errors/skipped。完整项目尚未完成。** 首链包含原计划 S2 的核心 runtime/context 能力，后续模型、多角色、向量、外部集成与硬化仍分别验收。

唯一代码原件为 `/path/to/forgeflow`。未修改其他本地项目；没有 Git commit/push、外部 Issue/群消息、上线、付费 API、云机、E2B 或 GPU。原设计交付保存为 [DESIGN-DELIVERY.md](DESIGN-DELIVERY.md)，原 review/manifest 与设计文档快照保留，不改写历史状态。

## 已实现与实际边界

| 模块 | 已实现 | 当前限制 |
|---|---|---|
| Control plane | FastAPI/Pydantic，持久 runs/steps/events/artifacts/evidence/approvals/actions/receipts；全 scope FK、DB 时间 lease/epoch、取消、恢复清理屏障、幂等提交 | DEV_ONLY loopback 身份，worker CLI；没有生产 SSO/RLS、常驻 worker 运维、正式完整迁移 |
| Supervisor/Skills | 结构化确定性计划，hardware role principal；动态 Skill/hash、工具权限交集、路径/资源限制 | 没有 LLM、多 Agent；PM Skill 存在不等于 PM role 已完成实际 SOP |
| Knowledge/Memory | ACL 在读取内容/排名前；版本/hash/时效/精确引用、显式冲突；独立、有来源、持久、幂等的 Memory candidate | 词法检索；没有 embedding/pgvector/RRF；semantic contradiction、完整 promotion/supersession API 待做 |
| Scanner/MCP | 隔离 Semgrep CE 1.179.0、固定规则和同源码 hash；SDK 1.30.0 独立 stdio，实际协议 2025-11-25、initialize/initialized/list/call | 无发现不是安全证明；仅 sandbox.execute 是 MCP；其他操作仍是普通本地服务 |
| SandboxProvider | Docker 非 root10001、只读 rootfs、无网络、cap drop/nnp/seccomp、CPU/RAM/PID/time/output 限额、临时 workspace、精确句柄清理和旧 attempt 撤销 | 固定可信模板；单个受限 result 文件 bind/freeze 验证；RLIMIT_FSIZE 不抵御任意代码 preallocation，generated code 禁止 |
| Stage Gate/审批/动作 | 合成规则 PASS/BLOCKED/UNKNOWN；批准绑定参数与材料 hash/expiry/revision；local Issue/receipt、approval 消费、项目 stage CAS 与事件同事务 | 实际边为 PROTOTYPE→EVT；本地 adapter，不算 GitHub/办公；测试 reviewer 不算用户人工批准 |
| 审计/失败证据 | scope 因果事件 hash chain，raw artifact bytes 校验，优化 Python 模式仍有效；失败与恢复证据保留 | 应用 append-only，不是防 DB 管理员重写的 WORM 或外部签名审计 |

新 PostgreSQL 16 容器 `forgeflow-task6-db` 与专用 volume 保留供演示，端口为 `127.0.0.1:55436`。应用账号已实测非 superuser/NOCREATEDB/NOCREATEROLE。独立 Python 3.12.13 `.venv`；core/dev 实际安装版本在 `requirements.lock`。首次任务开发密码曾被测试 traceback 显示，现已轮换并验证新连接；没有读取私人密钥。配置/token 仅在忽略的 `.runtime/`、0600，不进入归档。

## 实跑验收与证据

| 验证 | 结果与原始证据 |
|---|---|
| 最终 opt-in 联合套件 | **136 passed**，30.97s，0 skipped；[pytest.xml](implementation-evidence/pytest.xml) |
| 默认套件 | **123 passed、13 skipped**，3.45s；live skips 不算通过；[pytest-unit.xml](implementation-evidence/pytest-unit.xml) |
| Ruff | PASS；命令/版本/退出码见 [verification-summary.json](implementation-evidence/verification-summary.json) |
| 正常与技术拒绝 | [PASS](implementation-evidence/chain-pass.json)、[BOM BLOCKED](implementation-evidence/chain-blocked.json)、[缺证据 UNKNOWN](implementation-evidence/chain-unknown.json)、[TIMED_OUT→UNKNOWN](implementation-evidence/chain-timeout.json)；真实 PG/scan/MCP/Docker，local approvals/receipts/Memory |
| 取消与资源边界 | 套件覆盖 cancel-before-dispatch、数据库取消中断 MCP；[runtime conformance](implementation-evidence/runtime-security-canonical.json) 另有输出限额、网络/FS/socket/UID、取消与 SIGKILL 探针 |
| 持久中断恢复 | [real PG worker SIGKILL + paused orphan cleanup + retry](implementation-evidence/durable-paused-orphan-recovery.json)：先清旧资源，撤销旧 attempt，再重跑；迟到 worker/同名不同 scope/重复 action 均有断言 |
| 独立复核 | [09 implementation review](../../docs/09-implementation-review.md)：限定 S1 范围无未解决的已确认 P1；不是生产安全保证 |
| 失败保留 | [首轮 scanner 导入整合失败](implementation-evidence/pytest-first-integration-import-failure.xml)、[真实恢复大小写 absence 首败与修复](implementation-evidence/recovery-first-failure-and-fix.json)；后续验证通过，不删除失败历史 |

测试中审批身份明确为 SCRIPTED_TEST_HARNESS，用于验证 reviewer API/状态机。不是本次用户已批准实际 Aurora run。四条链的资料/规则和所有 Issue 数据均为 SYNTHETIC/LOCAL；没有真实硬件实验、认证或外部业务行动。

早期 `EVIDENCE.json`、`TEMPLATE_SCAN.json`、`runtime-security.json` 是模块/阶段证据，包含原 staging source hash。最终代码经过集成/格式整理，以当前 manifest 的 canonical hash 为准；实际 scanner image ID 被锁定，不声称已经取得全转递依赖、所有源码字节和 image build 的可重建供应链证明。最新安全记录为 `runtime-security-canonical.json`。

有一个 Starlette httpx TestClient deprecation warning；当前测试仍通过，后续适配维护待做。运行资源核对：本任务 checker/scanner 容器各 0 遗留，数据库保留；未做 broad prune。私有生命周期 lock/revoked tombstone 为防旧 attempt 迟到启动而有意保留。

## 现在可以手动看与修改

保留真实检查后的 run **08d6ca27-13d8-4a62-95ca-c3c03184d5f4**，技术 PASS、两项审批仍 PENDING、没有 receipt，项目仍 PROTOTYPE。[未批准的实际快照](implementation-evidence/manual-review-ready.json) 可直接查看。

```sh
.venv/bin/python -m forgeflow.local show 08d6ca27-13d8-4a62-95ca-c3c03184d5f4
.venv/bin/python scripts/verify.py --live --output .runtime/live-evidence
```

从唯一工程目录执行，Docker/本项目 PostgreSQL 必须运行。每项审批需要当前 proposal_hash 和 run revision；用户手动决定后再 dispatch。详见 [演示与本人修改练习](../../docs/07-manual-demo-and-learning.md)：修改合成阈值、删 required evidence、重放 key、观察跨项目拒绝与取消。能复现/修改某条机制需本人实际练习证明，不反推已经掌握 backend/ML。

## 后续阶段与具体决定缺口

1. **模型/embedding 接入。** 需要 exact provider/model/revision、endpoint、单独授权的控制面 secret 注入方式、费用上限、允许发送的数据级别、token/tool/time budget。不要把私人 key 贴入聊天，也不要默认使用其他项目 `.env`。可选已确认可用的本地 endpoint 或授权 API；没有该决定前继续本地准备与确定性验证，不伪造模型 trace。
2. **真实多角色与检索。** Supervisor+PM/SRE 先有实质分派与独立上下文；真实 embedding+pgvector/keyword/RRF，冻结 held-out evaluation，完善 Memory promotion。五角色与所有 NPI（含 RELEASE）保留后续独立验收，不生成空壳。
3. **Generated code。** 先解决任意代码磁盘/进程/产物隔离与恶意测试，再开放模型 source→hash/scan/policy/必要批准/MCP/sandbox/独立 checker。当前只允许可信模板。
4. **外部 DevOps/办公。** 需要准确 owner/repo 或 channel/table/project、允许动作、可审查 proposal 内容和授权身份。实际写入、凭据、网络安全设置、部署、付费云资源仍缺具体批准；local adapter 不能替代外部回读和结果未知对账测试。
5. **生产硬化。** SSO/RLS、服务 capability 生命周期、正式迁移、完整供应链锁、retention/delete、持续 worker/metrics/load/recovery 与批准的 Linux gVisor/microVM/egress 方案分别验收。

当前没有 S1 本地实现阻塞；父 Review 可直接评估这份可运行阶段后决定下一阶段顺序。完整验收和原最终目标见 [04](../../docs/04-acceptance-matrix.md) 与 [06](../../docs/06-execution-plan.md)。可读文档按每日归档，代码只有 canonical 一份；不为赶期限虚写完成。
