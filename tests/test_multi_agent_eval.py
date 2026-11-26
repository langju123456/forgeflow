"""The four-arm comparison (preparation/s6/multi-agent): the runner and the held-out question tooling.

Everything runs on the synthetic PDFs from scripts/make_financial_pdfs.py with stand-in models; no real report,
no real model. What is checked is the plumbing the registration relies on: one model call per distinct prompt,
calls counted per arm, the adoption rule, resuming, and that held-out questions cannot be run before a person
has checked them.
"""

import copy
import hashlib
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

ROOT = Path(__file__).resolve().parents[1]
S6 = ROOT / "preparation/s6/multi-agent"
sys.path.insert(0, str(S6))
import run_arms  # noqa: E402

from forgeflow import report_orchestration as ro  # noqa: E402
from forgeflow.analysis_provider import ProviderInvalid  # noqa: E402
from forgeflow.contracts import ContextHit  # noqa: E402

s4 = run_arms.s4
_spec = importlib.util.spec_from_file_location("s6_write_heldout", S6 / "write_heldout.py")
wh = importlib.util.module_from_spec(_spec)
sys.modules["s6_write_heldout"] = wh
_spec.loader.exec_module(wh)

ZH, EN, Q3, HK = "cc_annual_report_2025_zh", "cc_form10k_2025_en", "cc_q3_2025_report_zh", "cc_interim_2025_hk_en"
PER_REPORT = {"answerable": 2, "answerable_cross_language": 1, "absent": 1}
EXPECTED = {"count": 12, "answerable": 8, "absent": 4, "answerable_cross_language": 4, "per_report": PER_REPORT}
A, N = wh.A, wh.N
CANDIDATES = (
    A("S05", ZH, "zh", "澄川智造 2025 年度的预计负债是多少？", [r"^预计负债 {note}{amount}"]),  # not in the report
    A("S01", ZH, "zh", "澄川智造 2025 年度的研发费用是多少？", [r"^研发费用 {note}{amount}"]),
    A("S02", ZH, "zh", "澄川智造 2025 年末的货币资金是多少？", [r"^货币资金 {note}{amount}"]),
    A("S03", ZH, "en", "What was Chengchuan's income tax expense for 2025?", [r"^(?:减:)?所得税费用 {note}{amount}"]),
    N("S04", ZH, "zh", "澄川智造 2025 年在欧洲的营业收入是多少？", ["欧洲", "营业收入"], "只有合计"),
    N("S06", ZH, "zh", "澄川智造 2025 年末有多少名员工？", ["员工", "职工"], "没有人数"),
    A("S13", EN, "en", "What was Chengchuan's total revenue for fiscal 2025, in millions of dollars?",
      [r"Total revenue for fiscal 2025 was \$ ?{decimal} million"], lines=2),
    A("S11", EN, "en", "What was Chengchuan's product revenue in fiscal 2025?", [r"^Product revenue {usd}{amount}"]),
    A("S12", EN, "zh", "澄川智造 2025 财年的净利润是多少？", [r"^Net income {usd}{amount}"]),
    N("S14", EN, "en", "How many employees did Chengchuan have in fiscal 2025?", ["employees", "headcount"], "not stated"),
    A("S21", Q3, "zh", "澄川智造 2025 年销售商品、提供劳务收到的现金是多少？",
      [r"^销售商品、提供劳务收到的现金 {amount}"], unit="元"),
    A("S22", Q3, "en", "What was Chengchuan's weighted average return on net assets in 2025?",
      [r"^加权平均净资产收益率 {decimal}%"]),
    N("S23", Q3, "zh", "澄川智造 2025 年的研发人员数量是多少？", ["研发人员"], "没有人数"),
    A("S31", HK, "en", "What interim dividend per share did Chengchuan declare for the first half of 2025?",
      [r"interim dividend of RMB ?{decimal} per share"], lines=2),
    A("S32", HK, "zh", "澄川智造 2025 年上半年的收入是多少？", [r"^Revenue {note}{amount}"]),
    N("S33", HK, "en", "What was Chengchuan's capital expenditure in the first half of 2025?", ["capital expenditure"],
      "not stated"),
)
DEV = [
    {"id": "D1", "doc": ZH, "lang": "zh", "kind": "answerable", "query": "澄川智造 2025 年度的销售费用是多少？",
     "pages": [1], "answer": "6,845.20 万元", "accept": ["6,845.20"]},
    {"id": "D2", "doc": EN, "lang": "en", "kind": "answerable", "query": "What was Chengchuan's service revenue in fiscal 2025?",
     "pages": [1], "answer": "24,118", "accept": ["24,118"]},
    {"id": "D3", "doc": HK, "lang": "zh", "kind": "answerable", "query": "澄川智造 2025 年上半年的毛利是多少？",
     "pages": [1], "answer": "422,352", "accept": ["422,352"]},
    {"id": "D4", "doc": Q3, "lang": "zh", "kind": "absent", "query": "澄川智造 2025 年在欧洲市场的营业收入是多少？",
     "near": "只有境内、境外"},
]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("make_financial_pdfs", ROOT / "scripts/make_financial_pdfs.py")
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    folder = tmp_path_factory.mktemp("s6")
    reports = folder / "reports"
    reports.mkdir()
    generator.generate(reports)
    sources = folder / "sources.json"
    listed = ((ZH, "澄川智造（年报）", "zh"), (EN, "Chengchuan (10-K)", "en"), (Q3, "澄川智造（三季报）", "zh"),
              (HK, "Chengchuan (interim)", "en"))
    sources.write_text(json.dumps({"reports": [
        {"key": key, "company": company, "document": key, "language": language, "pages": 2, "url": "-",
         "sha256": hashlib.sha256((reports / f"{key}.pdf").read_bytes()).hexdigest()}
        for key, company, language in listed]}, ensure_ascii=False))
    dev = folder / "dev.json"
    dev.write_text(json.dumps({"id": "synthetic-dev", "answer_rule": "-", "questions": DEV}, ensure_ascii=False))
    return {"folder": folder, "reports": reports, "sources": sources, "dev": dev}


def make_draft(corpus, out, **kwargs):
    return wh.draft(corpus["reports"], candidates=CANDIDATES, sources=corpus["sources"], per_report=PER_REPORT,
                    out=out, **kwargs)


def freeze(corpus, out, **kwargs):
    return wh.freeze(sources=corpus["sources"], out=out, frozen=out / "heldout.json",
                     freeze_file=out / "heldout-freeze.json", expected=EXPECTED, **kwargs)


@pytest.fixture(scope="module")
def frozen(corpus):
    """A held-out set made the way the real one is: drafted from page text, confirmed, frozen."""
    out = corpus["folder"] / "heldout"
    assert make_draft(corpus, out, images=False) == 0
    assert freeze(corpus, out, reviewer="tester", confirm_all=True) == 0
    return out


