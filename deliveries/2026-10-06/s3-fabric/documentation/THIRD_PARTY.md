# 来源与许可证记录

ForgeFlow 第一阶段代码为本任务独立实现。现有本机工程仅作模式盘点，未复制其代码；其许可证未知状态保留在 `05-reuse-and-sources.md`。

- 官方 MCP Python SDK：安装版本 1.30.0，MIT；[官方仓库](https://github.com/modelcontextprotocol/python-sdk)。运行证据记录实际协商版本，不按设计建议声称 v2。
- Semgrep CE：扫描镜像固定版本 1.179.0；[官方许可证](https://github.com/semgrep/semgrep/blob/develop/LICENSE)，安装版本许可证保留在 `scanner/vendor/semgrep-1.179.0-LICENSE`。本项目规则为独立编写的有限辅助规则，不复制商业规则集。
- Python/FastAPI/SQLAlchemy/psycopg/Pydantic/PyYAML 等依赖版本在 `requirements.lock`；扫描镜像另有版本与 immutable image ID。镜像依赖尚无完整逐包 hash lock，不宣称完整供应链复现。
- 参考 OpenHands software-agent-sdk/docker-sbx-kit、AgentSkills、LangMem、pgvector 等官方资料见 `05-reuse-and-sources.md`。这些参考不等于已安装、已集成或已复用其全部能力。

尚未公开发布源代码或镜像，也未决定新项目的对外代码许可证。
