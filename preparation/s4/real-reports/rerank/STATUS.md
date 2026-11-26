# 重排评测：进度

按步记录，每步做完更新一次。参数和采用规则以 [PREREGISTRATION.md](PREREGISTRATION.md) 和 [合成 47 题那边的登记](../../../s2/evaluation/rerank/PREREGISTRATION.md) 为准，本文件只记做了什么、结果是什么。

## 第一步：`RerankRanker.scored_texts` 只保留最近一次调用（完成，2026-11-26）

- 提交 `52cea48`：`scored_texts` 原来每次调用都往后追加，同一个 ranker 用于多次检索时会一直变长。现在每次调用开头清空，再换成这次送进重排模型的文字；这次没有调用重排模型（没有候选、没有可读行、或 `reranker=None`）就是空列表。
- 测试：新增 `test_scored_texts_hold_only_what_the_most_recent_call_sent`（连续三次调用，后一次替换前一次，最后一次没有候选时为空）；在旧实现上运行确认失败，新实现上通过。`tests/test_retrieval.py` 12/12 通过。
- 整套测试：1209 通过，31 跳过，3 失败。3 个失败都是 `ModuleNotFoundError: grpc / google.protobuf`（`tests/test_report_plugin.py` 两个、`tests/test_research_review.py` 一个），本机这个环境只装了 `.[dev,pdf,rerank]`，没装 `service` 额外依赖，与这次改动无关。
- 运行脚本里用到的审计（`TimedReranker.calls`）不读 `scored_texts`，这次改动不影响重排运行的计数。
- 另一处提交 `f91b781`：`scripts/mac_rerank_eval.sh` 提交运行结果时原来写死了提交者姓名，现在改为用仓库已配置的身份。不涉及运行内容和核对逻辑。