def register(patch, corpus, frozen):
    """Point both registrations at the synthetic corpus and sets; the code under test is unchanged."""
    base = copy.deepcopy(s4.PREREG)
    base["questions"] = {"path": str(corpus["dev"]), "sha256": s4.sha256(corpus["dev"])}
    base["sources"] = {"path": str(corpus["sources"]), "sha256": s4.sha256(corpus["sources"])}
    base["permission_checks"]["restricted_copies"] = [
        {"key": f"board-only-copy-{ZH}", "source": ZH, "check": "role", "scope": "finreports-2025", "roles": ["board"],
         "classification": 1, "why": "-"},
        {"key": f"other-project-copy-{EN}", "source": EN, "check": "scope", "scope": "board-restricted",
         "roles": ["finance"], "classification": 1, "why": "-"}]
    patch.setattr(s4, "PREREG", base)
    ours = copy.deepcopy(run_arms.PREREG)
    ours["sets"]["dev"] |= {"path": str(corpus["dev"]), "sha256": s4.sha256(corpus["dev"]), "count": 4,
                            "answerable": 3, "absent": 1}
    ours["sets"]["heldout"] |= {"path": str(frozen / "heldout.json"), "freeze": str(frozen / "heldout-freeze.json"),
                                **{k: v for k, v in EXPECTED.items() if k != "per_report"}}
    patch.setattr(run_arms, "PREREG", ours)
    return ours


@pytest.fixture
def registered(corpus, frozen, monkeypatch):
    return register(monkeypatch, corpus, frozen)


def run(corpus, out, *extra):
    code = run_arms.main(["--reports", str(corpus["reports"]), "--llm", "fake", "--embedder", "fake", "--out", str(out),
                          *extra])
    assert code == 0
    folder = max(out.iterdir(), key=lambda p: p.stat().st_mtime)
    return folder, json.loads((folder / "report.json").read_text())


# --------------------------------------------------------------------------- what is recorded in the repository


def test_the_registration_the_pins_and_the_code_agree():
    registration = run_arms.verify_registration()
    assert registration["pins"]["orchestration_prompt_sha256"] == ro.prompt_sha256()
    assert json.loads(run_arms.PINS.read_text())["cited_answer"]["sha256"] == s4.PREREG["prompts"]["cited_answer"]["sha256"]
    assert run_arms.ARMS == ro.MODES and tuple(run_arms.PREREG["precedence"]) == ro.PRECEDENCE
    assert [arm["max_model_calls"] for arm in run_arms.PREREG["arms"]] == [1, 2, 4, 5]
    assert set(run_arms.PREREG["statuses"]["errors_count_as_wrong"]) == {"ANSWER_ERROR", "VERIFY_ERROR"}
    questions, meta = run_arms.load_dev()  # the frozen S4 set, byte for byte
    assert len(questions) == 40 and meta["sha256"] == s4.PREREG["questions"]["sha256"]


def test_a_changed_role_prompt_stops_the_run_until_it_is_pinned_again(monkeypatch):
    monkeypatch.setattr(ro, "CHECKS", ro.CHECKS + "\n5. unit: the unit is the printed one.")
    with pytest.raises(SystemExit, match="pins.json"):
        run_arms.verify_registration()


def test_in_the_repository_the_held_out_set_is_not_runnable_before_it_is_frozen():
    spec = run_arms.PREREG["sets"]["heldout"]
    if (ROOT / spec["path"]).exists():
        questions, meta = run_arms.load_heldout()  # once frozen it must be the registered, checked set
        assert len(questions) == spec["count"] and meta["verified_by"]
    else:
        with pytest.raises(SystemExit, match="not frozen yet"):
            run_arms.load_heldout()


# --------------------------------------------------------------------------- scoring, failure types, the rule


def hit(id, body, page=1, file=f"{ZH}.pdf"):
    return ContextHit(id=id, version="v1", source_hash=id, classification=1, status="CURRENT",
                      content=f"[source] {file} | p.{page} [1.0,2.0,3.0,4.0] | sha256 0123456789abcdef\n{body}")


GOLD, ELSEWHERE = hit(f"{ZH}#p1-t1", "销售费用 | 6,845.20 | 6,102.77"), hit(f"{EN}#p2-c1", "Interest income 1,842", 2, f"{EN}.pdf")
ANSWERABLE, ABSENT = DEV[0], DEV[3]


def result(status, answer="", citations=()):
    return {"status": status, "found": status == "ANSWERED", "answer": answer, "citations": list(citations)}


@pytest.mark.parametrize(("question", "given", "hits", "correct", "confident", "failure"), [
    (ANSWERABLE, result("ANSWERED", "销售费用 6,845.20 万元", [GOLD.id]), (GOLD,), True, False, None),
    (ANSWERABLE, result("ANSWERED", "销售费用 6,102.77 万元", [GOLD.id]), (GOLD,), False, True, "wrong_figure_gold_retrieved"),
    (ANSWERABLE, result("ANSWERED", "1,842", [ELSEWHERE.id]), (ELSEWHERE,), False, True, "wrong_figure_gold_not_retrieved"),
    (ANSWERABLE, result("ANSWERED", "6,845.20", [ELSEWHERE.id]), (GOLD, ELSEWHERE), False, False, "right_figure_wrong_page"),
    (ANSWERABLE, result("ANSWERED", "见利润表", [GOLD.id]), (GOLD,), False, False, "answer_without_figure"),
    (ANSWERABLE, result("NOT_FOUND"), (GOLD,), False, False, "said_not_found_gold_retrieved"),
    (ANSWERABLE, result("NOT_FOUND"), (ELSEWHERE,), False, False, "said_not_found_gold_not_retrieved"),
    (ANSWERABLE, result("NEEDS_REVIEW"), (GOLD,), False, False, "needs_review"),
    (ANSWERABLE, result("VERIFY_ERROR"), (GOLD,), False, False, "error:VERIFY_ERROR"),
    (ABSENT, result("NOT_FOUND"), (GOLD,), True, False, None),
    (ABSENT, result("NEEDS_REVIEW"), (GOLD,), True, False, None),  # no answer shown; counted apart in the summary
    (ABSENT, result("NO_HITS"), (), True, False, None),
    (ABSENT, result("ANSWERED", "欧洲收入 6,845.20 万元", [GOLD.id]), (GOLD,), False, True, "fabricated_figure"),
    (ABSENT, result("ANSWERED", "没有按地区披露", [GOLD.id]), (GOLD,), False, False, "answer_without_figure"),
    (ABSENT, result("ANSWER_ERROR"), (GOLD,), False, False, "error:ANSWER_ERROR"),
    (ABSENT, result("VERIFY_ERROR"), (GOLD,), False, False, "error:VERIFY_ERROR"),  # an error is never "not found"
])
def test_scoring_is_the_registered_s4_rule_and_every_miss_gets_one_failure_type(question, given, hits, correct,
                                                                             confident, failure):
    scored = run_arms.score(question, given, hits)
    gold_in = any(s4.on_gold_page(h, question["doc"], question["pages"]) for h in hits) if "pages" in question else None
    assert scored["correct"] is correct and scored["wrong_but_confident"] is confident
    assert run_arms.failure_type(question, given, scored, gold_in) == failure


