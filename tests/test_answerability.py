"""Answerability gate: verdict parsing, literal quotes, never adding hits, fail-closed, ACL order, loopback."""

import json
import sys
from pathlib import Path

import httpx
import pytest

from forgeflow import answerability as ans
from forgeflow.analysis_provider import ProviderInvalid
from forgeflow.contracts import ContextHit, DomainError, Scope
from forgeflow.retrieval import HybridRanker, RankedKnowledgeService, VectorRanker

ROOT = Path(__file__).resolve().parents[1]
EVAL = ROOT / "preparation/s2/evaluation"
S3 = ROOT / "preparation/s3/answerability"
sys.path.insert(0, str(EVAL))
sys.path.insert(0, str(EVAL / "hybrid"))
sys.path.insert(0, str(S3))
import evaluator as ev  # noqa: E402
import run_eval  # noqa: E402
from run_modes import FakeEmbedder  # noqa: E402


def hit(id, content, status="CURRENT"):
    return ContextHit(id=id, version="v1", content=content, source_hash=id * 4, classification=0, status=status)


HITS = (
    hit("evt-boot", "EVT boot cycles minimum 100. Boot failures must be zero."),
    hit("thermal", "EVT thermal peak maximum 70 Celsius."),
    hit("zh-aging", "老化 测试：重复上电并记录失败次数。"),
)


class ScriptedModel:
    """Returns a fixed reply (or raises) and records every prompt it was given."""

    model_id = "test/scripted"

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[list[dict]] = []

    def complete(self, messages, schema):
        self.prompts.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


def verdicts(*items):
    return {"verdicts": [{"passage": p, "answers": a, "quote": q} for p, a, q in items]}


def test_supported_hits_are_a_subset_of_the_input_in_original_order():
    model = ScriptedModel(verdicts((3, True, "记录失败次数"), (1, True, "boot cycles minimum 100"), (2, False, "")))
    result = ans.AnswerabilityGate(model).check("how many boot cycles", HITS)
    assert result.status == "SUPPORTED" and result.answerable
    assert result.kept == (0, 2)
    assert result.hits[0] is HITS[0] and result.hits[1] is HITS[2]  # the same objects, never rebuilt
    assert [v.reason for v in result.verdicts] == ["SUPPORTED", "NOT_ANSWERING", "SUPPORTED"]


def test_a_yes_whose_quote_is_not_literally_in_that_passage_is_rejected():
    model = ScriptedModel(verdicts(
        (1, True, "boot cycles at least 100"),  # paraphrase, not a literal span
        (2, True, "Boot failures must be zero."),  # literal, but from passage 1, not passage 2
        (3, True, "记录"),  # literal but too short to support anything
    ))
    result = ans.AnswerabilityGate(model).check("q", HITS)
    assert result.status == "NO_ANSWER_IN_KNOWLEDGE_BASE" and result.hits == () and result.kept == ()
    assert [v.reason for v in result.verdicts] == ["QUOTE_NOT_IN_PASSAGE", "QUOTE_NOT_IN_PASSAGE", "QUOTE_TOO_SHORT"]
    assert not any(v.accepted for v in result.verdicts)


def test_quote_whitespace_is_trimmed_but_nothing_else_is_normalized():
    ok = ans.parse_verdicts(json.dumps(verdicts((1, True, "  maximum 70 Celsius \n"))), HITS[1:2])
    case = ans.parse_verdicts(json.dumps(verdicts((1, True, "MAXIMUM 70 CELSIUS"))), HITS[1:2])
    assert ok[0].accepted and not case[0].accepted


def test_a_missing_verdict_is_a_no():
    result = ans.AnswerabilityGate(ScriptedModel(verdicts((2, True, "thermal peak maximum 70")))).check("q", HITS)
    assert result.kept == (1,)
    assert [v.reason for v in result.verdicts] == ["NO_VERDICT", "SUPPORTED", "NO_VERDICT"]


@pytest.mark.parametrize(
    "reply, code",
    [
        ("not json", "INVALID_JSON_DOCUMENT"),
        ('{"verdicts": [], "verdicts": []}', "DUPLICATE_JSON_KEY"),
        (json.dumps({"verdicts": [], "answer": "yes"}), "VERDICT_SHAPE"),
        (json.dumps([]), "VERDICT_SHAPE"),
        (json.dumps({"verdicts": [{"passage": 1, "answers": True}]}), "VERDICT_SHAPE"),
        (json.dumps({"verdicts": [{"passage": 1, "answers": "yes", "quote": ""}]}), "VERDICT_TYPES"),
        (json.dumps({"verdicts": [{"passage": True, "answers": True, "quote": ""}]}), "VERDICT_TYPES"),
        (json.dumps(verdicts((4, True, "EVT boot cycles minimum 100"))), "VERDICT_UNKNOWN_PASSAGE"),
        (json.dumps(verdicts((0, True, "EVT boot cycles minimum 100"))), "VERDICT_UNKNOWN_PASSAGE"),
        (json.dumps(verdicts((1, True, "EVT boot cycles"), (1, False, ""))), "VERDICT_DUPLICATE_PASSAGE"),
    ],
)
def test_malformed_output_fails_closed_with_no_hits(reply, code):
    result = ans.AnswerabilityGate(ScriptedModel(reply)).check("q", HITS)
    assert result.status == "GATE_ERROR" and result.error == code
    assert result.hits == () and not result.answerable and result.raw_output == reply


