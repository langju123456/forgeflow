# S2B：把模型输出绑定到实际证据

本阶段修复 S2A 的一个实际失败：PASS 报告引用通用 SOP 的“unknown 不得通过”规则，写出了并不存在于当前测量中的 unknown。旧报告、原始响应和封存保留；本阶段是独立的应用版本。

当前模型任务是 **constrained evidence selection**。模型选择带来源的结构化观察和问题 ID，服务端校验覆盖与状态，再生成中文报告。它不是开放式技术推理能力的完整评测，也不把服务端生成的文字算作模型原创结论。

## 模块契约

| 模块 | 输入／输出与边界 |
|---|---|
| `grounded_context` | 先用既有 Store 检查调用者与接收角色对完整 Run 的权限，再附加精确授权的合成评测来源。冻结 scope 到本地 scope 的映射有独立 witness；内容、版本、分类与来源 hash 均重新检查。 |
| `grounded_contracts` | 五项 observation 必须完整且唯一，concern 集合必须完全匹配。只有全部 SATISFIED / MEASURED 才能 COMPLETE。报告与摘要没有自由断言字段。 |
| `grounded_workflow` | 继承既有四次调用、租约、幂等、最小工具权限和预算；新 Skills 动态加载并绑定 hash。每次请求在 dispatch 前冻结实际 messages/schema。 |
| `grounded_store` | 新增独立表，原始 Provider 响应先提交，随后记录结构与语义校验结果。只读接口重新检查完整来源权限；raw、判定、renderer 分开。 |
| `grounded_runtime/api/cli` | 本机 DEV 身份与固定 Ollama 模型；仅五个行为场景能进入真实模型工厂。故障配置不能通过 API 变成模型 prompt。V1保留供历史读取。 |
| `grounded_selection/runtime/api` | V2在每次输出schema中枚举实际合法的claim IDs，明确status只描述测量证据。完整catalog与动态schema在dispatch前绑定；ContextVar按引擎和异步任务隔离。V2工厂共享Provider，并能复核V1历史记录。 |
| `s2b_evaluate` | 原始 12 cases / 13 scenarios 不改。5 个模型行为场景、8 个 TEST_DRIVER 场景分别执行和统计，独立比对冻结值、来源版本与 hash。 |

catalog 的 observation 保存 `source_ref, metric, value, check_state, evidence_state, authority`。claim ID 由这些字段派生；每次调用的 material 在 dispatch 前绑定 catalog hash，并在持久化事务内重新从真实 Run 和授权来源推导。通用 SOP 只提供规则，不能成为实例测量。

第一轮真实实验跑完五个场景、17次已确认调用：结构5/5通过，语义2/5通过。PASS的INCOMPLETE误判、BLOCKED／注入场景的ID误抄均被拒绝，原文和用量保留。V2是基于人工检查这些失败后新增的应用版本与实验，不是自动repair或Provider重试。枚举只限定可选ID；重复、遗漏、错误状态仍由原语义校验拒绝。

第二轮V2真实实验跑完同一五个场景、19次已确认调用：结构5/5通过，语义4/5通过。正常PASS、thermal阻塞和缺失CI符合完整冻结gold；M03仍为PARTIAL。M04选择了全部正确ID及注入concern，却把完整测量状态写成INCOMPLETE，触发`UNSUPPORTED_COMPLETION_STATUS`，报告未发布。两轮共36次真实调用、零自动重试；这些结果只评价受约束的证据选择。8个故障场景由TEST_DRIVER分别执行，8/8通过、零真实模型调用。按完整冻结验收合并13场景是11 PASS、1 PARTIAL、1 FAIL。

当前全套离线测试为470 passed、13 skipped；冻结资料完整性检查62 passed。另用新进程与真实PostgreSQL/API复核五个持久链，同key重放零新增模型调用，并核对来源授权、scope外键、MCP协议证据、扫描、沙箱清理及审计hash链。离线测试、真实检查与模型质量分别记账。

原始响应表保存原始 `content_utf8`、实际使用量、Provider 状态、输入 hash、catalog hash及完整 scope/call 绑定。已校验记录引用它的不可变 payload hash。数据库特权写入者仍属于信任边界，hash 不是密码学签名。

## 威胁模型与验收

