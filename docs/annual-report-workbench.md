# 企业年报研究工作台

目标是让分析师交付可复核、可修订的经营报告。研究员确认公司、期间和指标，工具查表计算，审阅人打开原页、退回或批准指定版本。最终交付 HTML、事实与引用清单。首个开发案例是宁德时代经营现金流的全年与季度混淆。Apple FY2025 增速与增量贡献作为跨案例检查。两者均公开开发，盲测另选文档。

## 可续做清单

|能力|验收与证据|进度|
|---|---|---|
|指定期间查数|年报问答真实入口查全年，保留期间、单位、引用；无法证实进入待核|已完成公开开发用例，仍需盲测。[决策与实验](../preparation/annual-report-workbench/DECISION-001.md)|
|两期变化和分部贡献|同一组事实切换分母，记录公式、版本和百分点；Apple 案例交叉检查|指定问题的真实查表、公式和引用已打通。改问“只保留增长贡献”后可以保存新版。[增速与贡献对照](../preparation/annual-report-workbench/DECISION-002.md)|
|可修改的研究任务|研究员中途改变口径，看见受影响的表格、句子和新旧差异；从断点继续|已支持退回意见、再查、增订、可见版本逐项对照、历史版下载与重开恢复。[真实改稿与三种对照方法](../preparation/annual-report-workbench/DECISION-005.md)。多指标整份报告待做|
|可追溯的事实与检索|原格、表头、单位和跨页出处；受权后词法、向量、RRF；按增益试 Docling 与重排|已有页级检索与解析，单元格持久化和回归待做|
|审阅和交付|分离研究员和审阅人，批准绑定稿件摘要，幂等导出和可查回执|本机两种真实角色已接入独立持久库。可下载 [HTML](../preparation/annual-report-workbench/apple-review-v2.html) 和 [JSON](../preparation/annual-report-workbench/apple-review-v2.json)。[审批取舍与证据](../preparation/annual-report-workbench/DECISION-004.md)|
|工作流与性能|复用 FastAPI、队列、Redis、gRPC、SSE，测冷启动、热索引、缓存、P95 和成功任务成本；按需对照 LangGraph、并行工具、多 Agent、OTLP|已有问答服务；整任务尚待验证|
|独立试用|独立研究员在未调试年报上完成比较，与手工 PDF＋表格比较耗时、关键错数和首次签收|待独立试用|

## 三项目交换

ForgeFlow 拥有产品执行、权限、审批、业务状态及交付回执。HorizonBench 拥有环境、故障时序、独立终态核对及原动作回放。EvidenceLoop 拥有数据版本、金标、校准、配对评分和训练。共同交换版本化 case_id、run_id、来源与配置、工具及参数、artifact_refs/digest、业务终态和分项检查、时延及成本。对新字段做兼容性检查；隐藏答案只进入评测端。

证据分别注明真实请求、实际执行的工具、模型调用、脚本、模拟和替身。每个决策记录候选方案、同预算小实验、失败与推荐者。开发者两次查看同一报告不构成独立使用者验证。企业 Agent 应聘可演示交付过程，Harness 可展示恢复和回执，评测和训练成果由负责的项目提供证据。

下一步见表中最靠前的未完成业务能力。重大决策由 Codex 代试验并提出建议，保留用户实际作出的业务判断。

## 本机复现与面试演示

1. 在具有本项目依赖的 Python 环境中启动现有年报工作台。可用 `python -m forgeflow.workbench`，打开 `http://127.0.0.1:8780/workbench/#reports`。需要先构建 `web` 并准备本机登记的年报 PDF。默认独立研究库在仓库的 `.workbench/annual-research/research.sqlite3`；两个入口也可使用同一个绝对目录环境变量 `FORGEFLOW_RESEARCH_DIR`。旧 Fabric 演示库仍有自己的生命周期。
2. 首次用 `python -m forgeflow.research_store author analyst` 和 `python -m forgeflow.research_store reviewer reviewer` 分别登记本机身份。各命令只在终端显示一次新凭证，供对应操作者保存。私有目录由程序创建。研究员在“研究稿与审阅”登录，查询 Apple FY2025 服务收入增速和增长贡献，保存说明并指派审阅人。匿名查表须在登录后重新运行才能入稿。
3. 在另一个浏览器窗口用审阅人凭证登录，打开已送审稿，点第 26、39 页原表，写明要保留的分母并退回。研究员刷新，重新询问 Apple FY2025 服务收入增长贡献，保存新版后再次送审。审阅人批准，研究员登记本地交付，下载证据清单与阅读稿。刷新后重新登录可找回每个版本及回执。
4. 公开开发样例可用 `PYTHONPATH=src python scripts/research_review_dev.py ~/code/finreports /tmp/forgeflow-review-evidence` 复跑。默认重新解析真实 Apple PDF，也可用 `--cache` 指向已有解析缓存。交付物含输入、原格引用、来源 PDF 指纹、工具及索引版本、查询和生产 run、作者、审阅人、前版摘要和本机回执。同步来源暂存最近 128 次已认证查表结果，入稿许可有效一小时，过期可重新查表。已保存版本独立于检索缓存。

查看修订时，在研究稿详情点击“与第 … 版对照”。表格先列出移除、变更及保留的指标，再列出有变化的文字、原格或来源。基准是当前账号可见的最近一次较早版本；作者两次送审之间的私稿只对作者可见。

**证据边界。** 数值工具运行于真实 PDF；公开案例由开发脚本控制两个本机角色。配对评测及独立终态判分分别由 EvidenceLoop 和 HorizonBench 承担。交付回执记录本机登记，第三方签收另行确认。常规问答仍支持原有本地模型，本轮两个结构化年报任务实际模型调用均为零。

### 固定开发归档怎样读取

从同一 Git 提交取 [任务与动作记录](../preparation/annual-report-workbench/review-dev.json)、[被退回的第一版](../preparation/annual-report-workbench/apple-review-v1.json)、[已交付的第二版](../preparation/annual-report-workbench/apple-review-v2.json) 和对应 HTML。入口文件的 `artifact_refs` 包含原始字节的 SHA-256、大小和文件名。`draft_id` 标识整份稿件，`versions[].run_id` 标识一次实际查表。第二版的 `parent_digest` 指向第一版的 `digest`，`approved` 和 `delivered` 两个事件都绑定第二版摘要。`local_receipt` 取交付事件规范化 JSON 的 SHA-256。规范化采用 UTF-8、键名排序、保留 Unicode 和无空格分隔符。固定提交和文件清单可用来核对传输完整性。

HorizonBench 可按 `calls[].seq` 重放动作，将开发轨迹里的稿号、版本摘要和来源 run 映射到新环境生成的对应值，再独立核对终态。EvidenceLoop 可读取 `payload.evidence` 的问题、两期金额、公式、原页与文件指纹，用独立原表核对。样例中认证后的研究库和工具真实执行，`dev-author` 和 `dev-reviewer` 是脚本操作者。[界面实测图](../preparation/annual-report-workbench/review-ui.png) 来自另一次 Chrome 操作，该次改写说明、保留两个比例，故截图与固定归档各有自己的稿号与回执。