def test_a_model_can_never_add_a_hit_retrieval_did_not_return():
    # A verdict for a passage that does not exist is not "ignored": the whole output is distrusted.
    model = ScriptedModel(verdicts((1, True, "EVT boot cycles minimum 100"), (9, True, "anything at all")))
    result = ans.AnswerabilityGate(model).check("q", HITS[:1])
    assert result.status == "GATE_ERROR" and result.hits == ()
    # And the prompt only ever contains the passages it was handed.
    assert "[2]" not in model.prompts[0][1]["content"]


def test_transport_errors_fail_closed_and_are_not_reported_as_no_answer():
    result = ans.AnswerabilityGate(ScriptedModel(httpx.ConnectError("refused"))).check("q", HITS)
    assert result.status == "GATE_ERROR" and result.error == "ConnectError" and result.hits == ()


def test_empty_retrieval_says_no_answer_without_calling_the_model():
    model = ScriptedModel(AssertionError("must not be called"))
    result = ans.AnswerabilityGate(model).check("What is the humidity limit?", [])
    assert result.status == "NO_ANSWER_IN_KNOWLEDGE_BASE" and result.hits == () and not result.model_called
    assert model.prompts == []


def test_all_no_means_no_answer_in_the_knowledge_base():
    model = ScriptedModel(verdicts((1, False, ""), (2, False, ""), (3, False, "")))
    result = ans.AnswerabilityGate(model).check("What is the maximum humidity during EVT?", HITS)
    assert result.status == "NO_ANSWER_IN_KNOWLEDGE_BASE" and result.hits == () and result.model_called


def test_qwen_empty_think_block_is_not_the_verdict():
    raw = "<think>\n\n</think>\n" + json.dumps(verdicts((2, True, "thermal peak maximum 70 Celsius")))
    assert ans.AnswerabilityGate(ScriptedModel(raw)).check("q", HITS).kept == (1,)


def test_identifier_lookups_keep_only_the_record_carrying_the_identifier_without_a_model_call():
    model = ScriptedModel(AssertionError("must not be called"))
    gate = ans.AnswerabilityGate(model)
    assert gate.check("evt-boot", HITS).kept == (0,)
    assert gate.check("EVT-BOOT", HITS).kept == (0,)
    assert gate.check("no-such-id", HITS).status == "NO_ANSWER_IN_KNOWLEDGE_BASE"
    assert model.prompts == []
    # A question that merely contains an identifier is still a question for the model.
    with pytest.raises(AssertionError):
        gate.check("what does evt-boot require?", HITS)


def test_prompt_is_short_structured_and_numbers_each_passage():
    messages = ans.build_messages("Max temp?", HITS)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"].startswith("Question: Max temp?\n\nPassages:\n[1] EVT boot")
    assert "[3] 老化" in messages[1]["content"]


def test_prompt_hash_is_the_preregistered_one():
    prereg = json.loads((S3 / "preregistered.json").read_text())
    assert ans.prompt_sha256() == prereg["prompt_sha256"]
    assert prereg["min_quote_chars"] == ans.MIN_QUOTE_CHARS and prereg["prompt_version"] == ans.PROMPT_VERSION


# --- the gate sits after the S1 ACL and citation chain -------------------------------------------------


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    data = ev.validate_dataset(json.loads((EVAL / "dataset.json").read_text()))
    store, _ = ev.seed_store(data, "sqlite:///" + str(tmp_path_factory.mktemp("s3") / "eval.db"))
    yield data, store, ev.principals(data)
    store.engine.dispose()