def test_the_two_diagnostic_flags_describe_an_answer_without_blocking_it():
    flagged = run_arms.flags(ANSWERABLE, result("ANSWERED", "2025 年销售费用 6,845.20 万元，约 0.68 亿元", [GOLD.id, ELSEWHERE.id]),
                             (GOLD, ELSEWHERE))
    assert flagged == {"cites_other_report": [ELSEWHERE.id], "figures_not_in_cited": ["0.68"]}  # 2025 is a year
    assert run_arms.flags(ANSWERABLE, result("NOT_FOUND"), (GOLD,)) == {"cites_other_report": [], "figures_not_in_cited": []}


def summary(**arms):
    return {"arms": {arm: {"answerable_correct": {"all": {"n": right}}, "wrong_but_confident": {"total": confident}}
                     for arm, (right, confident) in arms.items()}}


@pytest.mark.parametrize(("measured", "permissions", "adopt"), [
    (dict(single=(13, 9), self_check=(14, 8), verify=(16, 9), plan_verify=(14, 9)), True, "verify"),
    (dict(single=(13, 9), self_check=(13, 8), verify=(15, 9), plan_verify=(13, 9)), True, None),  # +2 over single
    (dict(single=(13, 9), self_check=(15, 8), verify=(16, 9), plan_verify=(13, 9)), True, None),  # +1 over self-check
    (dict(single=(13, 9), self_check=(14, 8), verify=(16, 10), plan_verify=(13, 9)), True, None),  # confident up
    (dict(single=(13, 9), self_check=(14, 8), verify=(16, 9), plan_verify=(13, 9)), False, None),  # a leak
    (dict(single=(13, 9), self_check=(14, 8), verify=(16, 9), plan_verify=(18, 7)), True, "plan_verify"),
    (dict(single=(13, 9), self_check=(14, 8), verify=(17, 9), plan_verify=(17, 4)), True, "verify"),  # tie: fewer calls
    (dict(single=(13, 9), self_check=(14, 8), verify=(13, 2), plan_verify=(19, 10)), True, None),
])
def test_the_adoption_rule_is_the_registered_one(measured, permissions, adopt):
    decision = run_arms.decide(summary(**measured), permissions)
    assert decision["adopt"] == adopt and decision["conclusion"] == (f"采用 {adopt}" if adopt else "不采用")
    assert decision["rule"]["min_answerable_gain_over_single"] == 3
    assert decision["rule"]["min_answerable_gain_over_self_check"] == 2
    assert decision["rule"]["max_wrong_but_confident_increase_over_single"] == 0
    assert set(decision["arms"]) == {"verify", "plan_verify"}


# --------------------------------------------------------------------------- one call per distinct prompt


class Counting:
    model_id = "test/counting"

    def __init__(self, *replies):
        self.replies, self.calls, self.asked = list(replies), [], 0

    def complete(self, messages, schema):
        self.asked += 1
        reply = self.replies.pop(0) if self.replies else f"reply {self.asked}"
        self.calls.append({"ms": 12.5, "finish_reason": "stop", "usage": {"prompt_tokens": 7}, "reasoning_returned": False})
        if isinstance(reply, Exception):
            raise reply
        return reply


def prompt(text):
    return [{"role": "user", "content": text}]


def test_the_same_prompt_reaches_the_model_once_and_each_arm_still_counts_its_own_call(tmp_path):
    inner = Counting()
    memo = run_arms.CallMemo(inner, tmp_path / "calls.sqlite", "model@digest")
    first, second = run_arms.ArmModel(memo), run_arms.ArmModel(memo)
    assert first.complete(prompt("a"), {"type": "object"}) == second.complete(prompt("a"), {"type": "object"})
    assert second.complete(prompt("b"), {"type": "object"}) != first.complete(prompt("a"), {"type": "object"})
    assert inner.asked == 2 and len(memo.sent) == 2
    assert [c["shared"] for c in first.calls] == [False, True] and [c["shared"] for c in second.calls] == [True, False]
    assert [c["ms"] for c in first.calls + second.calls] == [12.5] * 4  # the call's own duration, also when shared
    assert len(first.prompts) == 2 and len(second.prompts) == 2
    # Another schema, or another model, is another call.
    first.complete(prompt("a"), {"type": "array"})
    other = run_arms.CallMemo(inner, tmp_path / "calls.sqlite", "other-model@digest")
    run_arms.ArmModel(other).complete(prompt("a"), {"type": "object"})
    assert inner.asked == 4


def test_call_records_on_disk_let_a_later_process_continue_without_asking_again(tmp_path):
    inner = Counting()
    run_arms.ArmModel(run_arms.CallMemo(inner, tmp_path / "calls.sqlite", "m")).complete(prompt("a"), {})
    later = run_arms.CallMemo(Counting(), tmp_path / "calls.sqlite", "m")
    arm = run_arms.ArmModel(later)
    assert arm.complete(prompt("a"), {}) == "reply 1" and later.inner.asked == 0
    assert later.from_disk == 1 and arm.calls[0]["from_disk"] is True and arm.calls[0]["ms"] == 12.5
    arm.complete(prompt("a"), {})
    assert later.from_disk == 1 and arm.calls[1] == arm.calls[0] | {"shared": True, "from_disk": False}


def test_an_unusable_reply_is_kept_but_a_call_that_got_no_reply_is_tried_again_and_never_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(run_arms.time, "sleep", lambda seconds: None)
    inner = Counting(ProviderInvalid("OUTPUT_TRUNCATED"), httpx.ConnectError("down"), httpx.ReadTimeout("slow"), "fine")
    memo = run_arms.CallMemo(inner, tmp_path / "calls.sqlite", "m")
    for _ in range(2):  # the same input would be cut off again: the second arm gets the same failure, not a new call
        with pytest.raises(ProviderInvalid, match="OUTPUT_TRUNCATED"):
            run_arms.ArmModel(memo).complete(prompt("long"), {})
    assert inner.asked == 1
    assert run_arms.ArmModel(memo).complete(prompt("b"), {}) == "fine"  # two transport failures, then a reply
    assert inner.asked == 4 and memo.transport_retries == 2
    down = run_arms.CallMemo(Counting(*[httpx.ConnectError("down")] * 3), None, "m")
    arm = run_arms.ArmModel(down)
    with pytest.raises(httpx.ConnectError):
        arm.complete(prompt("c"), {})
    assert len(arm.calls) == 1 and arm.calls[0]["key"] is None  # counted for the arm, nothing stored
    assert down.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0


