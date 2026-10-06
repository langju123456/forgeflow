"""Metric/protocol integrity tests; never tune the heldout scorer or rewrite gold."""

from __future__ import annotations

import copy
import json

import evaluator
import pytest
from evaluator import (
    ROOT,
    RecordingStore,
    latency_summary,
    principals,
    rate,
    ref_key,
    retrieval_scores,
    row_authorized,
    sha256,
    summarize,
    validate_dataset,
)


@pytest.fixture
def data():
    return json.loads((ROOT / "dataset.json").read_text())


def test_dataset_partition_and_adversarial_controls(data):
    validate_dataset(data)
    assert len(data["questions"]) == 47
    assert sum(bool(q["relevant"]) for q in data["questions"]) == 37
    assert sum("unanswerable" in q["tags"] for q in data["questions"]) == 3
    assert sum("acl-negative" in q["tags"] for q in data["questions"]) == 7
    assert sum("acl-control" in q["tags"] for q in data["questions"]) == 7
    assert all(q["split"] == "heldout" for q in data["questions"])


@pytest.mark.parametrize(
    "mutation",
    [
        "content_hash",
        "gold_hash",
        "superseded_gold",
        "unauthorized_gold",
        "future_expiry",
        "missing_twin",
        "duplicate_qid",
        "too_few",
    ],
)
def test_invalid_data_fails_before_retrieval(data, mutation):
    broken = copy.deepcopy(data)
    if mutation == "content_hash":
        broken["documents"][0]["content"] = "silently changed"
    elif mutation == "gold_hash":
        broken["questions"][0]["relevant"][0]["source_hash"] = "0" * 64
    elif mutation == "superseded_gold":
        old = next(d for d in broken["documents"] if d["id"] == "npi-bom" and d["version"] == "v1")
        broken["questions"][7]["relevant"][0].update(version="v1", source_hash=old["source_hash"])
    elif mutation == "unauthorized_gold":
        broken["questions"][33]["principal"] = "hw-a"
    elif mutation == "future_expiry":
        broken["documents"][0]["valid_until"] = "2099-01-01T00:00:00Z"
    elif mutation == "missing_twin":
        broken["questions"][32]["counterpart"] = "Q99"
    elif mutation == "duplicate_qid":
        broken["questions"][1]["id"] = "Q01"
    elif mutation == "too_few":
        broken["questions"] = broken["questions"][:29]
    with pytest.raises(ValueError):
        validate_dataset(broken)


def test_recall_mrr_use_exact_refs_and_deduplicate():
    a = ("scope", "a", "v2", "hash-a")
    b = ("scope", "b", "v1", "hash-b")
    old = ("scope", "a", "v1", "hash-old")
    foreign = ("foreign", "a", "v2", "hash-a")
    measured = retrieval_scores([old, foreign, a, a, ("scope", "irrelevant", "v1", "x")], {a, b})
    assert measured == {
        "recall_at_5": 0.5,
        "reciprocal_rank_at_5": 1 / 3,
        "relevant_found": 1,
        "relevant_total": 2,
    }
    assert retrieval_scores([old, foreign], {a})["reciprocal_rank_at_5"] == 0
    assert retrieval_scores([old] * 5 + [a], {a})["recall_at_5"] == 0


def test_empty_gold_and_empty_citation_denominators_are_na():
    assert retrieval_scores([("valid", "irrelevant", "v1", "h")], set())["recall_at_5"] is None
    assert rate(0, 0) == {"numerator": 0, "denominator": 0, "rate": None}


