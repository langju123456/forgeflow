# S2 最小模型与 embedding 决策

2026-10-05：仅官方资料、静态本机条件及 read-only Ollama metadata 核验。没有 LLM/embedding inference、模型下载、付费 API、凭据配置或 daemon/network 设置变更。S1 封存文件未改。

## 推荐起点

**优先复用已安装的本地 `qwen3:8b`，后续经批准仅补 `qwen3-embedding:0.6b`。** 初次只接入有限的结构化计划与角色报告，仍用 S1 固定模板、确定性 Gate 和人类批准动作。不要同时引入多个模型供应商、reranker、训练或大型模型。

实际本机为 Apple M4、24GB 内存、约143.5GiB 可用磁盘，Ollama 0.32.1 在 `http://127.0.0.1:11434` 提供 read-only version/tags。已安装 `qwen3:8b`，size=5,225,388,164 bytes，digest=`500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41`；没有 embedding 模型。详见 [machine-readiness.json](machine-readiness.json)，不保存序列号或硬件 UUID。

本机资源及模型文件大小支持“可以做小并发试运行”的工程推断，**不证明有足够实时空闲内存、Metal 加速、延迟、长上下文或工具调用质量**。不停止现有工程来腾资源，不另购 GPU。候选 provider 上限为并发1、context8192、单次输出1024、per-call60s/run180s；首个 analysis 应用更收紧为每call输入3500/输出800、最多4次生成。均是拟议 budget；总剩余 deadline 不够时停止，并不承诺4×60s都能执行。授权后由首批 conformance 与峰值内存测量决定是否调整。

| 选择 | 具体组合 | 前置缺口 |
|---|---|---|
| **A：本地优先，推荐** | LLM `qwen3:8b` 固定上述现有 digest；embedding `qwen3-embedding:0.6b`，拟用1024维 | 同意本项目调用现有 local endpoint；同意下载 embedding 约639MB（如选择先LLM，可单独延后）；首次 /api/show、schema/tools/embedding shape 与 budget conformance 尚未运行 |
| B：付费 API 最小组合 | OpenAI `gpt-5.6-terra` + `text-embedding-3-small`（默认1536维，首次实得维度验证）；或 local embedding 单独保留 | 具体 API 项目/访问权、secret 注入方式、总预算/每run上限、允许发出的 SYNTHETIC 文本；没有已核验 API entitlement，不将聊天订阅当成本项目 API 额度 |

本地模型不是“能力不够时自动升级云端”的许可；任何 fallback 都必须显式选择。已有 qwen2:7b 只作为 metadata inventory，不自动代替已选模型。当前 global Ollama cloud 配置没有核实或修改；应用只允许登记的 local model+digest，工具由 ForgeFlow 执行，不使用 Ollama web/cloud tools。需要独立 local-only server 或修改全局设置时，先报告具体变更；不重启别人的服务。

## 官方核验与选择理由

Ollama 库页列出 `qwen3:8b` 为 Q4_K_M、5.2GB，prefix digest 与本机吻合；上游 Qwen3-8B 标记 Apache-2.0。官方 tool-calling 示例使用 qwen3，/api/chat 支持 tools 和 JSON schema format。这些是选型依据，不能代替对已安装版本/模型的实际验收。[Ollama 模型](https://ollama.com/library/qwen3:8b)、[Qwen 官方模型卡](https://huggingface.co/Qwen/Qwen3-8B)、[tool calling](https://docs.ollama.com/capabilities/tool-calling)、[/api/chat](https://docs.ollama.com/api/chat)

Ollama `qwen3-embedding:0.6b` 约639MB、Q8_0；上游模型卡标记 Apache-2.0、多语言、最大1024维。Ollama /api/embed 支持 dimensions，并默认 truncate=true；ForgeFlow 必须使用 truncate=false，显式分块和长度校验，避免来源文本被悄悄截断。query instruction、dimension、normalization 与模型 digest 一起锁进 index generation；不能把 Ollama 量化分发与原始 HF 权重当作完全相同二进制。[库页](https://ollama.com/library/qwen3-embedding:0.6b)、[Qwen 官方 embedding 卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[/api/embed](https://docs.ollama.com/api/embed)

API 候选采用当前官方仍列为 default 的 GPT-5.6 Terra，支持 function calling/structured outputs，standard text 价格为输入$2、输出$12/百万tokens；text-embedding-3-small 为$0.02/百万tokens。此次核查发现 gpt-5-mini 页面已标 Deprecated，因此不推荐新接入沿用旧 snapshot。官方 Terra 当前页仅列 alias，未提供可锁的独立日期 snapshot；记录实得 model/provider fingerprint、prompt/schema hash 与 eval版本，不虚构 snapshot。[Terra 模型与价格](https://developers.openai.com/api/docs/models/gpt-5.6-terra)、[embedding模型与价格](https://developers.openai.com/api/docs/models/text-embedding-3-small)、[默认1536维与维度参数](https://developers.openai.com/api/docs/guides/embeddings)

以上模型价格是本日公开价格，账号可用性/地区/配额尚未验证。先仅允许 synthetic 数据，不把企业 Memory、个人聊天或旧项目资料发到第三方。schema-valid output 仍可错误、拒答、截断或没有证据；解析后继续校验来源、权限、budget 与 deterministic Gate。[structured output 边界](https://developers.openai.com/api/docs/guides/structured-outputs)、[API data controls](https://developers.openai.com/api/docs/guides/your-data)

## 费用和性能，不虚造 benchmark

若选择 API，建议初始批准上限 **总计$5、每run$0.10**，仅 synthetic；这只是建议，不是已批准预算。按100个run、每run合计8,000 uncached input与2,000 billed output tokens推算，Terra token费用约$4；另200,000 embedding tokens约$0.004。这是算术估计，不保证100次实际能落在$5：reasoning/output、重试、失去回包的可能调用与限额都会影响次数。dispatcher需预留最坏允许费用，未知调用不立即释放额度，不靠账单延迟当实时硬限额。不使用额外计费的 hosted tools。

本地没有这类 API token账单，但仍有时间、内存、电量与服务占用。先测cold/warm、token/s、P50/P95、schema/citation有效性和真正role贡献；不把模型卡公开评分当 ForgeFlow benchmark。不自动升级 Ollama、下载大模型、改网络暴露或多并发。

## 用户只需决定的最小项

1. **本地 A 或付费 B。** A 可以先调用已有 qwen3:8b，再单独批准639MB embedding；B 指定允许的总/单run费用及 API 项目。
2. **数据范围。** 建议首批仅本项目 synthetic eval语料；不涉及私人会话和旧项目客户资料。
3. **接入授权。** A 明确本项目可调用哪个loopback endpoint、是否可下载embedding；B 使用专门授权secret注入控制面，用户不向模型上下文粘贴key。需要设置改变时列明后再执行。

决策后顺序：只读capability/preflight → 单role conformance → 固定模型/schema/prompt trace → 使用S1已核硬件事实的真实Supervisor+PM只读模型链（hardware仍确定性） → 有任务需要且source ACL/profile审查通过时增加SRE → embedding/vector/keyword/RRF对比原始基线。PM产生gap/Issue草案，不自动改变业务动作。generated code、外部写入、完整五role/NPI与production都不是这次选择自动授权的内容。
