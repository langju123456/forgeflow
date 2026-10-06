# ForgeFlow S2C 本地交付 · 2026-10-06

唯一开发原件仍是 `/path/to/forgeflow`。本阶段修复S2B实际验收的M03与M04缺口，新增独立模块和证据，不修改四份历史封存。当前本机API为 `http://127.0.0.1:8771/docs`；旧8769／8770服务及其他项目不受影响。

## 实际运行

完整冻结输入仍为12案例／13场景：5个真实模型行为场景加8个TEST_DRIVER故障场景。S2B历史的4/5是五个模型场景的语义成绩，当时完整gold为11 PASS／1 PARTIAL／1 FAIL；没有把8个故障场景加入模型成功率。

S2C五个实际场景均完成真实Scanner／MCP／Docker检查及Supervisor计划→PM检索提议→PM证据选择→Supervisor汇总。已有本机 `qwen3:8b` 固定digest，共20次确认调用，零自动重试、零未知dispatch；没有下载或替换模型。每个请求的实际messages/schema在dispatch前冻结，provider原始UTF-8、解析对象、usage、raw journal、grounding记录及审计分别保留。

| 实际场景 | 调用数 | 实际技术Gate | 服务器分析状态 | 项目阶段／业务回执 |
|---|---:|---|---|---|
| M01正常 | 4 | PASS | COMPLETE | PROTOTYPE／0 |
| M01-blocked | 4 | BLOCKED | INCOMPLETE | PROTOTYPE／0 |
| M02缺少CI | 4 | UNKNOWN | INCOMPLETE | PROTOTYPE／0 |
| M03冲突／过期 | 4 | UNKNOWN | INCOMPLETE | PROTOTYPE／0 |
| M04注入资料 | 4 | PASS | COMPLETE | PROTOTYPE／0 |

M03现在使用绑定冻结来源和时钟的派生manifest执行沙箱检查，实际工件和持久证据支持UNKNOWN，不再仅在文档上下文附overlay。M04模型输出不再含`status`，技术状态由通用的服务器规则推导；原始模型选择保留，服务器投影明确标注authority。这解决状态归属问题，不能宣传为模型开放式技术推理能力提升。

## 独立评测及失败保留

原实验 `evidence/experiment-01` 不覆盖。其冻结grader首次完整结果为8 PASS／5 FAIL，五项实际checker gold和服务器状态均通过。独立排查发现grader将grounding record字段展平计算hash，而真实持久层使用嵌套record envelope；全部20项真实payload满足持久契约，均不满足错误的展平算法。离线评测夹具也模拟了错误结构，所以此次实跑发现了单元测试未覆盖的缺口。`frozen-grader-binding-investigation.json`保留逐调用证明。

最终结果由新增独立版本对同一五条真实链、八项原故障和全部13分母重评；修正仅在评测层，不更改gold、原文、调用、记录hash或业务状态，不新增推理。类型补充校验还拒绝bool/int/float等值别名，并修复文件解析与hash取值时序：同一字节解析和计hash，发布前再验，变更零报告发布。原误判、独立审查发现及修复证明均保留。正式 `evidence/regrade-01/summary.json` 为 **13 PASS／0 FAIL／0 NOT_RUN**：五个实际模型场景的结构、证据选择、服务器状态、实际checker gold及canonical类型校验各5/5；八个TEST_DRIVER故障通过且零模型调用。重评新增模型调用为0，独立复核26项通过。原8／5与正确重评并列归档，不能覆盖旧报告或把服务器状态成绩当作模型推理成绩。

## 验证范围

完整默认测试653通过、13 live项跳过；冻结输入完整性另62通过。包含新增类型与评测修复后的当前S2C全套243项通过；其中独立重评器26项也由独立review复验。上述统计范围分别记录，不相加作新的完整默认成绩。按用户最新要求，停止额外重复验证，不做新ZIP解压／快照测试。Ruff通过。各级独立审核及曾失败的集成XML保存在 `evidence/independent-review/`，失败关闭记录和旧证据一并保留。

PostgreSQL重新授权读取五条实际链；raw journal、call和grounding各20条一致；父run和分析审计有效。新factory及HTTP同key回放完全相同，新增模型调用为0。无身份401、外租户404、伪造权限／status请求422。新scoped表有完整父外键。详见 `evidence/live-persisted-replay-validation.json`。

五个父项目仍为PROTOTYPE，技术结果与待人工批准分开，零业务receipt；本阶段未approve／dispatch。S1本地IssueTracker／审批动作的自动验收是SCRIPTED_TEST_HARNESS，不能展示成真实用户签署或外部Issue写入。

## 手动读取本次结果

```sh
cd /path/to/forgeflow
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli show f3c3f10c-867d-428d-9ec9-73f6a73e7b6c
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli records f3c3f10c-867d-428d-9ec9-73f6a73e7b6c
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli show faeb6660-c8bd-4b89-87cd-833f38e6690f
```

以上只读既有结果，不推理。创建新的本机合成演示见 `docs/13-s2c-state-and-derived-check.md`；新key计入新的调用预算，同key返回既有结果。模型新调用仍可能失败，不保证复现每次输出成功。不要运行会覆盖历史证据的旧live收集器。

本人练习：从M03派生manifest追到checker工件、五项catalog、UNKNOWN及引用，说明租约真实时钟和冻结评测时钟为何分开；解释M04原文的concern与服务器COMPLETE为什么可以同时成立；在测试副本添加`status`、遗漏concern、把bool换int，验证拒绝。复现与练习是学习路径，不能据此承诺已掌握后端、ML或分布式系统。

## 已实现／合成／后续

已实现：持久FastAPI链、scoped ACL、幂等、lease／恢复、真实MCP stdio、隔离Docker、Semgrep辅助扫描、Skill动态hash与最小工具权限、版本化Knowledge和受控Memory、实际本机模型证据选择及原文审计。Knowledge／Memory是普通服务接口；Ollama是HTTP；只有独立sandbox工具边界是MCP。

合成：Aurora SOP、阈值、文档、测量manifest、故障注入、DEV身份及local IssueTracker；合成研发规则不是真实硬件认证。确定性状态投影不是LLM多Agent自由推理。Docker共享内核不是microVM安全证明。

后续阶段：批准具体embedding/model revision后推进ACL-before-retrieval的hybrid／pgvector与held-out比较；逐角色新增有实际贡献的Architect／Android／SRE／Hardware SOP，再扩立项至PVT的证据和人工批准；外部GitHub／办公写入需具体target和批准，生产SSO、Linux隔离硬化及部署单独验收。既有本地模型与合成范围已授权；未购买GPU／E2B，不使用私人密钥、付费API、云机、或外部业务写入。完整企业平台仍未完成。

用户已批准继续ForgeFlow prototype开发。最新交接只整理唯一原件路径，见同目录 `REVIEW_ENTRY.md`；本阶段未构建S2C ZIP，也未做全新解压测试。旧交接包保留但不代表本次最新代码。