def test_the_gate_only_ever_sees_hits_the_caller_was_authorized_for(frozen):
    data, store, identities = frozen
    heldout = run_eval.validate_heldout(data, json.loads((S3 / "heldout.json").read_text()))
    model = run_eval.FakeChatModel()
    for q in data["questions"] + heldout:
        identity, scope = identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]])
        service = RankedKnowledgeService(store, HybridRanker(VectorRanker(FakeEmbedder(), min_similarity=-1.0)))
        gated = ans.GatedKnowledgeService(service, ans.AnswerabilityGate(model))
        try:
            returned = gated.search(identity, scope, q["query"], 5)
        except DomainError:
            assert gated.gate.seen == [], q["id"]  # a denied caller never reaches the gate
            continue
        expected = service.search(identity, scope, q["query"], 5)
        (query, seen), = gated.gate.seen
        assert query == q["query"] and list(seen) == expected, q["id"]  # exactly what retrieval returned
        readable = {str(row["content"]) for row in store.document_rows(identity, scope)}
        assert {h.content for h in seen} <= readable, q["id"]
        assert all(any(r is s for s in seen) for r in returned), q["id"]


def test_acl_negative_questions_never_show_the_forbidden_source_to_the_model(frozen):
    data, store, identities = frozen
    forbidden = {d["content"] for d in data["documents"] if d["id"] in {"pm-risk", "restricted-cost", "secret-decoy",
                                                                         "private-canary", "pm-schedule"}}
    model = ScriptedModel(verdicts())
    for q in [q for q in data["questions"] if q["principal"] in {"hw-a", "hw-low"}]:
        service = RankedKnowledgeService(store, HybridRanker(VectorRanker(FakeEmbedder(), min_similarity=-1.0)))
        try:
            ans.GatedKnowledgeService(service, ans.AnswerabilityGate(model)).search(
                identities[q["principal"]], Scope.model_validate(data["scopes"][q["scope"]]), q["query"], 5)
        except DomainError:
            pass
    sent = "\n".join(m["content"] for prompt in model.prompts for m in prompt)
    assert model.prompts and not any(text in sent for text in forbidden)


def test_denials_propagate_before_the_gate(frozen):
    data, store, identities = frozen
    gate = ans.AnswerabilityGate(ScriptedModel(AssertionError("must not be called")))
    gated = ans.GatedKnowledgeService(RankedKnowledgeService(store), gate)
    with pytest.raises(DomainError) as denied:
        gated.search(identities["hw-a"], Scope.model_validate(data["scopes"]["aurora-b"]), "evt-boot", 5)
    assert denied.value.code == "NOT_FOUND" and gate.seen == []


def test_heldout_set_is_frozen_and_its_gold_is_current_and_readable(frozen):
    data, _, _ = frozen
    run_eval.verify_heldout()
    questions = run_eval.validate_heldout(data, json.loads((S3 / "heldout.json").read_text()))
    tags = [next(t for t in ("answerable", "unanswerable", "acl-negative") if t in q["tags"]) for q in questions]
    assert (tags.count("answerable"), tags.count("unanswerable"), tags.count("acl-negative")) == (18, 12, 2)


# --- local transport -----------------------------------------------------------------------------------


def test_the_chat_model_refuses_anything_but_a_local_server():
    for url in ["https://api.example.com", "http://10.0.0.5:11434", "http://ollama.internal:11434"]:
        with pytest.raises(ValueError):
            ans.OllamaChatModel(base_url=url)
    for url in ["http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"]:
        assert ans.OllamaChatModel(base_url=url)._client.trust_env is False  # proxies never see loopback


def _transport(seen, finish="stop", content=None):
    def handler(request):
        seen.append(json.loads(request.content))
        body = {"choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": content or json.dumps(verdicts((1, True, "EVT boot cycles minimum 100")))}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_the_chat_model_sends_thinking_off_greedy_schema_constrained_requests():
    seen = []
    model = ans.OllamaChatModel(transport=_transport(seen))
    result = ans.AnswerabilityGate(model).check("How many boot cycles?", HITS[:1])
    assert result.kept == (0,)
    (request,) = seen
    assert request["model"] == "qwen3:8b" and request["reasoning_effort"] == "none"
    assert request["temperature"] == 0 and request["seed"] == 0 and request["stream"] is False
    assert request["response_format"]["json_schema"]["schema"] == ans.VERDICT_SCHEMA
    assert model.calls[0]["finish_reason"] == "stop" and model.calls[0]["reasoning_returned"] is False
    prereg = json.loads((S3 / "preregistered.json").read_text())
    assert model.settings() == prereg["model"]["settings"]


def test_a_truncated_reply_is_a_gate_error():
    model = ans.OllamaChatModel(transport=_transport([], finish="length", content='{"verdicts": ['))
    result = ans.AnswerabilityGate(model).check("q", HITS[:1])
    assert result.status == "GATE_ERROR" and result.error == "OUTPUT_TRUNCATED" and result.hits == ()


def test_parse_verdicts_raises_provider_invalid_for_callers_that_use_it_directly():
    with pytest.raises(ProviderInvalid):
        ans.parse_verdicts("[]", HITS)