def result(id, *, gold=True, tags=(), leak=False, hit=True, current=True):
    return {
        "id": id,
        "tags": list(tags),
        "metrics": {
            "relevant_total": 1 if gold else 0,
            "relevant_found": 1 if gold and hit else 0,
            "recall_at_5": 1.0 if gold and hit else 0.0 if gold else None,
            "reciprocal_rank_at_5": 1.0 if gold and hit else 0.0 if gold else None,
        },
        "citation_checks": [
            {
                "resolves": True,
                "exact_authorized": not leak,
                "current_usable": current,
                "status_correct": True,
            }
        ]
        if hit
        else [],
        "gold_status_correct": int(gold and hit),
        "leak": leak,
        "forbidden_candidate_rows": int(leak),
        "forbidden_citation_rows": 0,
        "unknown_returned_sources": 0,
        "hits": [{}] if hit else [],
        "conflict_required": False,
        "conflict_flag_correct": None,
        "latency_ms": [2.0],
        "stable": True,
        "expected_denial": False,
        "denial_correct": True,
    }


def test_aggregate_separates_retrieval_acl_expiry_and_irrelevant_context():
    rows = [
        result("positive"),
        result("expired", current=False),
        result("positive-miss", hit=False),
        result("control", tags=["acl-control"]),
        result("negative-leak", gold=False, tags=["acl-negative"], leak=True),
        result("negative-clean", gold=False, tags=["acl-negative"], hit=False),
        result("unanswerable", gold=False, tags=["unanswerable"]),
    ]
    summary = summarize(rows)
    assert summary["positive_questions"] == 4
    assert summary["core_content_questions"] == 3
    assert summary["macro_recall_at_5"] == 0.75
    assert summary["core_content_recall_at_5"] == pytest.approx(2 / 3)
    assert summary["acl_negative_query_leak"] == rate(1, 2)
    assert summary["all_query_leak"] == rate(1, 7)
    assert summary["unanswerable_nonempty_context"] == rate(1, 1)
    assert summary["citation_resolves"] == rate(5, 5)
    assert summary["citation_exact_authorized"] == rate(4, 5)
    assert summary["returned_citation_current_usable"] == rate(4, 5)
    assert summary["expected_status_gold_coverage"] == rate(3, 4)


def test_acl_metadata_validation_includes_every_full_scope_axis(data):
    principal = principals(data)["hw-a"]
    scope = principal.scopes[0]
    row = {**scope.model_dump(), "roles": ["hardware"], "classification": 0}
    assert row_authorized(row, principal, scope)
    for field in ["tenant_id", "workspace_id", "project_id"]:
        assert not row_authorized({**row, field: "wrong"}, principal, scope)
    assert not row_authorized({**row, "roles": ["pm"]}, principal, scope)
    assert not row_authorized({**row, "classification": 3}, principal, scope)


def test_recording_store_is_transparent_and_never_post_filters():
    raw = [{"id": "permitted"}, {"id": "forbidden"}]

    class Port:
        def document_rows(self, *args):
            return raw

        def cite(self, *args):
            return raw[1]

    wrapper = RecordingStore(Port())
    assert wrapper.document_rows(None, None) is raw
    assert wrapper.cite(None, None, None, None) is raw[1]
    assert wrapper.rows == raw and wrapper.citations == [raw[1]]
    wrapper.reset()
    assert wrapper.rows == wrapper.citations == []


def test_nearest_rank_percentile_has_explicit_denominator():
    summary = latency_summary(list(range(1, 101)))
    assert summary["n"] == 100 and summary["median_ms"] == 50.5 and summary["p95_ms"] == 95
    assert latency_summary([])["p95_ms"] is None


def test_unknown_source_hash_does_not_match_gold(data):
    reference = data["questions"][0]["relevant"][0]
    changed = {**reference, "source_hash": "0" * 64}
    assert ref_key(changed) != ref_key(reference)


def test_run_label_cannot_write_outside_preparation():
    with pytest.raises(ValueError, match="Run label invalid"):
        evaluator.run_baseline("../outside")


