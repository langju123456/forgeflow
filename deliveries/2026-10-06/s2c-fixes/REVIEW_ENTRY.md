# ForgeFlow 当前原型：审阅入口

唯一可开发原件：`/path/to/forgeflow`。当前可打开 `http://127.0.0.1:8771/docs`。用户已批准继续原型开发，并要求省去全新解压测试与额外防御代码；本次不提供新ZIP。

请按以下路径审阅本地实现：

1. 本目录 `DELIVERY.md`：真实状态、模型／服务器职责、现成结果ID及手动读取命令。
2. `evidence/regrade-01/summary.json` 和 `review-freeze.json`：完整12案例／13场景重评；13PASS，5实际模型场景／8TEST_DRIVER，20原调用／0重评新增。
3. `evidence/experiment-01`：未覆盖的原始UTF-8、actual messages/schema、raw journal、真实checker／MCP工件及首次错误grader的8PASS／5FAIL。
4. `evidence/frozen-grader-binding-investigation.json`、`evidence/independent-review/regrade-independent-proof.json`：展平／嵌套哈希契约缺口与独立修正。
5. `evidence/live-persisted-replay-validation.json`、`manual-read-demo.json`：真实PG／HTTP／CLI读取与同key重放，0新增模型调用。
6. `docs/13-s2c-state-and-derived-check.md`、`src/forgeflow/s2c_*.py`、`scripts/s2c_regrade.py`、对应 `tests/test_s2c_*.py`：当前模块与契约。
7. `docs/02-contracts.md`、`03-threat-model.md`、`05-reuse-and-sources.md`、`06-execution-plan.md`、`THIRD_PARTY.md`：底座、复用出处许可证及后续阶段。

以上未带前缀的src/docs/scripts路径相对唯一工程根目录，evidence路径相对本日交付。当前全阶段测试243通过；此前完整默认653通过／13live skip；冻结完整性另62通过。无需重复解压或扩护栏，优先检查行为、读已有证据和推进有实际贡献的下一模块。

M03真实派生checker Gate UNKNOWN；M04模型选择concern，服务器技术分析COMPLETE。模型输出没有status；完整技术状态由服务器推导。项目仍PROTOTYPE、待人工批准、业务receipt为0。只验证本机模型与合成资料，尚非完整五角色／全NPI／生产身份／混合向量检索或真实企业外部集成。四份历史seal和此前失败全部保留。