| 风险 | 执行边界 | 验收证据 |
|---|---|---|
| 有合法引用但事实不成立 | 类型约束与精确 coverage 校验独立于 JSON schema | PASS 中增加虚构 unknown：结构合法、语义拒绝、原文留存、无可读报告发布。 |
| 租户、角色或来源被替换 | Store 全 Run ACL、角色交集、源版本与 catalog 在事务内重验 | 跨租户、授权撤销、内容漂移、重算 hash 后伪造都拒绝。 |
| 文档要求执行、自己批准或切换身份 | 不可信文档是数据；结构化工具参数与能力白名单控制调用 | M04 原文进入前三个真实模型请求；摘要接收已验证 PM 报告和来源描述。来源全文的授权仍复核。 |
| schema 成功冒充模型质量成功 | 保存结构与语义两个结果，并独立比对冻结 gold | 每个行为场景报告结构、语义、来源、实际调用与完整 frozen gold 状态。 |
| 超时或丢回复后重复生成 | 既有持久调用意图、全局 Provider slot 和未知占用 | M10 / M12 保留未知 reservation；重启或同 key 不再 dispatch。 |
| 已接受输出但原文在崩溃中丢失 | 原始响应先于 finish_call 提交 | 中断点测试：原文可读，未知占用不会被自动释放。远端收到回复与首次落盘之间仍不能原子提交。 |
| 取消后发布新报告 | 当前取消状态阻止新的 renderer 发布；历史记录不可变 | 取消竞态测试保留 raw，拒绝新 COMPLETE 报告。 |

固定预算不扩大：每个 Run 最多四次调用，3500 个保守 UTF-8 byte 输入上界，800 输出 tokens，单次最多60秒、总计180秒、并发1、零自动重试。byte 上界不是精确 tokenizer 计数。摘要省略已看过的注入原文，保留来源描述与完整授权绑定，使请求留在预算内。

M03 的原始硬件输入仍是 canonical PASS。派生文档把 thermal 标记 conflict、CI 标记 expired，模型应报告 UNKNOWN / INCOMPLETE。**本阶段没有把派生文档送入独立沙箱检查，因此完整 frozen hardware gold 必须记为 PARTIAL。** 资料规则分析与实际检查保持分开。

## 手动复现

在唯一原件项目目录运行；默认不下载模型或加载私人密钥：

```sh
cd /path/to/forgeflow
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 preflight
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 demo --scenario M01 --key my-grounded-pass-v2-01
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 demo --scenario M02 --key my-grounded-missing-v2-01
PYTHONPATH=src .venv/bin/python -m forgeflow.grounded_cli --selection-v2 demo --scenario M03 --key my-grounded-conflict-v2-01
```

同一 key、相同版本与请求返回相同持久结果；新的 key 是新的运行与模型预算。`--selection-v2 show`、`records`、`events` 接收 analysis ID，读取仍复核来源授权。省略flag使用V1。真实V2 API 使用 `127.0.0.1:8770`，模块为 `forgeflow.grounded_selection_api`，`/grounded/selection-health`显示版本；旧 S2A 的8769保持独立。人工批准与实际 Issue receipt 均不由模型分析产生。

可离线重跑故障评测：

```sh
PYTHONPATH=src .venv/bin/python scripts/s2b_evaluate.py --faults --output /tmp/forgeflow-my-fault-results-01
.venv/bin/pytest -q tests/test_grounded_contracts.py tests/test_grounded_context.py tests/test_grounded_store.py tests/test_grounded_workflow.py tests/test_grounded_api.py tests/test_grounded_frozen_eval.py tests/test_grounded_selection.py tests/test_selection_runtime.py
```

完整真实V2评测入口为 `scripts/s2b_live_evaluate.py --selection-v2 --output <全新目录> --key <新key>`。它会执行五个真实模型场景，归档实验与每次请求；不能把已封存目录用作输出。发生未知 dispatch 时停止后续场景，保留占用，不自动重放。

## 学习与后续阶段

先手动看 M01 与 M02 的 catalog、原始响应和可读报告，再说明为什么五项完整证据、模型 COMPLETE、Gate PASS、人工 APPROVED、实际 receipt 是不同状态。练习只修改测试副本：在 PASS 输出加入一个不存在的 concern，运行拒绝测试；再删除 CI observation，观察 coverage 拒绝。完成练习不等于已经掌握全部后台或 ML 技能。

接下来按真实必要性推进：

1. 为冻结派生 manifest 增加持久、受租约控制的可信输入提供者，把 M03 经真实扫描／MCP／沙箱执行后再评 Gate，消除当前硬件 gold 的 PARTIAL。
2. 在已经冻结的词法评测上验证检索改进；embedding 接入尚未安装或下载，先明确模型、空间与授权，再比较指标。
3. 增加有真实 SOP 和工具任务的角色、Prototype／DVT／PVT 规则与协作，持续保存失败案例。真实生产认证与五角色完整平台尚未完成。
4. GitHub／办公写入、部署、云模型及付费资源在具体 adapter 与权限缺口可审查后另行处理；本阶段只用本机合成来源。
