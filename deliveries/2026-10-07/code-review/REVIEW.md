# 接管审阅 · 2026-10-07

审阅结论：接管后的修复有实质内容，主要补上原来只在当前 HTTP 入口阻断的旧审批路径，并把历史冻结回放锚定到 Git。没有发现标准加载入口仍可授予旧硬件审批；但发现四项需要修正的 P2，其中审批升级问题已经影响当前演示的历史记录读取。时钟工作仍是未提交完成的增量，不能作为已交付新能力计入。此次没有发现所审增量中的 P1；不等于整项目生产安全认证。

## 接管范围与完成状态

依据是项目自己的 Git 提交、工作区 diff、封存 hash 与运行证据，没有读取私人会话。基线 `a185c622108779c85b308402568bf776af02eef3`，tag `freeze-baseline`，提交说明为 `baseline: design, implementation and evidence up to 2026-10-06`。之后有八个修复提交。

时间以下按 Asia/Shanghai：

| 时间／提交 | 工作 | 状态 |
| --- | --- | --- |
| 10-06 19:46，`a185c62` | 建立接管前 Git 基线、忽略 runtime／凭据／缓存 | 已记录；旧交付可在此版本校验 |
| 10-06 23:22，`7a24cd7` | 编写旧审批绕过的失败测试，驱动真实 local CLI | 已提交测试 |
| 10-07 07:34–07:37，`25d44ad`／`5d73a03` | 新增 Git anchor；冻结源码从 baseline blobs 读取；调整重评末尾校验 | 已合并；仍有下述来源记录问题 |
| 10-07 07:37，`1491a8e` | 新增 `EVTBoundStore`，local loader、FabricStore 和两个内部 wrapper 接入；拒绝旧 APPROVED／approved dispatch，并审计拒绝 | 已合并，修复方向有效；引入 adapter 升级回归 |
| 10-07 09:06，`ac14c00` | HTTP 决定交给 Store，确保拒绝可审计；EVT app 强制使用 gated Store | 已合并 |
| 10-07 09:08，`8fd4047` | 真正从 Git anchor 导出代码运行冻结回放，比较历史证据字节及源码 hash | 已合并；正常路径通过，子进程选择器有漏洞 |
| 10-07 10:00，`d5bc3eb` | 合并 `fix/legacy-approval-bypass` 到 master | 已完成 Git 合并；未见新实跑交付 |
| 10-07 11:05，`cbfcdaa` | `fix/fixture-expiry` 提交统一测试时钟规格 | 当前 HEAD；提交说明明确为“clock 实现前应失败”的测试 |
| 同分支未提交部分 | 5 个生产模块改为动态调用 `contracts.utcnow()`，调整其他测试，新增 `tests/conftest.py` 的 session clock | 工作区草稿；16 个 tracked 修改、1 个 untracked fixture，不属于完整 HEAD |

master 的修复共 13 个文件，633 行新增／22 行删除；当前 HEAD 加工作区共 31 个路径变化（包含 untracked `conftest.py`）。新旧文件具体 hash、已提交／未提交分类及完整 diff 见 `evidence/inventory.json`、`committed.patch`、`uncommitted.patch`。`gate.py` 虽有新修改时间，但字节与基线相同，不计为新增规则。

## 四项审阅发现

### F1 · P2 · 代码升级后旧回执不能重放，失效审批不能重新申请

位置：`src/forgeflow/fabric_store.py:287`、`:347`、`:255–265`；触发变更是 `:112` 的 transaction wrapper 改用 `EVTBoundStore`。

`adapter_hash()` 是整个 `fabric_store.py` 文件 hash，本次变更从封存的 `9fc51fa…` 变为 `2f9f6c4…`。未执行的提案需要重新审查是合理的；问题在于代码没有重新申请通路：同 key 返回旧失效 proposal，新 key 又被每 run／kind／target 的唯一限制拒绝。待执行的 ISSUE_RETRACT 等补偿因此可能无法更新批准。当前 `execute()` 还在查询已有 receipt 之前检查当前 adapter hash，连已提交效果的只读回执重放也拒绝。

隔离 SQLite 复现记录 `evidence/repro-probes.json`：

- 已提交 ISSUE 的重放：`FABRIC_PROPOSAL_CHANGED`。
- 旧版本待审批 ISSUE_RETRACT 的决定：`FABRIC_PROPOSAL_CHANGED`。
- 同 key 重新申请：返回旧 adapter hash。
- 新 key 重新申请：`FABRIC_ACTION_ALREADY_PROPOSED`。

实际 8773 的只读 GET 也返回旧审批 `409 FABRIC_PROPOSAL_CHANGED`。旧技术报告因源码指纹改变返回 `409 FABRIC_SOURCE_CHANGED`；这是来源变化后的拒绝，不是伪造 PASS，但当前旧演示确实不能继续按原入口读取／重放。

最小修正：历史 receipt 的授权／完整性读取与“当前 adapter 能否执行新效果”分开；为未消费、失效 proposal 提供有审计的版本替换／supersession，保留旧提案和决定。新效果仍须按当前来源重新批准，避免仅放宽 hash 校验。增加跨版本重放和待执行补偿更新的测试。当前真实六条历史回执已全部补偿；“待补偿被卡住”是在隔离夹具复现，不声称当前数据库仍有未撤回效果。

### F2 · P2 · 当前重评执行的依赖与记录的历史源码 hash 不一致

位置：`scripts/s2c_regrade.py:40`、`:254–259`、`:371–373`。

历史 hash 校验现在读取 Git anchor，而 `original` evaluator、typed helper 及其导入模块仍从工作区执行。发布时只复查两个 `REVIEW_PATHS`。`review_source_files_sha256` 混合历史 50 个文件 hash 与两个当前 review 脚本 hash，不能完整说明新重评实际执行了哪些字节。依赖变动可能不被记录，也不被末尾 source-drift 检查拒绝。

