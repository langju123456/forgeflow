"""CrossEncoderReranker against the installed sentence-transformers, with a tiny random-weight model built here.

Skipped without the optional extra (`pip install 'forgeflow[rerank]'`). No download and no real model: a
two-layer BERT with random weights and a character vocabulary is written to a temporary folder. What is
checked is the wrapper's contract with the library: nothing is loaded before the first call, scores are the
model's raw logits, and a pair longer than max_length is truncated instead of rejected.
"""

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("sentence_transformers")
transformers = pytest.importorskip("transformers")

from forgeflow.retrieval import CrossEncoderReranker, RerankRanker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preparation/s2/evaluation/rerank"))
import rerank_support as support  # noqa: E402

MAX_LENGTH = 64
QUERY = "What was revenue in 2025?"
PASSAGES = [
    "[source] a.pdf | p.6 [1.0,2.0,3.0,4.0] | sha256 0123456789abcdef\nRevenue was 12 in 2025.",
    "Nothing about it.",
    "x " * 400,  # far longer than MAX_LENGTH tokens
    "营业收入 168",
]


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory):
    folder = tmp_path_factory.mktemp("tiny-cross-encoder")
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"]
    vocab += [chr(code) for code in range(32, 127)] + ["##" + chr(code) for code in range(32, 127)] + list("营业收入")
    (folder / "vocab.txt").write_text("\n".join(vocab) + "\n", encoding="utf-8")
    tokenizer = transformers.BertTokenizerFast(vocab_file=str(folder / "vocab.txt"), do_lower_case=False)
    torch.manual_seed(0)
    config = transformers.BertConfig(vocab_size=len(vocab), hidden_size=32, num_hidden_layers=2,
                                     num_attention_heads=2, intermediate_size=64,
                                     max_position_embeddings=MAX_LENGTH, num_labels=1)
    model = transformers.BertForSequenceClassification(config).eval()
    model.save_pretrained(folder)
    tokenizer.save_pretrained(folder)
    return folder, tokenizer, model


def test_scores_are_the_raw_logits_and_long_pairs_are_truncated_not_rejected(tiny_model):
    folder, tokenizer, model = tiny_model
    reranker = CrossEncoderReranker(str(folder), device="cpu", max_length=MAX_LENGTH, batch_size=2,
                                    local_files_only=True)
    assert reranker._encoder is None  # constructing it loads nothing
    scores = reranker.score(QUERY, PASSAGES)
    assert len(scores) == len(PASSAGES) and all(type(score) is float for score in scores)
    with torch.inference_mode():
        for passage, score in zip(PASSAGES, scores, strict=True):
            encoded = tokenizer(QUERY, passage, truncation="longest_first", max_length=MAX_LENGTH, return_tensors="pt")
            assert score == pytest.approx(float(model(**encoded).logits[0, 0]), abs=1e-4)  # no sigmoid applied
    assert reranker.score(QUERY, []) == [] and len(reranker.score(QUERY, PASSAGES[:1])) == 1

    detail = support.reranker_detail(reranker)
    assert (detail["device"], detail["dtype"], detail["max_seq_length"], detail["activation_fn"]) == (
        "cpu", "float32", MAX_LENGTH, "Identity")
    timed = support.TimedReranker(reranker)
    timed.score(QUERY, PASSAGES)
    cut = support.truncation(reranker, timed.calls)
    assert (cut["pairs"], cut["over_max_length"], cut["max_length"]) == (4, 1, MAX_LENGTH) and cut["max_tokens"] > 400


def test_rerank_ranker_orders_the_first_candidates_by_the_model_and_leaves_the_rest(tiny_model):
    folder, _, _ = tiny_model
    reranker = CrossEncoderReranker(str(folder), device="cpu", max_length=MAX_LENGTH, local_files_only=True)
    rows = [{"id": f"r{i}", "version": "1", "content": passage, "source_hash": f"h{i}"}
            for i, passage in enumerate(PASSAGES)]

    def base(query, candidates):
        return [(1.0 / (rank + 1), row) for rank, row in enumerate(candidates)]

    ranked = RerankRanker(base, reranker, candidates=3)(QUERY, rows)
    scores = reranker.score(QUERY, PASSAGES[:3])
    expected = [f"r{i}" for i in sorted(range(3), key=lambda i: (-scores[i], i))] + ["r3"]
    assert [row["id"] for _, row in ranked] == expected
