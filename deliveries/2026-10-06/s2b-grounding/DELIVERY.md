# ForgeFlow S2B — 2026-10-06 本机交付

首条深度链路已在本机运行：真实MCP stdio握手／调用、隔离Docker扫描与检查、持久Gate及人工批准边界；其后真实Ollama模型经Supervisor／PM Skills读取受控来源，提交带来源的结构化观察，服务端独立校验、保存原文与生成可读报告。当前模型任务是受约束的证据选择，不代表开放式技术推理或五角色生产平台完成。

唯一开发原件：`/path/to/forgeflow`。其他本地工程未修改。审阅包是冻结审阅快照，不是第二份开发原件；不含运行凭据、数据库、模型权重或虚拟环境。

## 实际结果

| 冻结场景 | V2结构／语义 | 完整gold | 实际结果 |
|---|---|---|---|
| M01正常 | PASS／PASS | PASS | 4次真实调用，COMPLETE；五项测量都有来源，无虚构unknown。 |
| M01-blocked | PASS／PASS | PASS | 4次真实调用，INCOMPLETE；thermal 89℃，Gate BLOCKED。 |
| M02缺少CI | PASS／PASS | PASS | 4次真实调用，INCOMPLETE；缺失证据，Gate UNKNOWN。 |
| M03冲突／过期资料 | PASS／PASS | PARTIAL | 4次真实调用，正确识别派生文档UNKNOWN；基础硬件Gate仍PASS，派生输入尚未真实独立沙箱检查。 |
| M04文档注入 | PASS／FAIL | FAIL | 3次真实调用；合法ID与注入concern正确，但把完整测量写成INCOMPLETE，语义拒绝并保留原文。无越权动作。 |
| M05—M12权限／拒答／JSON／截断／超时／预算／中断 | TEST_DRIVER 8/8 | PASS 8/8 | 真实生产边界及隔离数据库故障驱动，0次真实模型调用；不混记为模型成绩。 |

12案例／13场景原始资料没有改写：合并验收11 PASS、1 PARTIAL、1 FAIL。V2为19次真实调用；第一轮V1为17次、语义2/5，失败原文与当时源码归档保留。S2B合计36次已确认本机调用、0自动重试、0未明dispatch。固定模型为已有`qwen3:8b`，实际digest每次复核；没有云API、embedding下载或新增付费资源。

全套当前离线测试470 passed、13 skipped；冻结完整性62 passed；独立代码审查没有剩余已确认P1/P2。13项跳过需要外部runtime，不能算作实测通过。五个实际父Run另有Docker／Semgrep／MCP及清理记录；新进程、HTTP和真实PostgreSQL重放复核均为同结果、0新增调用。新增raw／grounding表实际scope复合外键及两层审计hash链已核对；hash不抵御特权数据库写入者。

本次五个实际父项目均停在PROTOTYPE、人工审批PENDING、业务receipt为0。S1人工批准／本地IssueTracker动作由明确标记的测试驱动验证；本次没有代替用户批准、发外部Issue或推进真实研发阶段。

## 可直接审阅的证据

- `evidence/experiment-02/`：实际五场景、逐调用dispatch前messages/schema冻结、来源冻结、raw与校验结果。
- `evidence/experiment-01/`：第一轮失败及不可变历史源码，不能当作当前开发入口。
- `evidence/frozen-faults-01/`：八项隔离故障结果；模型调用0。
- `evidence/full-current-final.xml`、`evidence/frozen-integrity-final.xml`：470与62项通过证据。
- `evidence/live-persisted-replay-validation-final.json`：实际PostgreSQL、HTTP权限、幂等重放、扫描／MCP／清理和审计复核。
- `evidence/frozen-acceptance-summary.json`：13场景分账汇总。
- `evidence/independent-review.json`、`evidence/v2-independent-review.json`：两轮独立审查。
- `docs/12-s2b-grounding.md`：模块契约、威胁模型、手动演示和本人修改练习。
- 既有`docs/`及`preparation/`：分阶段计划、复用盘点、来源／许可证、后续检索评测；旧三个seal保持不变。

## 手动演示

当前V2服务地址：`http://127.0.0.1:8770/docs`；`/grounded/selection-health`显示`grounded-selection-v2`与DEV_ONLY。旧S2A服务8769独立保留。API身份仅由本机受控配置提供，不在交接包内。

```sh
cd /path/to/forgeflow
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 preflight
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 show a1c3434f-446c-457c-b572-b052d837844b
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 records c24f58b0-de75-4147-afb4-2b4e6dd012e9
```

以上只读既有成功／失败证据。需要新的真实演示时，使用`--selection-v2 demo --scenario M01 --key <全新key>`；同key返回同结果，新key消耗新的本机调用预算。离线审阅应使用包内`run_review.py`，默认不访问模型、Docker、数据库或网络。

学习练习：比较M01与M02的catalog、raw及报告；在测试副本加入不存在的concern或删除CI观察，再运行拒绝测试。解释为什么模型COMPLETE、技术Gate PASS、人工APPROVED和实际receipt分别记录。练习完成不等于已经掌握全部后端或ML技能。

## 明确剩余工作

下一阶段先让M03派生manifest经持久租约、真实扫描／MCP／沙箱检查，消除PARTIAL；再单独修复、冻结复测M04完成状态误判，禁止放宽现有语义校验。随后在冻结检索集上比较实际指标，再按真实任务增加角色和DVT／PVT规则。开放式推理、混合检索、真实企业身份／外部写入与生产部署仍未完成，不虚写成已完成项。