def test_a_real_held_out_run_is_found_again_so_the_set_is_run_once(tmp_path):
    for name, status, sets in (("arms-a", "REAL", {"dev": {}}), ("arms-b", "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS",
                                                                {"heldout": {}}), ("arms-c", "REAL", {"heldout": {}})):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(json.dumps({"status": status, "sets": sets}))
    (tmp_path / "arms-d").mkdir()  # interrupted: no report yet
    assert run_arms.earlier_real_heldout_runs(tmp_path) == [str(tmp_path / "arms-c" / "report.json")]


# --------------------------------------------------------------------------- the whole runner, on stand-ins


@pytest.fixture(scope="module")
def finished(corpus, frozen):
    out = corpus["folder"] / "runs"
    with pytest.MonkeyPatch.context() as patch:  # undone before any other test looks at the registrations
        register(patch, corpus, frozen)
        folder, report = run(corpus, out)
    return {"out": out, "folder": folder, "report": report}


def test_four_arms_answer_both_sets_end_to_end(finished):
    report = finished["report"]
    assert report["status"] == "FAKE_PLUMBING_ONLY_NUMBERS_MEANINGLESS" and report["resumed"] is False
    assert list(report["sets"]) == ["dev", "heldout"] and report["inputs_unchanged_after"] is True
    assert report["sets"]["dev"]["questions_run"] == 4 and report["sets"]["heldout"]["questions_run"] == 12
    assert report["sets"]["dev"]["decision"] is None  # the development set never decides anything
    assert report["sets"]["heldout"]["decision"]["conclusion"] in {"不采用", "采用 verify", "采用 plan_verify"}
    assert report["permissions"]["passed"] is True and report["permissions"]["leak_total"] == 0
    assert report["permissions"]["store_rows_seen"] > 0 and report["permissions"]["model_prompts"] > 0
    assert report["registration"]["pins"]["orchestration_prompt_sha256"] == ro.prompt_sha256()
    for name in ("dev", "heldout"):
        for q in report["sets"][name]["questions"]:
            assert set(q["arms"]) == set(ro.MODES)
            for arm, got in q["arms"].items():
                trace = got["orchestration"]
                assert got["calls"] == trace["calls"] == len(trace["steps"]) <= {"single": 1, "self_check": 2,
                                                                                 "verify": 4, "plan_verify": 5}[arm]
                assert sum(got["calls_by_step"].values()) == got["calls"]
                assert got["ms"]["end_to_end"] == round(got["ms"]["retrieval"] + got["ms"]["planned_retrieval"]
                                                        + got["ms"]["model"], 1)
                assert got["audit"]["citations_outside_hits"] == 0 and set(got["citations"]) <= set(got["evidence"]["hits"])
                assert (got["failure"] is None) == got["score"]["correct"]


def test_arms_share_the_first_answer_and_differ_only_in_their_own_steps(finished):
    report = finished["report"]
    questions = report["sets"]["dev"]["questions"] + report["sets"]["heldout"]["questions"]
    for q in questions:
        first = {arm: next(s for s in q["arms"][arm]["orchestration"]["steps"] if s["step"] == "answer")
                 for arm in ("single", "self_check", "verify")}
        assert first["single"]["shared"] is False and first["self_check"]["shared"] and first["verify"]["shared"]
        assert first["single"]["raw_output"] == first["self_check"]["raw_output"] == first["verify"]["raw_output"]
        planned = q["arms"]["plan_verify"]
        if planned["evidence"]["source"] == "original":  # fell back: exactly arm A's calls after the plan
            assert [s["shared"] for s in planned["orchestration"]["steps"][1:]] == [True] * (planned["calls"] - 1)
            assert planned["status"] == q["arms"]["verify"]["status"]
    calls = report["model_calls"]
    counted = sum(got["calls"] for q in questions for got in q["arms"].values())
    assert calls["distinct_prompts"] == calls["sent_this_process"]["n"] < counted
    statuses = {q["arms"]["verify"]["status"] for q in questions}
    assert "NEEDS_REVIEW" in statuses and "ANSWERED" in statuses, "the stand-ins must exercise pass and veto"
    assert {q["arms"]["plan_verify"]["orchestration"]["plan"]["status"] for q in questions} >= {"USED"}


def test_the_summary_adds_up_and_names_what_went_wrong(finished):
    report = finished["report"]
    for name in ("dev", "heldout"):
        body, questions = report["sets"][name]["summary"], report["sets"][name]["questions"]
        assert body["questions"]["all"] == len(questions)
        for arm in ro.MODES:
            got = body["arms"][arm]
            assert got["answerable_correct"]["all"]["of"] == body["questions"]["answerable"]
            assert got["absent_correct"]["all"]["of"] == body["questions"]["absent"]
            missed = sum(len(ids) for ids in got["failure_types"]["answerable"].values())
            assert missed == got["answerable_correct"]["all"]["of"] - got["answerable_correct"]["all"]["n"]
            assert got["calls"]["total"] == sum(q["arms"][arm]["calls"] for q in questions)
            assert sum(got["calls"]["by_step"].values()) == got["calls"]["total"]
            assert sum(got["status_counts"].values()) == len(questions)
            assert got["wrong_but_confident"]["total"] == len(got["wrong_but_confident"]["ids"])
        assert body["arms"]["single"]["calls"]["by_step"] == {"answer": len(questions)}
        assert "verifier" in body["arms"]["verify"] and "verifier" not in body["arms"]["self_check"]
        assert "plan" in body["arms"]["plan_verify"] and "plan" not in body["arms"]["verify"]
        needs = body["arms"]["verify"]["needs_review"]
        absent_review = [q["id"] for q in questions if q["kind"] == "absent" and q["arms"]["verify"]["status"] == "NEEDS_REVIEW"]
        assert needs["absent"] == absent_review  # counted apart: these are "correct" without a clean "not found"


