# ForgeFlow · Oct6 S2A 真实本机模型链交付

项目已经在原机实跑：专属PostgreSQL、Semgrep、真实stdio MCP与隔离Docker检查后，同一Aurora run进入qwen3:8b的Supervisor→PM→Supervisor四次调用，持久保存来源、检索收据、输出、用量和审计。本次仍是DEV_ONLY、合成研发示例；完整企业平台、真实硬件认证和完整模型质量验收未完成。

唯一开发原件：`/path/to/forgeflow`。
开发API：`http://127.0.0.1:8769/docs`；最终启动PID见 `s2-evidence/api-relaunch-final.json`，启动/运行命令见 `docs/10-local-model-demo.md`。其他本地工程未改。本次未发送外部Issue/群消息或上线。

## 已实跑的三种场景

| 输入 | 技术Gate | 四次实际模型调用 | 分析state | 原项目阶段／业务执行 |
| --- | --- | --- | --- | --- |
| aurora-pass | PASS | 全部VALIDATED；80596ms，2172输入/691输出tokens | COMPLETE | PROTOTYPE；2项待批准；0回执 |
| aurora-blocked | BLOCKED | 全部VALIDATED；127030ms，2190输入/703输出tokens | INCOMPLETE | PROTOTYPE；1项待批准；0回执 |
| aurora-unknown | UNKNOWN | 全部VALIDATED；118480ms，1962输入/555输出tokens | INCOMPLETE | PROTOTYPE；1项待批准；0回执 |

上述时间是四次provider耗时之和，token为实际Ollama计数，未计成本金额。BLOCKED报告正确识别89°C超过合成70°C阈值；UNKNOWN报告正确指出ci_smoke缺失。分析INCOMPLETE和“调用链四步已执行”分别记录，没有强行改成COMPLETE或PASS。

本次三种场景共12次真实模型调用；此前另有1次小型schema conformance、attempt01的2次有效调用和attempt02的4次有效调用，合计19次。没有自动重试或云端回退。固定本机qwen3:8b digest为 `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`，来自调用前后元数据，不自称请求级密码学证明。

汇总与实际DB重核：`s2-evidence/live-acceptance-final.json`。三组 `live-*-attempt-03.json` 和对应 `-events.json` 保留每次实际输出/用量/来源及真实检索收据。Knowledge/Memory为IN_PROCESS服务，模型为Ollama HTTP；MCP仅S1的sandbox.execute，不能混称。

## 失败与质量状态

- attempt01：首两次模型输出有效，但第3次发送前超过3500输入保守上界，FAILED。原失败及用量保留；精简上下文后使用新key，没有提高预算或改写旧输出。
- attempt02：四次调用有效，PM因分析层把真实MCP receipt里不存在的status字段映射成UNKNOWN而返回INCOMPLETE。已修为校验真实握手/响应/调用记录后标RECORDED；原报告保留。
- 最新PASS03：仍出现 `Unknown evidence status`，引用的是SOP的通用unknown规则，不能支持该PASS实例存在未知测量。这是观察到的unsupported语义声明；**模型语义质量未完全接受**。引用/JSON合法不等于结论正确，Gate和动作权限继续由独立规则与人工批准控制。
- 真实模型timeout/中断的请求级远端停止证明未验收；本次新增取消/超时/HTTP500unknown槽/跨实例重复发起故障使用明确TEST_DRIVER，未知结果不会自动释放槽或重新生成。

固定budget为4 calls、输入保守界3500、输出800、单次60秒、整链180秒、并发1。消息/schema UTF-8字节加余量不是精确tokenizer，真实token用量另存。旧context版本当前读/发表会报ANALYSIS_CONTEXT_CHANGED；历史JSON归档未删，不将旧材料当成新版本继续执行。

## 验证与审阅

- S1真实回归：136通过，0失败/0skip；`s2-evidence/s1-regression/pytest.xml`，涵盖真PG、Semgrep、MCP、Docker、超时及SIGKILL恢复。审批者标SCRIPTED_TEST_HARNESS，非用户已批准。
- 新S2：176项新增测试；加旧准备62项完整性，共238通过，0失败/0skip，见 `final-s2-and-frozen-integrity.xml`。模型provider fault用Mock/TEST_DRIVER，不能算模型质量通过。
- 新台账实际PostgreSQL：43通过，见 `s2-postgres-ledger/pytest.xml`；模型输入材料为TEST_DRIVER，不调用模型。
- 真同key跨进程重放：相同analysis/四条call；`live-pass-replay-03.json`。三组parent/audit重核、链hash及stage/approval/零receipt见实际汇总。
- 真loopback HTTP最终服务：授权读取200，匿名与伪角色headers均401；`http-smoke-final-server.json`。`python -O`仍执行验收条件，坏响应负例拒写结果。
- 独立最终审阅无剩余confirmed P1/P2，见 `runtime-review-final.json`。修复了HTTP500未知结果错误释放槽、来源重授权/ORM stale fencing、优化模式跳过验收和MCP witness非法响应语义等问题；早期review和失败证据未覆盖。
- Ruff全项目通过；原S1 95项与S2准备38项封存均UNCHANGED，见 `s1-seal-final.json`、`preparation-seal-final.json`。新S2列通过显式 `schema-migration.json` 迁移，未改S1表。

## 阅读与后续交接

先读 `docs/10-local-model-demo.md`（手动复现/讲解/本人修改练习），再读 `docs/11-s2a-contracts-threats-acceptance.md`（新契约/威胁/验收矩阵/下一阶段）。当日可读文档另存 `deliveries/2026-10-06/docs/`；代码只在原工程持续开发，审阅 ZIP是审阅快照。

审阅新入口在仓库外的交接目录。旧审阅包描述S2准备状态，保留作为历史，不与新包混淆。新包排除.runtime/venv/数据库/模型和3张私人截图，旧manifest原字节保留；便携原S1仅核92/95，其缺失必须明示。

下一阶段优先typed断言与语义grounding、冻结正负case和实际模型评估，再按必要性做检索/embedding与更多角色，不堆空壳。原冻结12模型cases/13scenarios尚未整体执行；47题词法评估既有失败不因本次三条链而改善。外部DevOps/办公写入、SSO/RLS、多人部署、完整五角色与DVT/PVT仍待后续。没有新embedding或云端模型/私人key接入，不擅下载、购买或上线。