def test_frozen_file_digest_tamper_is_rejected(tmp_path, monkeypatch):
    protected = {"manifest_sha256": "fixed", "files_checked": 1, "scorer_sha256": {"x": "y"}}
    sample = tmp_path / "dataset.json"
    sample.write_text("original")
    (tmp_path / "freeze.json").write_text(
        json.dumps(
            {
                "protocol_version": evaluator.PROTOCOL_VERSION,
                "files": {"dataset.json": sha256(sample)},
                "protected_s1": protected,
            }
        )
    )
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setattr(evaluator, "verify_s1", lambda: protected)
    assert evaluator.verify_freeze()["protected_s1"] == protected
    sample.write_text("tampered")
    with pytest.raises(ValueError, match="Frozen benchmark changed"):
        evaluator.verify_freeze()


def fake_question_run(
    data, q, *, duplicate=False, foreign_origin=False, late_forbidden=False, forged_classification=False
):
    from forgeflow.contracts import ContextHit, DomainError, Scope

    ref = q["relevant"][0]
    document = next(
        d
        for d in data["documents"]
        if (d["scope"], d["id"], d["version"]) == (ref["scope"], ref["id"], ref["version"])
    )
    scope = Scope.model_validate(data["scopes"][q["scope"]])
    metadata = {
        **document,
        **scope.model_dump(),
        "citation_id": "expected-version-uuid",
        "status": ref["status"],
    }
    hit = ContextHit(
        id=document["id"],
        version=document["version"],
        content=document["content"],
        source_hash=document["source_hash"],
        classification=0 if forged_classification else document["classification"],
        status=ref["status"],
    )
    origin = (
        {**metadata, "tenant_id": "eval-b", "citation_id": "foreign-version-uuid"}
        if foreign_origin
        else metadata
    )

    class Port:
        def cite(self, _principal, _scope, _kind, identifier):
            if identifier != "expected-version-uuid":
                raise DomainError("NOT_FOUND", status=404)
            return metadata

    port = Port()
    trace = RecordingStore(port)

    class ScriptedReader:
        calls = 0

        def search(self, *_args, **_kwargs):
            self.calls += 1
            trace.rows = [origin]
            if late_forbidden and self.calls == 3:
                trace.rows.append({**metadata, "roles": ["pm"]})
            trace.citations = [origin] * (2 if duplicate else 1)
            return [hit] * (2 if duplicate else 1)

    mapping = {ref_key(ref): "expected-version-uuid"}
    return evaluator.evaluate_question(
        data, q, port, mapping, ScriptedReader(), trace, principals(data)[q["principal"]]
    )


def test_security_traces_include_later_repetitions_even_when_hits_stable(data):
    measured = fake_question_run(data, data["questions"][0], late_forbidden=True)
    assert measured["stable"] is True
    assert measured["leak"] is True and measured["forbidden_candidate_rows"] == 1
    assert measured["candidate_row_observations"] == 6


def test_foreign_samehash_origin_cannot_be_blessed_by_expected_seed(data):
    measured = fake_question_run(data, data["questions"][0], foreign_origin=True)
    assert measured["hits"] and measured["hit_origin_scopes"] == ["aurora-b"]
    assert measured["metrics"]["recall_at_5"] == 0
    assert measured["leak"] is True
    assert measured["citation_checks"][0]["exact_authorized"] is False
    assert measured["citation_checks"][0]["resolves"] is False


def test_duplicate_gold_hit_cannot_inflate_expected_status_coverage(data):
    measured = fake_question_run(data, data["questions"][0], duplicate=True)
    assert measured["metrics"]["recall_at_5"] == 1
    assert measured["gold_status_correct"] == 1
    assert len(measured["citation_checks"]) == 2


def test_forged_lower_hit_classification_fails_provenance_check(data):
    measured = fake_question_run(data, data["questions"][34], forged_classification=True)
    assert measured["citation_checks"][0]["resolves"] is True
    assert measured["citation_checks"][0]["exact_authorized"] is False
    assert measured["citation_checks"][0]["current_usable"] is False