def test_an_interrupted_run_continues_from_its_call_records(finished, corpus, registered):
    folder, first = finished["folder"], finished["report"]
    with pytest.raises(SystemExit, match="nothing to resume"):
        run_arms.main(["--reports", str(corpus["reports"]), "--llm", "fake", "--embedder", "fake", "--resume", str(folder)])
    (folder / "report.json").rename(folder / "first-report.json")
    with pytest.raises(SystemExit, match="start a new run"):  # other sets than the folder was started with
        run_arms.main(["--reports", str(corpus["reports"]), "--llm", "fake", "--embedder", "fake", "--sets", "dev",
                       "--resume", str(folder)])
    assert run_arms.main(["--reports", str(corpus["reports"]), "--llm", "fake", "--embedder", "fake", "--resume",
                          str(folder)]) == 0
    again = json.loads((folder / "report.json").read_text())
    assert again["resumed"] is True and again["started_at"] == first["started_at"]
    assert again["model_calls"]["sent_this_process"]["n"] == 0
    assert again["model_calls"]["reused_from_disk"] == first["model_calls"]["distinct_prompts"]
    for name in ("dev", "heldout"):
        for before, after in zip(first["sets"][name]["questions"], again["sets"][name]["questions"], strict=True):
            for arm in ro.MODES:
                assert {k: after["arms"][arm][k] for k in ("status", "answer", "citations", "calls", "score")} == \
                    {k: before["arms"][arm][k] for k in ("status", "answer", "citations", "calls", "score")}


def test_plumbing_runs_are_labelled_and_limited_runs_decide_nothing(corpus, registered, tmp_path):
    folder, report = run(corpus, tmp_path, "--sets", "heldout", "--limit", "2")
    assert folder.name.endswith("-fake") and report["status"].startswith("FAKE")
    assert report["sets"]["heldout"]["questions_run"] == 2 and report["sets"]["heldout"]["decision"] is None
    with pytest.raises(SystemExit, match="plumbing"):
        run_arms.main(["--reports", str(corpus["reports"]), "--limit", "2"])


# --------------------------------------------------------------------------- the real-model path, against a local server


