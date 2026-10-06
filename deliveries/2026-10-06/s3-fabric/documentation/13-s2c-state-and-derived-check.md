# S2C：派生检查与技术状态归属

本增量针对S2B两个实测缺口。旧12案例／13场景、原始失败、四份旧seal及审阅 ZIP均保留。S2B的语义4/5只覆盖五个真实模型场景；八个故障测试另计，因此当时完整验收为11 PASS、1 PARTIAL、1 FAIL。

## 修复契约

M03的合成冲突／过期资料不再仅附在模型上下文。`S2CInputProvider`读取冻结来源，验证原始UTF-8、语义manifest、基准fixture、精确scope映射、规则／策略／代码／Skill版本及评测时钟；Run、request hash、Step和scoped可信输入行在同一事务提交。调用者只能给scope、固定场景和key，不能给manifest、路径、代码或时钟。

专用`aurora-frozen-derived`标识由新检查工作流处理，旧worker拒绝它。新工作流继续使用原90秒租约、Scanner、官方MCP stdio握手／调用和Docker隔离边界。实际工件必须包含精确派生manifest；完成事务再次验证绑定，Gate按冻结合成时钟评估。租约、来源权限及审批有效期仍使用数据库当前时间，不能借评测时钟延长授权。检查结果、持久Artifact／Evidence行、snapshot、完成事件、source版本与hash必须一致。恢复继续按已有fencing和精确资源handle清理；本阶段新检查类拒绝业务dispatch。

M04的问题属于状态归属：技术完成状态是可确定的规则结果，文档注入或待人工批准不应让模型改变它。新模型report／summary只提交typed observation与concern IDs及最小推荐／交接字段；严格schema拒绝`status`、批准、身份或任意断言字段。服务器独立验证全部五项完整覆盖、唯一性、关注项与来源后，才根据实际catalog派生技术分析状态：全部SATISFIED／MEASURED为COMPLETE，否则INCOMPLETE。它是通用规则，不按M04或fixture答案特判。

`RawProviderReply`和`AnalysisCall.output_json`保存原始无status选择。内部类型投影仅供原编排交接，必须重新推导比对，不能当作Provider输出。新`S2CAnalysisStore.complete`检查四个唯一阶段、角色／step、租约、完整来源重授权、原文／校验记录、实际用量及PM／Supervisor catalog一致性；持久结果分为`selection`、`server_projection`和`rendered_report`。状态标为`SERVER_DERIVED_AUTHORIZED_CATALOG`，不会把服务器判断当作模型推理成绩。

## 模块与验收矩阵

| 模块／边界 | 本阶段验收 |
|---|---|
| `s2c_checks` | 原子幂等提交、scope FK、来源漂移／篡改拒绝、派生manifest经检查进入UNKNOWN、错误工件／超时／清理失败不成通过、中断与旧lease fencing。 |
| `s2c_context` | caller和角色的完整Run ACL，M03所有观测引用实际硬件check，过期／冲突可见但不假fresh，资料检索使用同一个派生验证器，撤权与重算hash伪造拒绝。 |
| `s2c_contracts/store/workflow` | raw无status、服务器状态分账、五项唯一覆盖、缺concern／自批／重复／model status拒绝，原文先落盘，取消／未知占用不授权发布，完整四次调用与重启零重发。 |
| `s2c_runtime/api/cli` | 固定本机Provider与最小Skills，严格场景选择、无body权限授予；人工hardware检查入口先授权；新loopback8771，不抢占旧服务。 |
| 独立冻结评测 | 原12案例／13场景完整重跑；5真实模型行为与8 TEST_DRIVER故障分别记录；结构、证据选择、服务器状态和完整gold分别核对，不挑子集。 |

新的模型质量范围为`EVIDENCE_SELECTION_ONLY_AND_SERVER_STATE_DERIVATION`。可比较证据选择是否完整，不能把新接口的服务器状态正确率说成模型学会了技术推理。真实结果以本阶段日交付和原始实验为准。

评测开始后发现独立gold的Python字典比较可能把布尔`true`与整数`1`混同。实际运行时来源hash／catalog派生已拒绝此替换。原grader没有中途修改；独立类型补充验证按canonical内容重核全部实际场景，并保留原grader缺口记录。补充验证在独立审查中又发现读文件后才取hash的时序缺口；修复为先捕获精确字节、按同一字节解析与计hash，发布前再验全部输入，变化即拒绝且零报告发布。旧复现与修复证据均归档。

首次完整实跑完成20次确认调用，五条链全部完成，五项实际检查gold及服务器状态均通过；冻结grader仍给出8 PASS／5 FAIL。排查确认其grounding payload hash把record字段展开，与真实持久层的`{scope, analysis_id, call_id, record: {...}}`嵌套契约不一致。实际原文、schema和运行时校验没有改写。原8／5报告保留，另用新独立评测版本对同一完整五条原始链和八项故障重新核验，不新增推理、不修改gold、不覆盖原实验。最终计数以日交付中明确标注版本的报告为准。

## 手动复现与学习

```sh
cd /path/to/forgeflow
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli preflight
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli demo --scenario M03 --key <全新key>
PYTHONPATH=src .venv/bin/python -m forgeflow.s2c_cli demo --scenario M04 --key <全新key>
```

新的key使用新的本机调用预算；同一key、同版本和输入返回持久结果。`show/records/events`读取已有analysis ID即可查看原文与服务器投影，无须再次推理。当前API使用`127.0.0.1:8771/docs`；`/s2c/health`声明新契约。API由本机DEV身份保护，运行凭据不进入交接包。

练习：对比M03实际manifest、checker工件、五项catalog与UNKNOWN；解释为什么过期CI不能作为当前通过证据。在测试副本中把bool改成int、漏掉一项关注或添加模型status，运行拒绝测试并说明触发边界。练习与复现不能替代本人已掌握后端／ML的证据。

完整五角色、真实企业SSO、开放式技术推理、混合向量检索、外部GitHub／办公写入及部署仍属后续阶段。本增量只执行既有本机模型和合成数据，不下载模型、不读取私人密钥、不批准阶段或写外部业务。
