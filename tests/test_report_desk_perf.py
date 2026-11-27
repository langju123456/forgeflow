from threading import Lock

import pytest

from forgeflow.report_desk_perf import percentile_ms, run_query_plan


def test_percentile_uses_nearest_rank_for_small_samples():
    assert percentile_ms([1.0, 2.0, 3.0], 50) == 2.0
    assert percentile_ms([1.0, 2.0, 3.0], 95) == 3.0


def test_query_plan_preserves_each_concurrent_request_question():
    calls = []
    lock = Lock()

    def ask(question):
        with lock:
            calls.append(question)
        return {"question": question, "status": "ANSWERED", "found": True,
                "citations": [{"id": question + "-citation"}]}

    result = run_query_plan(ask, ["first", "second"], repeats=2, workers=3)
    assert result["same_question"]["summary"]["requests"] == 2
    assert result["different_questions"]["summary"]["requests"] == 2
    assert result["concurrent_in_process"]["summary"]["requests"] == 3
    assert [row["question"] for row in result["concurrent_in_process"]["records"]] == ["first", "second", "first"]
    assert all(row["citation_ids"] == [row["question"] + "-citation"]
               for row in result["concurrent_in_process"]["records"])
    assert all(row["kept_ids"] == [] for row in result["concurrent_in_process"]["records"])
    assert len(calls) == 7


def test_echoed_question_cannot_hide_a_crossed_concurrent_answer():
    from threading import current_thread

    def ask(question):
        answer = question if current_thread().name == "MainThread" else "crossed answer"
        return {"question": question, "answer": answer, "found": True}

    with pytest.raises(RuntimeError, match="serial same-question baseline"):
        run_query_plan(ask, ["first", "second"], repeats=2, workers=3)
