"""The two rerank runners: registered settings, reproduce-before-rerank, guardrail, metrics, audit, decision.

preparation/s2/evaluation/rerank/run_guardrail.py (47 frozen questions, guardrail only) and
preparation/s4/real-reports/rerank/run_rerank.py (real annual reports, retrieval only). No model is loaded
here: the reranker is a scripted or fake object and the vector half is replayed or switched off.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

from forgeflow.evt_store import EVTBoundStore
from forgeflow.pdf_ingest import source_line
from forgeflow.retrieval import CrossEncoderReranker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preparation/s2/evaluation/rerank"))
sys.path.insert(0, str(ROOT / "preparation/s4/real-reports"))
sys.path.insert(0, str(ROOT / "preparation/s4/real-reports/rerank"))
import rerank_support as support  # noqa: E402
import run_guardrail  # noqa: E402
import run_qa as qa  # noqa: E402
import run_rerank  # noqa: E402


def test_both_registrations_state_the_same_reranker_settings_and_the_runners_build_exactly_that():
    settings = support.registered_reranker()
    assert settings == run_guardrail.REGISTERED["reranker"] == run_rerank.REGISTERED["reranker"]
    assert (settings["model"], settings["candidates"], settings["max_length"], settings["score_threshold"]) == (
        "BAAI/bge-reranker-v2-m3", 30, 1024, None)
    reranker = support.build_reranker("cross-encoder", settings, local_files_only=True)
    assert isinstance(reranker, CrossEncoderReranker) and reranker._encoder is None  # nothing is loaded yet
    assert (reranker.model_id, reranker.device, reranker.max_length, reranker.batch_size) == (
        "sentence-transformers/BAAI/bge-reranker-v2-m3", "mps", 1024, 8)
    loaded = {"model_id": reranker.model_id, "device": "mps", "dtype": "float32", "max_length": 1024,
              "max_seq_length": 1024, "batch_size": 8, "activation_fn": "Identity"}
    assert support.settings_mismatch(loaded, settings) == []
    assert support.settings_mismatch(loaded | {"device": "cpu", "activation_fn": "Sigmoid"}, settings) == [
        "device: loaded 'cpu', registered 'mps'", "activation_fn: loaded 'Sigmoid', registered 'Identity'"]


# --------------------------------------------------------------------------- 47 frozen questions (guardrail)


def test_guardrail_holds_only_when_nothing_is_lost_and_nothing_unauthorized_is_read():
    sealed = run_guardrail.load_sealed()  # also checks the registered hash of the sealed hybrid run
    metrics, slices = sealed["metrics"], sealed["slices"]
    assert run_guardrail.guardrail(sealed, metrics, slices, outside_readable=0)["passed_all"]
    assert not run_guardrail.guardrail(sealed, metrics, slices, outside_readable=1)["passed"]["unauthorized_zero"]
    lower = metrics | {"core_content_recall_at_5": metrics["core_content_recall_at_5"] - 1 / 30}
    assert not run_guardrail.guardrail(sealed, lower, slices, 0)["passed"]["recall_not_lower"]
    more = metrics | {"unanswerable_nonempty_context": {"numerator": 2, "denominator": 3, "rate": 2 / 3}}
    assert not run_guardrail.guardrail(sealed, more, slices, 0)["passed"]["unanswerable_not_more"]
    leak = metrics | {"all_query_leak": {"numerator": 1, "denominator": 47, "rate": 1 / 47}}
    verdict = run_guardrail.guardrail(sealed, leak, slices, 0)
    assert not verdict["passed"]["unauthorized_zero"] and not verdict["passed_all"]


def test_guardrail_runner_reproduces_the_sealed_hybrid_run_before_it_reranks(tmp_path):
    output, report = run_guardrail.run("sealed", "fake", "http://127.0.0.1:11434", tmp_path, False)
    assert report["status"] == "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS"  # never mistaken for a real result
    assert report["hybrid_parity_with_sealed_run"] == {
        "against": run_guardrail.REGISTERED["base"]["sealed_run"]["path"], "questions": 47, "differ": [],
        "reproduced": True}
    audit = report["rerank_audit"]
    assert audit["passages"] > 47 and audit["outside_readable"] == 0 and audit["query_altered"] == 0
    assert audit["most_passages_in_one_call"] <= report["reranker_settings"]["candidates"]
    assert report["hybrid_rerank"]["metrics"]["all_query_leak"]["numerator"] == 0
    assert report["benchmark_and_present_s1_unchanged_after"] is True
    assert json.loads((output / "report.json").read_text())["guardrail"] == report["guardrail"]


def test_a_real_guardrail_run_stops_before_the_reranker_when_hybrid_is_not_reproduced(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the reranker must not be built when the baseline is not reproduced")

    monkeypatch.setattr(support, "build_reranker", refuse)
    monkeypatch.setattr(run_guardrail, "OllamaEmbedder", lambda *args, **kwargs: run_guardrail.hybrid_run.FakeEmbedder())
    _, report = run_guardrail.run("ollama", "cross-encoder", "http://127.0.0.1:11434", tmp_path, False)
    assert report["status"] == "BASELINE_NOT_REPRODUCED"
    assert report["hybrid_parity_with_sealed_run"]["differ"] and "hybrid_rerank" not in report


# --------------------------------------------------------------------------- annual reports (retrieval only)

NAMED, PEOPLE = qa.scopes(), qa.principals()
LANGUAGES = {"alpha_2025_en": "en", "beta_2025_zh": "zh"}
QUESTIONS = [
    {"id": "S01", "doc": "alpha_2025_en", "lang": "en", "kind": "answerable", "query": "revenue total", "pages": [7]},
    {"id": "S02", "doc": "beta_2025_zh", "lang": "zh", "kind": "answerable", "query": "贝塔公司 2025 年的营业收入是多少？",
     "pages": [3]},
    {"id": "S03", "doc": "beta_2025_zh", "lang": "en", "kind": "answerable", "query": "What was Beta revenue in 2025?",
     "pages": [3, 4]},
    {"id": "S04", "doc": "alpha_2025_en", "lang": "en", "kind": "absent", "query": "dividend"},
]
SETTINGS = support.registered_reranker()


class NoVector:
    """The vector half switched off, so the hybrid order is the lexical order and can be written down."""

    embedded_texts: list[str] = []

    def __call__(self, query, rows):
        return []


class ScriptedReranker:
    """Prefers the passage that states the figure; for the Beta question, the Chinese passage."""

    model_id = "test/scripted"

    def score(self, query, passages):
        return [10.0 if "was 99" in p or ("Beta" in query and "贝塔公司" in p) else 0.0 for p in passages]


def _row(doc, page, text, prefix=""):
    line = source_line(f"{prefix}{doc}.pdf", "ab" * 32, [{"page": page, "bbox": [1.0, 2.0, 3.0, 4.0]}])
    return f"{prefix}{doc}#p{page}-c1", f"{line}\n{text}"


def _fill(store):
    """Two small reports the analyst may read, plus the two kinds of restricted copy the real run uses."""
    admin, scope = PEOPLE["report-admin"], NAMED["finreports-2025"]
    rows = [_row("alpha_2025_en", page, "revenue revenue total") for page in range(1, 7)]  # six stronger decoys
    rows += [_row("alpha_2025_en", 7, "Total revenue was 99"), _row("alpha_2025_en", 8, "nothing here")]
    rows += [_row("beta_2025_zh", 3, "贝塔公司 2025 营业收入 12"), _row("beta_2025_zh", 9, "贝塔公司 董事会")]
    for identifier, content in rows:
        store.put_document(admin, scope, identifier, "v1", content, {"finance"}, 1, None)
    identifier, content = _row("alpha_2025_en", 7, "Total revenue was 99", prefix="board-only-copy-")
    store.put_document(admin, scope, identifier, "v1", content, {"board"}, 1, None)
    identifier, content = _row("beta_2025_zh", 3, "贝塔公司 2025 营业收入 12", prefix="other-project-copy-")
    store.put_document(admin, NAMED["board-restricted"], identifier, "v1", content, {"finance"}, 1, None)


@pytest.fixture()
def corpus(tmp_path):
    store = EVTBoundStore(f"sqlite:///{tmp_path / 'store.sqlite'}")
    store.create_schema()
    _fill(store)
    yield store
    store.engine.dispose()


def _evaluate(store, *, candidates=30, sealed=None, enforce_parity=False, make_reranker=ScriptedReranker):
    return run_rerank.evaluate(
        store=store, questions=QUESTIONS, extra_questions=qa.PREREG["permission_checks"]["extra_questions"],
        people=PEOPLE, named=NAMED, asker="finance-analyst", corpus_scope="finreports-2025", languages=LANGUAGES,
        vector=NoVector(), rrf_k=60, top_k=5, warmup_query="annual report",
        settings=SETTINGS | {"candidates": candidates}, make_reranker=make_reranker, sealed=sealed,
        expected_page_recall="1/3", enforce_parity=enforce_parity, log=lambda line: None)


def test_annual_report_pass_reports_recall_mrr_ceiling_slices_and_zero_unauthorized(corpus):
    result = _evaluate(corpus)
    assert result["status"] == "COMPLETED"
    hybrid, reranked = result["summary"]["hybrid"], result["summary"]["hybrid_rerank"]
    # Hybrid: S01's gold page is the 7th candidate, S03's the 8th; only S02 has it in the top 5 (at rank 1).
    assert hybrid["page_recall_at_5"]["all"] == {"n": 1, "of": 3, "rate": 0.3333}
    assert hybrid["mrr_at_5"]["all"] == 0.3333 and hybrid["first_gold_rrf_rank"] == {"S01": 7, "S02": 1, "S03": 8}
    assert hybrid["candidate_ceiling"]["all"]["n"] == 3  # all three gold pages are among the first 30
    # After reranking all three are in. For S03 two passages get the same score and keep their hybrid order,
    # so its gold page is second: MRR is (1 + 1 + 1/2) / 3.
    assert reranked["page_recall_at_5"]["all"]["n"] == 3 and reranked["mrr_at_5"]["all"] == 0.8333
    assert {k: (hybrid["page_recall_at_5"][k]["n"], reranked["page_recall_at_5"][k]["n"], v["of"])
            for k, v in reranked["page_recall_at_5"].items()} == {
        "all": (1, 3, 3), "same_language": (1, 2, 2), "cross_language": (0, 1, 1), "zh_on_zh": (1, 1, 1),
        "en_on_en": (0, 1, 1)}
    assert result["changes"]["gained"] == ["S01", "S03"] and result["changes"]["lost"] == []
    # The absent question has no candidate, so the reranker is not called for it; nor for the two extra questions.
    by_id = {o["id"]: o for o in result["questions"]["hybrid_rerank"]}
    assert by_id["S04"]["reranker"]["calls"] == 0 and by_id["S01"]["reranker"]["passages"] == 7
    assert by_id["S01"]["candidates"][6] == {
        "id": "alpha_2025_en#p7-c1", "file": "alpha_2025_en.pdf", "pages": [7], "rrf_rank": 7,
        "rrf_score": round(1 / 67, 6), "rerank_score": 10.0, "gold_page": True}
    permissions = result["permissions"]
    assert permissions["passed"] and permissions["leak_total"] == 0
    assert permissions["hybrid_rerank"]["reranker_passages"] > 0
    for name in ("hybrid", "hybrid_rerank"):
        assert [(e["id"], e["retrieval_error"], e["store_rows_seen"], e["hits"], e["reranker_calls"], e["passed"])
                for e in permissions[name]["extra_questions"]] == [
            ("X01", None, 0, 0, 0, True), ("X02", "NOT_FOUND", 0, 0, 0, True)]


def test_a_gold_page_outside_the_first_n_candidates_cannot_be_recovered_and_is_reported_as_such(corpus):
    result = _evaluate(corpus, candidates=6)
    assert result["summary"]["hybrid"]["candidate_ceiling"]["all"]["n"] == 1
    assert result["summary"]["hybrid_rerank"]["page_recall_at_5"]["all"]["n"] == 1
    assert result["changes"]["gained"] == []
    assert result["changes"]["missed_with_gold_outside_candidates"] == ["S01", "S03"]
    assert all(o["reranker"]["passages"] <= 6 for o in result["questions"]["hybrid_rerank"])


def test_pass_two_never_starts_when_pass_one_differs_from_the_sealed_run(corpus):
    def refuse():
        raise AssertionError("the reranker must not be built when the baseline is not reproduced")

    first = _evaluate(corpus)
    sealed = {key: [{"id": o["id"], "retrieval": {"hits": o["hits"], "error": o["error"],
                                                   "gold_page_in_top5": o.get("gold_page_in_top5")}}
                    for o in first[key]["hybrid"]] for key in ("questions", "extra_questions")}
    same = _evaluate(corpus, sealed=sealed, enforce_parity=True)
    assert same["status"] == "COMPLETED" and same["baseline_parity"]["reproduced"] is True

    moved = copy.deepcopy(sealed)
    hits = moved["questions"][1]["retrieval"]["hits"]
    hits[0], hits[1] = hits[1], hits[0]  # same rows, another order: not the sealed result
    stopped = _evaluate(corpus, sealed=moved, enforce_parity=True, make_reranker=refuse)
    assert stopped["status"] == "BASELINE_NOT_REPRODUCED"
    assert stopped["baseline_parity"]["top5_differ"] == ["S02"] and "hybrid_rerank" not in stopped["summary"]


def test_the_sealed_annual_report_run_is_what_pass_one_is_compared_with():
    sealed = run_rerank.load_sealed()  # also checks the registered hash of the sealed QA run
    expected = run_rerank.REGISTERED["base"]["sealed_run"]["page_recall_at_5"]["all"]
    base = [{"id": q["id"], "kind": q["kind"], "hits": q["retrieval"]["hits"], "error": q["retrieval"]["error"],
             "gold_page_in_top5": q["retrieval"].get("gold_page_in_top5")} for q in sealed["questions"]]
    extra = [{"id": q["id"], "kind": "permission", "hits": q["retrieval"]["hits"], "error": q["retrieval"]["error"]}
             for q in sealed["extra_questions"]]
    parity = run_rerank.baseline_parity(base, extra, sealed, expected)
    assert parity["reproduced"] is True and (parity["questions"], parity["page_recall_at_5"]) == (42, "18/28")

    reordered = copy.deepcopy(base)
    reordered[0]["hits"] = reordered[0]["hits"][::-1]
    assert run_rerank.baseline_parity(reordered, extra, sealed, expected)["top5_differ"] == [base[0]["id"]]
    fewer = copy.deepcopy(base)
    lost = next(o for o in fewer if o["gold_page_in_top5"])
    lost["gold_page_in_top5"] = False
    verdict = run_rerank.baseline_parity(fewer, extra, sealed, expected)
    assert verdict["gold_page_differ"] == [lost["id"]] and verdict["page_recall_at_5"] == "17/28"
    assert verdict["reproduced"] is False


def test_rows_the_asker_may_not_read_are_counted_even_if_they_never_reach_a_result(corpus):
    class LeakyStore:
        """A Store whose filter is broken: it also hands back the rows only the board may read."""

        def __init__(self, store):
            self.store = store

        def document_rows(self, principal, scope):
            rows = self.store.document_rows(principal, scope)
            if principal.subject_id == "finance-analyst":
                return rows + [r for r in self.store.document_rows(PEOPLE["report-admin"], scope) if r not in rows]
            return rows

        def cite(self, *args):
            return self.store.cite(*args)

    result = _evaluate(LeakyStore(corpus))
    permissions = result["permissions"]
    assert permissions["hybrid"]["store_rows_forbidden"] > 0 and permissions["hybrid"]["store_rows_restricted"] > 0
    assert permissions["hybrid_rerank"]["hits_restricted"] == 0  # the second check still kept them out of results
    assert permissions["passed"] is False and permissions["leak_total"] > 0
    summary = result["summary"]
    assert run_rerank.decide(summary["hybrid"], summary["hybrid_rerank"], permissions, None)["adopt"] is False


def _summary(n):
    return {"page_recall_at_5": {"all": {"n": n, "of": 28, "rate": round(n / 28, 4)}}}


@pytest.mark.parametrize(("after", "leaks", "guard", "adopt"), [
    (21, 0, True, True),  # three more questions, nothing unauthorized, guardrail holds
    (20, 0, True, False),  # two more is not enough
    (24, 1, True, False),  # any unauthorized content decides it, whatever the gain
    (24, 0, False, False),  # so does a failed guardrail
    (21, 0, None, None),  # guardrail verdict not given: undecided, which is not adopted
    (20, 0, None, False),
])
def test_decision_rule_needs_three_more_questions_zero_unauthorized_and_the_guardrail(after, leaks, guard, adopt):
    permissions = {"passed": leaks == 0, "leak_total": leaks}
    guardrail = None if guard is None else {"passed_all": guard}
    decision = run_rerank.decide(_summary(18), _summary(after), permissions, guardrail)
    assert decision["adopt"] is adopt
    assert decision["measured"]["gain_questions"] == after - 18
    assert decision["rule"]["min_page_recall_gain_questions"] == 3


def test_annual_report_runner_writes_a_plumbing_report_that_cannot_pass_for_a_real_run(tmp_path, monkeypatch, capsys):
    # The PDFs are not in the repository: input verification and ingestion are replaced, everything else is main().
    monkeypatch.setattr(qa, "verify_inputs", lambda reports: {"pdfs": "replaced in this test"})
    monkeypatch.setattr(qa, "load_questions", lambda: list(QUESTIONS))
    monkeypatch.setattr(qa, "ingest", lambda store, reports, scratch: (_fill(store) or {"documents": []}, LANGUAGES))
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["run_rerank.py", "--reports", str(tmp_path), "--embedder", "fake",
                                      "--reranker", "fake", "--out", str(out)])
    assert run_rerank.main() == 0
    report = json.loads(next(out.glob("rerank-*-fake/report.json")).read_text())
    assert report["status"] == "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS"
    assert report["decision"] is None  # no verdict from anything but the 40 registered questions
    assert report["reranker_settings"] == SETTINGS and report["reranker"] == {"model_id": "fake/trigram-overlap"}
    assert set(report["summary"]) == set(report["questions"]) == {"hybrid", "hybrid_rerank"}
    assert report["baseline_parity"]["reproduced"] is False  # synthetic rows are not the sealed run
    assert report["permissions"]["passed"] is True and report["index"]["texts_sent_to_embedder"] > 0
    assert set(report["latency"]) == {"hybrid_search", "hybrid_rerank_search", "reranker_inference",
                                      "questions_with_n_candidates"}
    printed = capsys.readouterr().out  # one progress line per question, then the brief as JSON
    assert printed.count("[hybrid+rerank] S0") == len(QUESTIONS)
    assert json.loads(printed[printed.index("{"):])["status"] == report["status"]
