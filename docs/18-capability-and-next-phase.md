# 当前能力与后续阶段

| 能力 | 已实现、实际证据 | 模拟／待接入 |
| --- | --- | --- |
| Supervisor／PM | 原 S2C 同父 run 的本地 Qwen3:8b grounded 分析；5 个实验、20 次 DELIVERED＋VALIDATED 调用 | 新 Fabric 闭环为确定性，不声称本批重新调用 LLM；其他职责未接 LLM |
| Runtime | FastAPI、PostgreSQL runs／steps／approvals／events／artifacts、lease／epoch、恢复和跨重试幂等 | DEV identity；生产 SSO、分布式运维尚未完成 |
| Sandbox | 真实 Docker 执行，非 root、只读 rootfs、临时 workspace、CPU／RAM／PID／timeout／kill、network none、清理证据；不挂 socket／凭据 | 固定合成 checker；非硬件／APK build，非 VM 强隔离证明 |
| MCP／Skills | 官方 Python MCP SDK stdio 握手与 sandbox.execute；动态本地 Skill 加载、职责工具白名单 | Stage／Issue／Memory 是 IN_PROCESS；没有假称 MCP；无多角色 LLM／A2A |
| Knowledge | scope／角色／classification 在检索前检查；独立引用、来源版本、有效期与冲突状态 | 词法检索；pgvector／混合检索尚未实现，不需要先购买 embedding API |
| Memory | 受控候选写入、事件来源；新组合证据及独立审批引用、有效期过滤和撤销 | 本地 TEST_DRIVER 决定；无 LangMem 后台自主学习、无真实业务确认 |
| Hardware／Architect／Android／SRE | 合成硬件阈值；实际 XML／BOM／interface／exposure 解析；真实保存的执行、lease、MCP、scan、cleanup 审核 | hardware 规则非认证；Android source 静态检查，APK／设备 NOT_RUN |
| Stage Gate | 六项默认 EVT gate；UNKNOWN 拒绝；source-only 接受和完整 EVT 区分；新本地 TEST_STAGE 流程 | 新模拟项目的 flow PASS 不授予 EVT；真实 EVT 仍 UNKNOWN |
| Stage／Issue／Memory 动作 | 新独立项目三项批准和事务效果、三项批准补偿、跨进程回放／中断恢复 | 本地数据库 Issue；原 Aurora 旧执行器未迁移，当前 HTTP 阻断旧硬件批准；无远程写入 |
| NPI／DevOps／办公 | 分阶段设计与当前 EVT 请求链可审阅运行 | 立项／Prototype／DVT／PVT 的完整业务 SOP、GitHub/Jira/办公适配未完成；没有空壳角色冒充生产 |

首阶段与后续已交付计划、模块契约、威胁模型、验收矩阵、复用盘点在 `docs/01` 至 `docs/09`；模型与各职责增量在 `docs/10` 至 `docs/17`。所有源码只有一份，逐日交付保留文档和证据，不再生成新 审阅 ZIP。

下一阶段先补真实 Android 证据，同时把其输出作为新技术来源绑定进 Gate，不使用静态源码 PASS 顶替 APK／设备。再按真实业务需要扩展 EVT 动作策略、其他 NPI stage 与职责。之后才考虑 pgvector、更多模型角色、远程 DevOps／办公 outbox／reconciliation；没有必要先增加 GPU 或付费沙箱。

**Android 的具体授权缺口是新软件下载／网络依赖解析和设备使用**，不是还没完成的本地 Stage／Issue／Memory 工程。此前仅核官方元数据，没有下载。可选构建组合与已核体积：

| 工具 | 固定版本／来源 | 下载体积 |
| --- | --- | --- |
| Temurin ARM64 JDK | 17.0.20.1+1；[Adoptium 官方元数据](https://api.adoptium.net/v3/assets/latest/17/hotspot) | 185,851,019 bytes，约 185.85 MB |
| Android Command-line Tools Mac ARM64 | 23.0／16111833；[Google registry](https://dl.google.com/android/repository/repository2-3.xml) | 155,384,151 bytes，约 155.38 MB |
| SDK Platform | android-34 rev3；同一 Google registry | 63,180,081 bytes，约 63.18 MB |
| Build Tools Mac | 36.0.0；同一 Google registry | 79,121,749 bytes，约 79.12 MB |
| Gradle binary | 9.6.0；[官方分发目录](https://services.gradle.org/distributions/) | 官方标 134.16M，未把此单位改写成精确字节 |
| Android Gradle Plugin | 9.4.0；[官方兼容表](https://developer.android.com/build/releases/agp-9-4-0-release-notes) | 本体及传递 Maven 依赖总量待固定依赖树后统计 |

前四个精确包合计 **483,537,000 bytes**；这不是总下载量，还需 Gradle、AGP 与传递依赖。目标仍为合成 API34 Java 小夹具，版本值属于项目示例。AGP9.4 官方矩阵对应 JDK17、Gradle9.6、BuildTools36；未声称这组合已构建成功。对应 AAPT2／zipalign 的 ARM 原生能力与 Rosetta 是否需要仍待具体包核验；安装计划不等于已解决 Mac builder 与 Linux Docker 的运行边界。

工具无需付费订阅、云机或 GPU；不安装 Android Studio／emulator／NDK。拟放在任务 `.runtime/android-toolchain`，环境只传给任务子进程，不改全局 shell。M4／24GB 小构建拟限制 JVM heap 2GiB、2 workers、暂停并行 LLM；这不是已测 RSS。拟定总下载预算 ≤2GB、工具／缓存磁盘预算 ≤8GB，解压／缓存实际峰值未知，超预算前停下报告。需要 Rosetta 或系统级安装时先报告具体操作。

设备路线额外需要 [Platform Tools37.0.1 Mac](https://developer.android.com/tools/releases/platform-tools)，**16,110,554 bytes**（约16.11MB），以及用户自己的 Android 手机、USB 和明确的调试使用授权；不自动连接设备。Mac 不需要另装 USB 驱动，见 [Google device 指南](https://developer.android.com/studio/run/device)。APK 构建成功不等于设备检查成功。

尚未授权也未执行：外部 Issue／群消息写入、私人凭据使用、部署、云机、付费 API、设备操作、新网络安全设置。对应实际接入应先给出目标仓库／项目、精确 action 参数、审批主体和资源变化。无需新的模型提供商：已有授权本地 Qwen 可以支撑后续增量；其调用、错误与确定性安全判定仍须分别记录。
