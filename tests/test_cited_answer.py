"""Cited answer: citations only from the given hits, no model call without hits, fail-closed, ACL order.

Also the scoring rules of the real-report evaluation (preparation/s4/real-reports/run_qa.py): figure matching
by whole numeric token, unit forms by bounded substring, gold-page citations, wrong-but-confident.
"""

import json
import sys
from pathlib import Path

import httpx
import pytest

from forgeflow import cited_answer as ca
from forgeflow.answerability import OllamaChatModel
from forgeflow.contracts import ContextHit, Principal, Scope
from forgeflow.evt_store import EVTBoundStore
from forgeflow.retrieval import RankedKnowledgeService

ROOT = Path(__file__).resolve().parents[1]
S4 = ROOT / "preparation/s4/real-reports"
sys.path.insert(0, str(S4))
import run_qa  # noqa: E402


def hit(id, content, pages=(6,), file="moutai_2025_annual_zh.pdf"):
    places = "; ".join(f"p.{p} [10.0,20.0,300.0,40.0]" for p in pages)
    return ContextHit(id=id, version="v1", content=f"[source] {file} | {places} | sha256 0123456789abcdef\n{content}",
                      source_hash=id * 2, classification=1, status="CURRENT")


HITS = (
    hit("moutai#p6-t1", "[table] 主要会计数据 | 单位：元 (x1 CNY)\n营业收入 | 168,838,102,514.79 | 170,899,152,276.33"),
    hit("moutai#p17-c3", "国外经销商 126 家。", pages=(17,)),
    hit("moutai#p22-c1", "茅台酒零售价格实行动态调整机制。", pages=(22,)),
)


class ScriptedModel:
    model_id = "test/scripted"

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[list[dict]] = []
        self.schemas: list[dict] = []

    def complete(self, messages, schema):
        self.prompts.append(messages)
        self.schemas.append(schema)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


def reply(answer, citations, found):
    return {"answer": answer, "citations": citations, "found": found}


def test_no_hits_means_not_found_without_calling_the_model():
    model = ScriptedModel(reply("x", ["moutai#p6-t1"], True))
    result = ca.CitedAnswerer(model).answer("营业收入是多少？", [])
    assert result["found"] is False and result["status"] == "NO_HITS" and result["answer"] == ""
    assert result["citations"] == [] and result["model_called"] is False and model.prompts == []


def test_an_answer_cites_only_ids_of_the_hits_it_was_given():
    model = ScriptedModel(reply(" 营业收入 168,838,102,514.79 元。 ", ["moutai#p6-t1", "moutai#p6-t1"], True))
    result = ca.CitedAnswerer(model).answer("营业收入是多少？", HITS)
    assert result["found"] is True and result["status"] == "ANSWERED"
    assert result["answer"] == "营业收入 168,838,102,514.79 元。" and result["citations"] == ["moutai#p6-t1"]
    assert result["rejected_citations"] == [] and result["prompt_sha256"] == ca.prompt_sha256()


def test_citations_outside_the_hits_are_dropped_and_reported():
    model = ScriptedModel(reply("126 家", ["moutai#p17-c3", "moutai#p99-t9", "other-doc#p1-c1"], True))
    result = ca.CitedAnswerer(model).answer("国外经销商多少家？", HITS)
    assert result["found"] is True and result["citations"] == ["moutai#p17-c3"]
    assert result["rejected_citations"] == ["moutai#p99-t9", "other-doc#p1-c1"]


def test_an_answer_with_no_valid_citation_is_not_shown():
    model = ScriptedModel(reply("出厂价 1,169 元", ["made-up#p1-c1"], True))
    result = ca.CitedAnswerer(model).answer("飞天茅台出厂价？", HITS)
    assert result["found"] is False and result["status"] == "UNSUPPORTED"
    assert result["answer"] == "" and result["citations"] == []
    assert result["model_answer"] == "出厂价 1,169 元" and result["rejected_citations"] == ["made-up#p1-c1"]


def test_an_empty_answer_claimed_as_found_is_not_shown():
    result = ca.CitedAnswerer(ScriptedModel(reply("  ", ["moutai#p6-t1"], True))).answer("q", HITS)
    assert result["found"] is False and result["status"] == "UNSUPPORTED" and result["citations"] == []


