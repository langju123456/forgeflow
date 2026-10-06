# 复用盘点与官方核验

只读核验：2026-10-05。未运行其他项目的 tests/worker，未读取 `.env`/Keychain/私钥/受限会话；源码存在不等于 runtime 验收。没有复制任何第三方实现代码。

## 本机已证据化复用

公开版说明：本节原为对作者其他本地私有项目的只读复用盘点，含本机路径、仓库地址、提交号和源码哈希，公开时已删除；原节之后对开发环境 Agent 指示文件的只读检查记录也一并删除。结论保留如下：参考过其他项目里的幂等键与租约认领、事件追加日志、词法检索排名与来源哈希、任务与回执契约、Skill 目录组织等做法，只借鉴思路；其中主要项目没有许可证文件，ForgeFlow 没有复制这些项目的代码，也没有连接它们的数据库、端口或凭据。

## 官方内容核验（网页现状，不是本项目实现）

| ID | 官方来源 | 核验事实与采用边界 |
|---|---|---|
| O1 | [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) | README明确Python/TS/REST及local/ephemeral workspace、tools/skills/events例；MIT。参考边界，不假定其替ForgeFlow实现tenant ACL、NPI审批或outbox。未安装/调用SDK。 |
| O2 | [OpenHands docker-sbx-kit README](https://github.com/OpenHands/docker-sbx-kit/blob/main/README.md) / [repo](https://github.com/OpenHands/docker-sbx-kit) | 声明proxy-managed credentials与egress allowlist，repo显示MIT。其Agent Canvas/port8000/proxy模式不是本项目已验的no-network Docker runtime；不照跑secret/login/headless示例。 |
| O3 | [Docker Engine run](https://docs.docker.com/engine/containers/run/) / [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) | Engine容器约束与sbx产品需区分。sbx文档描述microVM/local/cloud及网络策略；没有用Engine版本推断sbx已安装。仅借鉴Provider设计。 |
| O4 | [MCP latest](https://modelcontextprotocol.io/specification/latest) / [2025 lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) / [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) | latest本轮重定向2026-07-28且描述stateless/per-request协商；2025版本有initialize与initialized。协议提供互通，业务authorization仍由host/server实施。版本页部分访问失败，不假补细节。 |
| O5 | [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) / [SDK docs](https://py.sdk.modelcontextprotocol.io/) / [protocol versions](https://py.sdk.modelcontextprotocol.io/protocol-versions/) / [legacy serving](https://py.sdk.modelcontextprotocol.io/run/legacy-clients/) | 当前docs为stable v2，例为MCPServer，repo MIT。官方明确auto探测server/discover、legacy强制initialize；version pin不产生协商流量。建议v2+legacy满足真实握手另验modern，具体包release需安装时锁定。 |
| O6 | [AgentSkills spec](https://agentskills.io/specification) | 必需name/description frontmatter，按需加载scripts/references/assets；allowed-tools实验字段不能代替server的强制授权。自写SOP需合成来源与hash，不自动执行脚本。 |
| O7 | [LangMem](https://langchain-ai.github.io/langmem/) / [license](https://github.com/langchain-ai/langmem/blob/main/LICENSE) | 提供hot-path/background memory primitives；MIT；示例InMemoryStore重启会丢失，文档要求persistent store用于持久场景。参考候选/合并生命周期，不默认获得企业ACL/conflict政策。 |
| O8 | [Semgrep analysis scope](https://docs.semgrep.dev/semgrep-code/semgrep-pro-engine-intro) / [repo LICENSE](https://github.com/semgrep/semgrep/blob/develop/LICENSE) | 官方明确CE intraprocedural、cross-file需另engine。LICENSE包含LGPL2.1；最终scanner artifact与rules分开核许可。使用本地固定规则和隔离扫描，不靠扫描作为安全证明。 |
| O9 | [gVisor install](https://gvisor.dev/docs/user_guide/install/) | Linux5.6+、amd64/arm64；未安装runsc，Linux provider/daemon修改待批准。Mac Docker info不证明runsc可用。 |
| O10 | [pgvector](https://github.com/pgvector/pgvector) / [LICENSE](https://github.com/pgvector/pgvector/blob/master/LICENSE) | 官方说明ANN scan后过滤可少返回结果，共享tenant ANN会影响recall/speed；tenant partition或separate tables建议。初期authorized候选exact vector，后续有基线才启ANN。许可notice按选定artifact保留。 |
| O11 | [Docker logging](https://docs.docker.com/engine/logging/configure/) / [json-file](https://docs.docker.com/engine/logging/drivers/json-file/) | 独立reviewer核验：默认日志不等于应用输出截断，必须每容器限制。本设计none+bounded attach，尚需provider实测，不改daemon全局配置。 |
| O12 | [Docker tmpfs](https://docs.docker.com/engine/storage/tmpfs/) | 独立reviewer核验：tmpfs文件在容器stop后丢失。设计修为freeze/export-before-stop，已停止时artifact标UNAVAILABLE；尚非运行结果。 |

原文关于MCP reference servers过去path/argument漏洞的具体叙述本轮未完成逐advisory核实，不作为事实引用或选型依据；威胁模型基于接口攻击面本身。E2B/Mem0/Dify/WorkBuddy/Coze只是原方案可选参考，不依赖其未经核验功能，不声称已二开。没有购买、安装或取得外部凭据。

## 本机可行性事实

`pwd`确认新workspace；`git rev-parse --show-toplevel`返回not a git repository。Python路径 `/opt/homebrew/bin/python3` 指向3.14.6，uv指向0.11.29；Docker CLI存在。`command -v sbx/runsc/semgrep`无输出（没有证明安装）。依赖探测：FastAPI/Pydantic/pytest/httpx/yaml/uvicorn/mcp均False。

默认sandbox读Docker socket权限受限；经只读检查批准后，`docker info --format '{{.ServerVersion}}'` exit0，返回29.6.1；image列表有python:3.12-slim（f77ac9e44ae9）、postgres:16-alpine（721873c34ceb）。没有pull、build、run、daemon配置或资源清理。Artifact dependency loader返回“only supported for local threads”，没有据此假定bundled库可用。

这是环境准备发现，不是ForgeFlow test pass；实际SDK依赖/Python版本兼容、pgvector镜像、model endpoint、scanner和Docker约束均需实施阶段验证。
