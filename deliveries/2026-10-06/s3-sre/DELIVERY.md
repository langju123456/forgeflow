# ForgeFlow S3A：SRE运行就绪SOP · 2026-10-06

本批新增一个有实际诊断作用的SRE SOP，使用已有五条真实沙箱运行的回执，在真实PostgreSQL保存报告及scoped审计；没有新增模型调用或沙箱执行。当前是确定性SOP服务，不是新LLM Agent，也不是实时基础设施探针。用户已批准继续prototype，并取消新ZIP解压测试；本批遵循该范围。

## 已跑通

- 16项必要测试通过，Ruff通过；独立只读review无未关闭P1/P2，不重复全套或ZIP测试。
- 五个实际parent均完成SRE报告、重建服务读取、同key回放及HTTP读取，记录完全相同。
- 真实API：`http://127.0.0.1:8772/docs`，PID 60055；主证据选择API仍在8771。无身份401、跨租户404。
- 新表`sre_reviews`有scoped run外键；父run、原审计及业务状态保持原样；新增模型调用0、沙箱执行0、外部业务写入0。

| 实际场景 | SRE运行就绪 | 既有技术Gate | Supervisor交接 |
|---|---|---|---|
| M01正常 | READY | PASS | READY_FOR_HUMAN_REVIEW |
| M01-blocked | READY | BLOCKED | NEEDS_TECHNICAL_EVIDENCE |
| M02缺CI | READY | UNKNOWN | NEEDS_TECHNICAL_EVIDENCE |
| M03过期／冲突 | READY | UNKNOWN | NEEDS_TECHNICAL_EVIDENCE |
| M04注入资料 | READY | PASS | READY_FOR_HUMAN_REVIEW |

SRE的实际贡献是把执行故障和技术证据不足分开：清理失败、未闭合lease、错误epoch、损坏工件、资源／扫描回执不一致会进入运行排查；缺失协议证明为UNKNOWN。环境检查正常不能让M03的过期CI通过，也不能替人批准。五个项目仍为PROTOTYPE、批准PENDING、业务receipt为0。

## 模块与复现

唯一原件：`/path/to/forgeflow`。新增 `src/forgeflow/sre_ops.py`、`sre_api.py`、`sre_cli.py`，动态加载`skills/sre-operations/SKILL.md`。两个SRE读能力是IN_PROCESS普通服务；被复核的sandbox回执来自之前实际执行的MCP stdio，不能把这次读回执说成再次调用MCP。

```sh
cd /path/to/forgeflow
PYTHONPATH=src .venv/bin/python -m forgeflow.sre_cli show f62d4aa3-2439-433f-a124-310ac8ea6fc9
PYTHONPATH=src .venv/bin/python -m forgeflow.sre_cli review 02f831a1-c438-400c-9184-5cfb61779998 --key sre-my-review-01
```

第一条已实际执行，是只读；第二条会创建本地SRE报告，同key同来源回放。来源、Skill或父revision改变则需要新key。没有模型费用、批准或外部动作。CLI／API都使用现有任务DEV身份。

实际报告与回放证明见 `evidence/live-summary.json` 和 `evidence/M01.json`至`M04.json`；离线测试保留为离线，真实PG／HTTP证明另列。规则检查五类记录：完成审计绑定、fencing／lease／工件闭合、执行／清理、资源／扫描、真实MCP回执。inspect.state是工件导出时的paused快照；不能把Running=true误报成清理后仍存活。

本人练习：打开M03报告，分别解释operational_readiness、technical_gate、human_approval_state；在测试副本把cleanup改FAILED，运行对应单测，说明Supervisor交接为何改变。当前练习不证明本人已掌握SRE或ML。

本批没有造Architect／Android空适配器；后续须为它们提供实际输入、产物及职责检查。混合向量检索仍缺具体embedding接入授权，真实外部写入仍需target和批准。完整企业平台尚未完成。