def test_bracketed_ids_are_the_same_id():
    result = ca.CitedAnswerer(ScriptedModel(reply("126", ["[moutai#p17-c3]"], True))).answer("q", HITS)
    assert result["citations"] == ["moutai#p17-c3"] and result["rejected_citations"] == []


def test_not_found_drops_any_citations_and_text():
    model = ScriptedModel(reply("报告没有给出价格。", ["moutai#p22-c1"], False))
    result = ca.CitedAnswerer(model).answer("飞天茅台出厂价？", HITS)
    assert result["found"] is False and result["status"] == "NOT_FOUND"
    assert result["answer"] == "" and result["citations"] == [] and result["model_called"] is True
    assert result["model_answer"] == "报告没有给出价格。"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("not json", "INVALID_JSON"),
        ("[]", "ANSWER_SHAPE"),
        ('{"answer": "1", "citations": []}', "ANSWER_SHAPE"),
        ('{"answer": "1", "citations": [], "found": true, "confidence": 1}', "ANSWER_SHAPE"),
        ('{"answer": 1, "citations": [], "found": true}', "ANSWER_TYPES"),
        ('{"answer": "1", "citations": "moutai#p6-t1", "found": true}', "ANSWER_TYPES"),
        ('{"answer": "1", "citations": [1], "found": true}', "ANSWER_TYPES"),
        ('{"answer": "1", "citations": [], "found": "yes"}', "ANSWER_TYPES"),
        ('{"answer": "1", "answer": "2", "citations": [], "found": true}', "DUPLICATE_JSON_KEY"),
    ],
)
def test_unusable_output_fails_closed_and_is_not_a_not_found(raw, code):
    result = ca.CitedAnswerer(ScriptedModel(raw)).answer("q", HITS)
    assert result["status"] == "ANSWER_ERROR" and result["found"] is False and result["answer"] == ""
    assert result["citations"] == [] and result["raw_output"] == raw
    if code != "INVALID_JSON":
        assert result["error"] == code


def test_transport_errors_fail_closed():
    result = ca.CitedAnswerer(ScriptedModel(httpx.ConnectError("down"))).answer("q", HITS)
    assert result["status"] == "ANSWER_ERROR" and result["error"] == "ConnectError" and result["found"] is False


def test_qwen_empty_think_block_is_not_the_answer():
    raw = "<think>\n\n</think>\n" + json.dumps(reply("126 家", ["moutai#p17-c3"], True), ensure_ascii=False)
    assert ca.CitedAnswerer(ScriptedModel(raw)).answer("q", HITS)["citations"] == ["moutai#p17-c3"]


def test_prompt_carries_the_question_every_hit_id_and_content_and_the_schema():
    model = ScriptedModel(reply("126", ["moutai#p17-c3"], True))
    ca.CitedAnswerer(model).answer("国外经销商多少家？", HITS)
    (messages,) = model.prompts
    assert [m["role"] for m in messages] == ["system", "user"] and messages[0]["content"] == ca.SYSTEM_PROMPT
    user = messages[1]["content"]
    assert user.startswith("Question: 国外经销商多少家？\n\nPassages:\n")
    for h in HITS:
        assert f"[{h.id}]\n{h.content}" in user
    assert model.schemas == [ca.ANSWER_SCHEMA]


def test_prompt_hash_is_the_preregistered_one():
    prereg = json.loads((S4 / "preregistered.json").read_text())
    assert ca.prompt_sha256() == prereg["prompts"]["cited_answer"]["sha256"]