只读探针确认 current map 与工作区已有 6 个冻结路径不同，详见 `repro-probes.json`。这不意味着旧实验或既有 regrade-01 被篡改：真正的 anchor 回放测试已独立执行历史代码，原归档仍与基线相同。问题是未来使用当前程序发布新重评时的来源证明。

最小修正：分开记录 `anchored_experiment_sources` 和实际 `review_dependencies`；后者按实际加载的 current／export 路径取 hash，并在发布前复查。若声称冻结重评，就在 anchor 隔离代码环境执行；若是当前算法重评，就明确记录当前依赖版本。现有测试只改返回的 hash map，没有验证执行依赖发生变更的路径。

### F3 · P2 · 统一测试时钟仍遗漏 gate 默认时钟，测试会在 11-05 后失效

位置：`tests/test_analysis_context.py:90`、`tests/test_analysis_workflow.py:162`、`tests/test_grounded_context.py:72`，以及只扫描 package gate 调用的 `tests/test_clock.py:90`。

这些共享夹具仍调用 `evaluate(manifest, result)`，使用 gate 内的真实 UTC；修改后的 builder 显式使用测试时钟 Oct5。证据到 2026-11-05 过期后，夹具写入 UNKNOWN，builder 按 Oct5 复算 PASS，报 `SOURCE_CHANGED`。当前日期的 green 不能证明它已经解决长期 fixture expiry。

`evidence/clock-fixture-repro.json` 使用原共享夹具，单独模拟 gate 默认日期为 Nov6、contracts 日期为 Oct5，实际得到 stored UNKNOWN／builder SOURCE_CHANGED。没有调整系统时钟、没有实跑设备或模型。

最小修正：共享夹具显式传 `now=contracts.utcnow()`；把 gate-call 时钟检查扩展到测试调用，真实时钟用例单独标记。生产 `contracts.utcnow()` 仍是当前 UTC，未新增 caller-controlled 时间或实时过期绕过；这一点值得保留。

### F4 · P2 · 冻结回放子测试可以漏跑关键用例仍被判成功

位置：`tests/test_frozen_replay_at_anchor.py:63–68`。

子进程继承 `PYTEST_ADDOPTS`，父测试只检查 returncode0 和 stdout 含 `passed`。将完整 archive replay 用例 deselect 后，子进程显示 `25 passed, 1 deselected`，父断言仍成功。`evidence/frozen-selector-repro.json` 已在临时 anchor 导出中实际复现。

最小修正：子进程移除继承的选择／跳过参数，并检查固定预期 case 集合、无 skip／deselect。此发现不表示正常环境的回放失败；正常环境在本轮针对性测试中通过。

## 我实际验证了什么

本轮针对性执行 **94 passed**，覆盖 legacy gate、Git anchor、真正 anchor replay、clock、EVT、Fabric、domain。对应 source／helper 的 Ruff 通过。不是新全套测试、审阅 ZIP 解压验收或新模型实验。原 marker／fixture 钟移测试已覆盖，但两个时钟来源分裂是额外复现发现。

证据文件：

| 文件 | 证明 |
| --- | --- |
| `evidence/focused-tests.xml` | 94 项当前工作区测试，0 failure／error；不将 uncommitted 工作区结果称为 HEAD 交付结果 |
| `evidence/repro-probes.json` | 跨版本审批／回执回归、gate 默认时间分裂、当前源码记录差异 |
| `evidence/clock-fixture-repro.json` | 实际共享 fixture 的未来日期失败 |
| `evidence/frozen-selector-repro.json` | 漏跑完整 archive replay 仍被外层接受 |
| `evidence/live-read-only.json` | 当前 8773 的 health、PID／启动时间、两个实际只读 409 |
| `evidence/inventory.json` | Git 归属、增量 hash、历史 manifest 校验 |

五份 S2C／S3A／S3B／S3C／S3D manifest 的全部 259 个列项在 baseline commit 都匹配，历史证据目录没有被改写。当前工作区部分源码和测试当然已与旧 manifest 不同，应按 Git 版本区分，不能继续用旧 CURRENT 文档声称“当前代码封存校验全通过”。接管后新增的 delivery 文件只有 Git anchor；其中自报的 `824 passed / 13 skipped` 明确写成未归档，本审阅不把它当可复核测试证据，也没有重跑整个 suite。

## 当前运行与项目边界

当前服务仍是 PID69967，进程于 10-06 启动，早于上述变更。没有重启／新 API 实跑交付证据；磁盘改动不能直接视为已在该进程生效。动态源码 hash 已读取新磁盘版本，旧报告和审批因此拒绝，出现了“旧进程＋新源码指纹”的状态。

未发现这段接管增加 Android SDK／APK／device 证据、新模型调用、其他 LLM 职责、pgvector、远程 Issue／办公或新 NPI stage。此前 Supervisor／PM 模型实验、默认 EVT UNKNOWN、TEST_STAGE 本地模拟和其余确定性职责边界保持原样。没有把维护修复或新测试数量等同于新产品能力。

建议下一步依次处理 F1 的升级／历史读取、F2 与 F4 的回放来源／case 集合、F3 的余下 fixture 时钟；将完整工作区实现提交后再做相关验收、验证受控进程替换，最后新增本日交付和当前入口。可保留 master 的 legacy gate 修复，不必撤销整体接管成果。

本轮只读源代码与历史，复现使用临时 SQLite／anchor 环境，HTTP 仅 GET。代码、原 runtime 状态和旧交付没有修改；新增的只有本日审阅资料，没有发送消息、部署、下载工具或执行外部写入。