class FakeOllama(BaseHTTPRequestHandler):
    """The part of Ollama's HTTP API the runner uses, answered by the plumbing stand-ins. Not a model."""

    served, context, parameters = "forgeflow-qwen3-8b-ctx16k", 16384, "num_ctx 16384"
    roles, embedder, chats = run_arms.FakeRoles(), s4.FakeEmbedder(), []

    def _reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 - http.server's name
        details = {"format": "gguf", "family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}
        if self.path == "/api/version":
            return self._reply({"version": "0.0-test"})
        if self.path == "/api/tags":
            return self._reply({"models": [{"name": name, "digest": digest, "size": 1, "details": details}
                                           for name, digest in ((f"{self.served}:latest", "d-served"),
                                                                ("qwen3:8b", "d-weights"), ("bge-m3:latest", "d-embed"))]})
        return self._reply({"models": [{"name": f"{self.served}:latest", "digest": "d-served",
                                        "context_length": type(self).context, "size_vram": 1}]})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/show":
            served = body["model"] == self.served
            return self._reply({"modelfile": "FROM /blobs/sha256-a3de86cd", "details": {
                "format": "gguf", "family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"},
                "parameters": type(self).parameters if served else "temperature 0.6"})
        if self.path == "/api/embed":
            return self._reply({"embeddings": self.embedder.embed(body["input"])})
        self.chats.append({k: body[k] for k in ("model", "temperature", "seed", "reasoning_effort", "max_tokens")})
        text = self.roles.complete(body["messages"], body["response_format"]["json_schema"]["schema"])
        return self._reply({"choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                                                                                         "content": text}}],
                            "usage": {"prompt_tokens": len(body["messages"][-1]["content"]) // 4, "completion_tokens": 9}})

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeOllama.context, FakeOllama.parameters, FakeOllama.chats = 16384, "num_ctx 16384", []
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def real(corpus, out, url, *extra):
    return run_arms.main(["--reports", str(corpus["reports"]), "--ollama-url", url, "--out", str(out),
                          "--cache", str(out / "embeddings.sqlite"), *extra])


def test_the_real_path_checks_the_served_model_and_runs_the_held_out_set_once(corpus, registered, ollama, tmp_path):
    out = tmp_path / "runs"
    out.mkdir()
    assert real(corpus, out, ollama) == 0
    folder = next(p for p in out.iterdir() if p.is_dir())
    report = json.loads((folder / "report.json").read_text())
    assert report["status"] == "REAL" and folder.name.startswith("arms-") and not folder.name.endswith(("-fake", "-plumbing"))
    assert report["llm"] == "ollama/forgeflow-qwen3-8b-ctx16k" and report["llm_settings"] == s4.PREREG["model"]["settings"]
    assert report["ollama"]["after_warmup"][0]["context_length"] == 16384
    assert report["sets"]["heldout"]["decision"]["conclusion"] in {"不采用", "采用 verify", "采用 plan_verify"}
    # Every role ran on the one registered tag with the registered settings; the warm-up call is not counted.
    assert {(c["model"], c["temperature"], c["seed"], c["reasoning_effort"], c["max_tokens"]) for c in FakeOllama.chats} \
        == {("forgeflow-qwen3-8b-ctx16k", 0, 0, "none", 512)}
    sent = report["model_calls"]["sent_this_process"]
    assert sent["n"] == report["model_calls"]["distinct_prompts"] == len(FakeOllama.chats) - 1
    assert sent["max_prompt_tokens"] > 0 and sent["calls_over_context"] == 0 and sent["not_stopped"] == 0
    steps = [s for q in report["sets"]["dev"]["questions"] for s in q["arms"]["verify"]["orchestration"]["steps"]]
    assert all(s["prompt_tokens"] > 0 for s in steps)
    # The held-out set is run once: a second real run is refused, the development set can be run again.
    with pytest.raises(SystemExit, match="already run for real"):
        real(corpus, out, ollama)
    assert real(corpus, out, ollama, "--sets", "dev") == 0


@pytest.mark.parametrize(("context", "parameters", "message"), [
    (4096, "num_ctx 16384", "loaded with context 4096"),
    (16384, "temperature 0.6", "does not declare the registered num_ctx"),
])
def test_a_served_model_without_the_long_context_stops_the_run_before_any_question(corpus, registered, ollama, tmp_path,
                                                                                 context, parameters, message):
    FakeOllama.context, FakeOllama.parameters = context, parameters
    with pytest.raises(SystemExit, match=message):
        real(corpus, tmp_path, ollama)
    assert len(FakeOllama.chats) <= 1  # at most the warm-up on text unrelated to the reports


# --------------------------------------------------------------------------- held-out questions: candidates


def test_the_written_candidates_can_fill_the_registered_composition():
    reports = {r["key"]: r for r in json.loads(wh.SOURCES.read_text())["reports"]}
    dev = {q["query"] for q in json.loads(wh.DEV.read_text())["questions"]}
    wh.check_candidates(wh.CANDIDATES, reports, dev)  # unique ids, known reports, patterns compile, no tool question
    per = run_arms.PREREG["sets"]["heldout"]["per_report"]
    assert per == {"answerable": 7, "answerable_cross_language": 2, "absent": 3}
    for key, report in reports.items():
        own = [c for c in wh.CANDIDATES if c.doc == key]
        cross = [c for c in own if c.lang != report["language"]]
        answerable = [c for c in own if c.kind == "answerable"]
        # More than needed of each kind: some will not be found in the page text and are skipped.
        assert len([c for c in answerable if c not in cross]) > per["answerable"] - per["answerable_cross_language"]
        assert len([c for c in answerable if c in cross]) > per["answerable_cross_language"]
        assert len([c for c in own if c.kind == "absent" and c not in cross]) > 2
        assert len([c for c in own if c.kind == "absent" and c in cross]) > 1
        assert all("2025" in c.query for c in own), "every question names the period it asks about"


def test_a_candidate_that_a_report_tool_answers_or_the_development_set_asks_is_refused():
    reports = {r["key"]: r for r in json.loads(wh.SOURCES.read_text())["reports"]}
    tool = A("X1", wh.CATL, "zh", "宁德时代2025年经营活动产生的现金流量净额是多少？", [r"^经营活动产生的现金流量净额 {amount}"])
    with pytest.raises(SystemExit, match="report tool"):
        wh.check_candidates((tool,), reports, set())
    asked = A("X2", wh.MOUTAI, "zh", "贵州茅台 2025 年的营业收入是多少？", [r"^营业收入 {amount}"])
    with pytest.raises(SystemExit, match="development set"):
        wh.check_candidates((asked,), reports, {asked.query})
    with pytest.raises(ValueError, match="no value slot"):
        wh.compile_pattern(r"^营业收入 \d+")


# --------------------------------------------------------------------------- held-out questions: reading pages


def lines(*texts):
    return [{"text": text, "x0": 10.0, "top": 20.0 * i, "x1": 500.0, "bottom": 20.0 * i + 12} for i, text in enumerate(texts)]


def test_patterns_read_the_printed_line_not_the_note_column_or_a_quarter(corpus):
    pages = wh.page_lines(corpus["reports"] / f"{ZH}.pdf")
    by_id = {c.id: c for c in CANDIDATES}
    research = wh.locate(by_id["S01"], pages, "zh")
    assert research["located"] and research["accept"] == ["9,012.73"] and research["pages"] == [1]
    assert research["evidence"][0]["line"] == "研发费用 五、45 9,012.73 7,431.58"  # 五、45 is the note, not the figure
    assert len(research["evidence"][0]["box"]) == 4
    tax = wh.locate(by_id["S03"], pages, "zh")
    assert tax["accept"] == ["2,712.30"] and tax["cross_language"] is True  # full-width colon folded: 减：所得税费用
    assert wh.locate(by_id["S05"], pages, "zh") == {
        "id": "S05", "doc": ZH, "lang": "zh", "kind": "answerable", "query": by_id["S05"].query, "cross_language": False,
        "located": False, "reason": "no line matches any pattern"}


def test_a_sentence_that_wraps_is_found_once_over_two_lines(corpus):
    pages = wh.page_lines(corpus["reports"] / f"{EN}.pdf")
    found = wh.find_matches((r"Total revenue for fiscal 2025 was \$ ?{decimal} million",), pages, span=2, joiner=" ")
    assert [(m["page"], m["value"]) for m in found] == [(1, "176.5")]
    assert "Total revenue for fiscal 2025 was" in found[0]["line"] and "$176.5 million" in found[0]["line"]
    assert wh.find_matches((r"Total revenue for fiscal 2025 was \$ ?{decimal} million",), pages, span=1, joiner=" ") == []
    row = wh.find_matches((r"^Net income {usd}{amount}",), pages, span=3, joiner=" ")
    assert [(m["page"], m["value"], m["line"]) for m in row] == [(2, "10,854", "Net income $ 10,854 $ 5,206 108.5%")]


def test_wrapped_lines_are_joined_the_way_each_language_prints_them():
    english = [lines("Revenues from VAS increased by 16% year-on-", "year to RMB369.3 billion for the year ended 31",
                     "December 2025.")]
    found = wh.find_matches((r"Revenues from VAS " + wh.MOVED,), english, span=3, joiner=" ")
    assert [m["value"] for m in found] == ["369.3"] and found[0]["box"] == [10.0, 0.0, 500.0, 52.0]
    chinese = [lines("向全体股东每 10 股派发现", "金分红 69.57 元（含税）。")]
    assert [m["value"] for m in wh.find_matches((r"每 ?10 ?股派发现金分红 ?{decimal} ?元",), chinese, span=2, joiner="")] == ["69.57"]


def test_gold_pages_are_every_place_the_figure_is_printed_and_other_figures_are_only_shown():
    pages = [
        lines("主要会计数据", "归属于上市公司股东的净利润 82,320,067,101.68 86,228,146,421.62 -4.53"),
        lines("分季度主要财务数据", "归属于上市公司股东的净利润 26,847,000,000.00 18,555,000,000.00"),
        lines("合并利润表", "归属于母公司股东的净利润 82,320,067,101.68 86,228,146,421.62"),
        lines("加权平均净资产收益率（%） 32.53 36.02", "研发人员 32.53 人均"),
    ]
    profit = wh.locate(A("P", "doc", "zh", "净利润？", [r"^归属于上市公司股东的净利润 {amount}"], where="主要会计数据", unit="元"),
                       pages, "zh")
    assert profit["pages"] == [1, 3]  # a long figure printed again under another label is the same fact
    assert [e["why"] for e in profit["evidence"]] == ["标签和数字", "同一数字在别处又印了一次"]
    assert profit["other_values"] == [{"page": 2, "value": "26,847,000,000.00", "line": pages[1][1]["text"]}]
    assert profit["accept"] == ["82,320,067,101.68", "823.20亿", "823.2亿", "823亿"]
    ratio = wh.locate(A("R", "doc", "en", "ROE?", [r"^加权平均净资产收益率(?: ?\(%\))? {decimal}"]), pages, "zh")
    assert ratio["accept"] == ["32.53"] and ratio["pages"] == [4]
    assert len(ratio["evidence"]) == 1  # a short figure elsewhere on the page is not evidence


@pytest.mark.parametrize(("value", "unit", "forms"), [
    ("168,838,102,514.79", "元", ["168,838,102,514.79", "1688.38亿", "1688.4亿", "1688亿"]),
    ("72,201,282", "千元", ["72,201,282", "722.01亿", "722亿", "72.2 billion", "72.20 billion"]),
    ("369.3", "RMB billion", ["369.3", "3693亿"]),
    ("224,842", "RMB million", ["224,842", "2248.42亿", "2248.4亿", "2248亿", "224.8 billion", "224.84 billion"]),
    ("33,708", "USD million", ["33,708", "337.08亿", "337.1亿", "337亿", "33.7 billion", "33.71 billion"]),
    ("16.14", None, ["16.14"]),
    ("-627.62", None, ["627.62"]),
    ("5,000", "元", ["5,000"]),  # far below 1 亿: no converted form
])
def test_accepted_forms_are_the_printed_figure_and_its_plain_conversions(value, unit, forms):
    assert wh.accept_forms(value, unit) == forms
    answer = f"约 {forms[-1]}" if len(forms) > 1 else forms[0]
    assert s4.accept_match(answer, forms) == forms[-1]  # each form is one the registered matching can find


def test_the_draft_takes_the_first_located_candidates_per_report_in_the_registered_proportions():
    def item(id, doc, kind, cross, located=True):
        return {"id": id, "doc": doc, "kind": kind, "cross_language": cross, "located": located}

    pool = [item("a1", "d", "answerable", False), item("a2", "d", "answerable", False, located=False),
            item("a3", "d", "answerable", False), item("a4", "d", "answerable", False), item("c1", "d", "answerable", True),
            item("c2", "d", "answerable", True), item("n1", "d", "absent", False), item("n2", "d", "absent", False),
            item("n3", "d", "absent", False), item("m1", "d", "absent", True)]
    per = {"answerable": 3, "answerable_cross_language": 1, "absent": 3}
    chosen, short = wh.choose(pool, per, set())
    assert [c["id"] for c in chosen] == ["a1", "a3", "c1", "n1", "n2", "m1"] and short == []
    chosen, short = wh.choose(pool, per, {"a1", "m1"})
    assert [c["id"] for c in chosen] == ["a3", "a4", "c1", "n1", "n2"] and short == ["d: 0 of 1 cross-language absent"]


# --------------------------------------------------------------------------- held-out questions: draft, check, freeze


def test_a_draft_proposes_gold_from_page_text_and_marks_every_question_as_unchecked(corpus, tmp_path):
    assert make_draft(corpus, tmp_path) == 0
    draft = json.loads((tmp_path / "heldout-draft.json").read_text())
    questions = {q["id"]: q for q in draft["questions"]}
    assert list(questions) == ["S01", "S03", "S04", "S13", "S12", "S14", "S21", "S22", "S23", "S31", "S32", "S33"]
    assert all(q["review"] == {"status": "pending", "label": "待人工核对", "by": None, "at": None, "note": ""}
               for q in questions.values())
    assert draft["status"].startswith("DRAFT") and draft["sources_sha256"] == wh.sha256(corpus["sources"])
    assert questions["S21"]["accept"] == ["1,356,789,012.34", "13.57亿", "13.6亿", "14亿"] and questions["S21"]["pages"] == [1]
    assert questions["S31"]["accept"] == ["0.20"] and questions["S31"]["pages"] == [2]
    assert questions["S04"]["mentions"] > 0 and questions["S04"]["evidence"][0]["term"] == "营业收入"
    assert questions["S14"]["evidence"] == [] and questions["S14"]["mentions"] == 0
    assert wh.composition(draft["questions"], {ZH: "zh", EN: "en", Q3: "zh", HK: "en"}) == EXPECTED | {
        "per_report": {key: PER_REPORT for key in (ZH, EN, Q3, HK)}}
    report = json.loads((tmp_path / "heldout-candidates.json").read_text())
    assert report["chosen"] == list(questions) and report["short"] == []
    assert [c["id"] for c in report["candidates"] if not c["located"]] == ["S05"]
    listing = (tmp_path / "heldout-checklist.md").read_text()
    assert listing.count("待人工核对") >= len(questions) and "研发费用 五、45 9,012.73 7,431.58" in listing
    page = (tmp_path / "heldout-checklist.html").read_text()
    assert page.count("<section class='card'") == len(questions) and page.count("data:image/") == 8  # one page each
    assert f"{(corpus['reports'] / f'{ZH}.pdf').resolve().as_uri()}#page=1" in page
    assert all(f"data-proposal='{wh.proposal_hash(q)}'" in page for q in questions.values())


def test_too_few_located_candidates_is_an_error_not_a_smaller_set(corpus, tmp_path):
    fewer = tuple(c for c in CANDIDATES if c.id not in {"S03", "S23"})
    code = wh.draft(corpus["reports"], candidates=fewer, sources=corpus["sources"], per_report=PER_REPORT, out=tmp_path,
                    images=False)
    assert code == 2
    assert json.loads((tmp_path / "heldout-candidates.json").read_text())["short"] == [
        f"{ZH}: 0 of 1 cross-language answerable", f"{Q3}: 0 of 1 absent"]
    with pytest.raises(SystemExit, match="not verified"):
        freeze(corpus, tmp_path, reviewer="tester")


def test_a_changed_or_missing_pdf_stops_the_draft(corpus, tmp_path):
    copy_dir = tmp_path / "reports"
    copy_dir.mkdir()
    for pdf in corpus["reports"].glob("*.pdf"):
        (copy_dir / pdf.name).write_bytes(pdf.read_bytes() + (b"\n%tampered" if pdf.stem == EN else b""))
    with pytest.raises(SystemExit, match="hash differs"):
        wh.draft(copy_dir, candidates=CANDIDATES, sources=corpus["sources"], per_report=PER_REPORT, out=tmp_path / "out")


def marks(draft, **status):
    return {"set": wh.SET_ID, "reviewer": "tester", "items": {
        q["id"]: {"status": status.get(q["id"], "verified"), "note": "见原页" if q["id"] in status else "",
                  "proposal": wh.proposal_hash(q)} for q in draft["questions"]}}


def test_nothing_is_frozen_until_a_person_has_marked_every_question(corpus, tmp_path, capsys):
    assert make_draft(corpus, tmp_path, images=False) == 0
    with pytest.raises(SystemExit, match="12 question"):
        freeze(corpus, tmp_path, reviewer="tester")
    with pytest.raises(SystemExit, match="--reviewer"):
        freeze(corpus, tmp_path, reviewer="  ", confirm_all=True)
    with pytest.raises(SystemExit, match="--reviewer"):
        freeze(corpus, tmp_path, confirm_all=True)
    draft = json.loads((tmp_path / "heldout-draft.json").read_text())
    review = tmp_path / "heldout-review.json"
    review.write_text(json.dumps(marks(draft, S03="fix", S04="drop"), ensure_ascii=False))
    with pytest.raises(SystemExit, match="2 question"):
        freeze(corpus, tmp_path, reviewer="tester", review=review)
    assert "S03: fix 见原页" in capsys.readouterr().err
    assert not (tmp_path / "heldout.json").exists()
    kept = {q["id"]: q["review"]["status"] for q in json.loads((tmp_path / "heldout-draft.json").read_text())["questions"]}
    assert kept == {q["id"]: "pending" if q["id"] in {"S03", "S04"} else "verified" for q in draft["questions"]}


def test_a_mark_counts_only_for_the_proposal_it_was_given_for(corpus, tmp_path):
    assert make_draft(corpus, tmp_path, images=False) == 0
    path = tmp_path / "heldout-draft.json"
    draft = json.loads(path.read_text())
    review = tmp_path / "heldout-review.json"
    review.write_text(json.dumps(marks(draft)))
    changed = copy.deepcopy(draft)
    next(q for q in changed["questions"] if q["id"] == "S01")["pages"] = [1, 4]  # gold edited after the review
    path.write_text(json.dumps(changed, ensure_ascii=False))
    with pytest.raises(SystemExit, match="1 question"):
        freeze(corpus, tmp_path, reviewer="tester", review=review)


def test_a_rejected_question_is_replaced_and_checked_marks_survive_the_new_draft(corpus, tmp_path):
    assert make_draft(corpus, tmp_path, images=False) == 0
    draft = json.loads((tmp_path / "heldout-draft.json").read_text())
    review = tmp_path / "heldout-review.json"
    review.write_text(json.dumps(marks(draft, S04="drop")))
    with pytest.raises(SystemExit):
        freeze(corpus, tmp_path, reviewer="tester", review=review)
    assert make_draft(corpus, tmp_path, images=False, exclude=("S04",)) == 0
    again = json.loads((tmp_path / "heldout-draft.json").read_text())
    states = {q["id"]: q["review"]["status"] for q in again["questions"]}
    assert "S04" not in states and states.pop("S06") == "pending" and set(states.values()) == {"verified"}
    assert again["excluded"] == ["S04"]
    assert make_draft(corpus, tmp_path, images=False) == 0  # the exclusion is remembered
    assert "S04" not in {q["id"] for q in json.loads((tmp_path / "heldout-draft.json").read_text())["questions"]}
    with pytest.raises(SystemExit, match="1 question"):
        freeze(corpus, tmp_path, reviewer="tester")


def test_a_complete_review_file_freezes_the_set_under_the_name_typed_into_the_check_list(corpus, tmp_path):
    assert make_draft(corpus, tmp_path, images=False) == 0
    draft = json.loads((tmp_path / "heldout-draft.json").read_text())
    review = tmp_path / "heldout-review.json"
    review.write_text(json.dumps(marks(draft) | {"reviewer": "核对人甲"}, ensure_ascii=False))
    assert freeze(corpus, tmp_path, review=review) == 0
    record = json.loads((tmp_path / "heldout-freeze.json").read_text())
    assert record["verified_by"] == "核对人甲" and record["verified_how"].startswith("review file")
    assert {q["review"]["by"] for q in json.loads((tmp_path / "heldout.json").read_text())["questions"]} == {"核对人甲"}


def test_a_review_file_that_says_how_it_was_made_is_recorded_that_way(corpus, tmp_path):
    assert make_draft(corpus, tmp_path, images=False) == 0
    draft = json.loads((tmp_path / "heldout-draft.json").read_text())
    review = tmp_path / "heldout-review.json"
    review.write_text(json.dumps(marks(draft) | {"reviewer": "核对人乙", "how": "对照 PDF 原页逐题核对"},
                                 ensure_ascii=False))
    assert freeze(corpus, tmp_path, review=review) == 0
    record = json.loads((tmp_path / "heldout-freeze.json").read_text())
    assert record["verified_by"] == "核对人乙" and record["verified_how"] == "review file: 对照 PDF 原页逐题核对"


def test_the_frozen_set_is_what_the_runner_accepts_and_it_cannot_be_frozen_twice(corpus, frozen, registered):
    data = json.loads((frozen / "heldout.json").read_text())
    record = json.loads((frozen / "heldout-freeze.json").read_text())
    assert data["status"] == "FROZEN" and "excluded" not in data and len(data["questions"]) == 12
    assert all(q["review"]["status"] == "verified" and q["review"]["by"] == "tester" and q["review"]["at"]
               for q in data["questions"])
    assert all("box" not in e for q in data["questions"] for e in q["evidence"])
    assert record["files"]["heldout.json"] == wh.sha256(frozen / "heldout.json") and record["verified_by"] == "tester"
    assert record["verified_how"] == "the reviewer confirmed every question in conversation"
    assert (record["questions"], record["answerable"], record["absent"], record["answerable_cross_language"]) == (12, 8, 4, 4)
    questions, meta = run_arms.load_heldout()
    assert len(questions) == 12 and meta["verified_by"] == "tester" and meta["sha256"] == record["files"]["heldout.json"]
    with pytest.raises(SystemExit, match="already frozen"):
        freeze(corpus, frozen, reviewer="tester", confirm_all=True)


@pytest.mark.parametrize(("change", "message"), [
    (lambda data: data["questions"][0].update(query="另一个问题 2025？"), "differs from the hash"),
    (lambda data: None, "not frozen yet"),
])
def test_the_runner_refuses_a_held_out_set_that_is_missing_or_was_edited_after_the_freeze(corpus, frozen, registered,
                                                                                      tmp_path, change, message):
    target = tmp_path / "heldout.json"
    data = json.loads((frozen / "heldout.json").read_text())
    change(data)
    if message != "not frozen yet":
        target.write_text(json.dumps(data, ensure_ascii=False))
    registered["sets"]["heldout"]["path"] = str(target)
    with pytest.raises(SystemExit, match=message):
        run_arms.load_heldout()


@pytest.mark.parametrize(("change", "message"), [
    (lambda q: q["review"].update(status="pending"), "not checked by a person"),
    (lambda q: q.update(query="宁德时代2025年经营活动产生的现金流量净额是多少？"), "report tool"),
    (lambda q: q.update(doc="unknown_report"), "not in sources.json"),
])
def test_the_runner_checks_every_held_out_question_even_when_the_hash_matches(corpus, frozen, registered, tmp_path,
                                                                           change, message):
    data = json.loads((frozen / "heldout.json").read_text())
    change(data["questions"][0])
    target, record = tmp_path / "heldout.json", tmp_path / "heldout-freeze.json"
    target.write_text(json.dumps(data, ensure_ascii=False))
    record.write_text(json.dumps({"files": {"heldout.json": wh.sha256(target)}}))
    registered["sets"]["heldout"] |= {"path": str(target), "freeze": str(record)}
    with pytest.raises(SystemExit, match=message):
        run_arms.load_heldout()