def test_with_the_local_chat_model_requests_are_thinking_off_and_schema_constrained():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        content = json.dumps(reply("126 家", ["moutai#p17-c3"], True), ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": content}}], "usage": {"prompt_tokens": 80, "completion_tokens": 20}})

    model = OllamaChatModel(transport=httpx.MockTransport(handler))
    result = ca.CitedAnswerer(model).answer("国外经销商多少家？", HITS)
    assert result["found"] is True
    (request,) = seen
    assert request["reasoning_effort"] == "none" and request["temperature"] == 0
    assert request["response_format"]["json_schema"]["schema"] == ca.ANSWER_SCHEMA


def test_default_answerer_is_local_only_and_absent_without_a_server():
    with pytest.raises(ValueError):
        ca.default_answerer("https://api.example.com")
    assert ca.default_answerer("http://127.0.0.1:9") is None  # nothing listens on the discard port
    assert ca.PREFERRED_MODELS[-1] == "qwen3:8b"


def test_results_name_the_model_that_answered():
    assert ca.CitedAnswerer(ScriptedModel(reply("126", ["moutai#p17-c3"], True))).answer("q", HITS)["model"] \
        == "test/scripted"
    assert ca.CitedAnswerer(ScriptedModel("x")).answer("q", [])["model"] is None


def test_a_truncated_reply_is_an_answer_error():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": '{"answer": "1'}}]})

    result = ca.CitedAnswerer(OllamaChatModel(transport=httpx.MockTransport(handler))).answer("q", HITS)
    assert result["status"] == "ANSWER_ERROR" and result["error"] == "OUTPUT_TRUNCATED"


# --------------------------------------------------------------------------- behind the ACL


SCOPE = Scope(tenant_id="t", workspace_id="w", project_id="reports")
OTHER = Scope(tenant_id="t", workspace_id="w", project_id="other")
ADMIN = Principal(subject_id="admin", kind="user", roles=frozenset({"admin", "finance", "board"}),
                  scopes=(SCOPE, OTHER), clearance=3)
ANALYST = Principal(subject_id="analyst", kind="agent", roles=frozenset({"finance"}), scopes=(SCOPE,), clearance=1)
MARKETING = Principal(subject_id="mkt", kind="agent", roles=frozenset({"marketing"}), scopes=(SCOPE,), clearance=3)


@pytest.fixture
def store(tmp_path):
    store = EVTBoundStore(f"sqlite:///{tmp_path / 'qa.sqlite'}")
    store.create_schema()
    store.put_document(ADMIN, SCOPE, "public#p1", "v1", "revenue was 100 million", {"finance"}, 1)
    store.put_document(ADMIN, SCOPE, "board-copy#p1", "v1", "revenue was 100 million (board copy)", {"board"}, 1)
    store.put_document(ADMIN, OTHER, "other-project#p1", "v1", "revenue was 100 million (other project)",
                       {"finance"}, 1)
    yield store
    store.engine.dispose()


def test_the_answerer_only_ever_sees_what_retrieval_returned_for_this_caller(store):
    model = ScriptedModel(reply("100 million", ["public#p1", "board-copy#p1", "other-project#p1"], True))
    answerer = ca.CitedAnswerer(model)
    hits = RankedKnowledgeService(store).search(ANALYST, SCOPE, "revenue", top_k=5)
    assert [h.id for h in hits] == ["public#p1"]
    result = answerer.answer("revenue", hits)
    assert result["citations"] == ["public#p1"]
    assert result["rejected_citations"] == ["board-copy#p1", "other-project#p1"]
    prompt = model.prompts[0][1]["content"]
    assert "board copy" not in prompt and "other project" not in prompt


def test_a_caller_without_access_gets_no_hits_and_no_model_call(store):
    model = ScriptedModel(reply("100 million", ["public#p1"], True))
    answerer = ca.CitedAnswerer(model)
    hits = RankedKnowledgeService(store).search(MARKETING, SCOPE, "revenue", top_k=5)
    assert hits == [] and answerer.answer("revenue", hits)["found"] is False and model.prompts == []


# --------------------------------------------------------------------------- evaluation scoring rules


@pytest.mark.parametrize(
    ("answer", "accept", "matched"),
    [
        ("储能电池销量 121GWh", ["121"], "121"),
        ("储能电池销量 1210GWh", ["121"], None),  # token equality, not substring
        ("储能电池销量 121.5GWh", ["121"], None),
        ("营业收入 168,838,102,514.79 元", ["168,838,102,514.79"], "168,838,102,514.79"),
        ("营业收入 168838102514.79 元", ["168,838,102,514.79"], "168,838,102,514.79"),
        ("每股 5.30 港元", ["5.3"], "5.3"),  # decimal equality
        ("约 1,688.38 亿元", ["1688.38亿"], "1688.38亿"),  # spaces and separators ignored in unit forms
        ("约 1722 亿元", ["722亿", "722 亿"], None),  # a unit form never starts mid-number
        ("about RMB 72.20 billion", ["72.2 billion", "72.20 billion"], "72.20 billion"),
        ("about RMB 72.2 Billion", ["72.2 billion"], "72.2 billion"),
        ("$416.2 billion", ["416 billion"], None),
        ("耗资 893 亿美元", ["89.3", "893亿"], "893亿"),
        ("１２６ 家", ["126"], "126"),  # full-width digits
        ("", ["126"], None),
    ],
)
def test_accept_match(answer, accept, matched):
    assert run_qa.accept_match(answer, accept) == matched


def test_numeric_tokens_and_figures():
    assert run_qa.numeric_tokens("1,303,141.15万元，同比 -34.92%") == ["1303141.15", "34.92"]
    assert run_qa.numeric_tokens("1,2345") == ["1", "2345"]
    assert run_qa.has_number("2025 年的数据未披露") is False
    assert run_qa.has_number("2025 年国外经销商 126 家") is True


def _q(kind, **extra):
    base = {"id": "Q", "kind": kind, "doc": "moutai_2025_annual_zh", "lang": "zh", "query": "q"}
    return base | ({"pages": [6], "accept": ["168,838,102,514.79"]} if kind == "answerable" else {}) | extra


def _res(answer, citations, found=True, status="ANSWERED"):
    return {"answer": answer, "citations": citations, "found": found, "status": status}


def test_answerable_needs_the_figure_and_a_gold_page_citation():
    q = _q("answerable")
    good = run_qa.score_answer(q, _res("168,838,102,514.79 元", ["moutai#p6-t1"]), HITS)
    assert good["correct"] and not good["wrong_but_confident"]
    wrong_page = run_qa.score_answer(q, _res("168,838,102,514.79 元", ["moutai#p17-c3"]), HITS)
    assert not wrong_page["correct"] and wrong_page["right_figure_wrong_citation"]
    assert not wrong_page["wrong_but_confident"]
    wrong_doc = run_qa.score_answer(q, _res("168,838,102,514.79", ["x"]), (hit("x", "", file="catl_2025_annual_zh.pdf"),))
    assert not wrong_doc["correct"]
    confident = run_qa.score_answer(q, _res("170,899,152,276.33 元", ["moutai#p6-t1"]), HITS)
    assert not confident["correct"] and confident["wrong_but_confident"]
    declined = run_qa.score_answer(q, _res("", [], found=False, status="NOT_FOUND"), HITS)
    assert not declined["correct"] and not declined["wrong_but_confident"]


def test_absent_is_correct_only_when_not_found_and_not_an_error():
    q = _q("absent")
    assert run_qa.score_answer(q, _res("", [], False, "NOT_FOUND"), HITS)["correct"]
    assert run_qa.score_answer(q, _res("", [], False, "NO_HITS"), ())["correct"]
    assert not run_qa.score_answer(q, _res("", [], False, "GATE_ERROR"), HITS)["correct"]
    assert not run_qa.score_answer(q, _res("", [], False, "ANSWER_ERROR"), HITS)["correct"]
    hallucinated = run_qa.score_answer(q, _res("约 1,169 元", ["moutai#p22-c1"]), HITS)
    assert not hallucinated["correct"] and hallucinated["wrong_but_confident"]
    worded = run_qa.score_answer(q, _res("报告提到动态调整机制", ["moutai#p22-c1"]), HITS)
    assert not worded["correct"] and not worded["wrong_but_confident"]


def test_the_question_set_and_sources_are_the_registered_ones():
    prereg = run_qa.PREREG
    assert run_qa.sha256(ROOT / prereg["questions"]["path"]) == prereg["questions"]["sha256"]
    assert run_qa.sha256(ROOT / prereg["sources"]["path"]) == prereg["sources"]["sha256"]
    assert len(run_qa.load_questions()) == 40
