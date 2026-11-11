"""The desk service end to end with real Redis, RabbitMQ, gRPC and HTTP; only the model is a stand-in.

Off by default like the other live tests: ``FORGEFLOW_SERVICE_LIVE=1`` with Redis on 127.0.0.1:6379 and RabbitMQ
on 127.0.0.1:5672 (``docker compose -f compose.desk-service.yaml up -d``). Each test brings up its own queues,
gRPC port, gateway port and model port, so they do not see each other's jobs.
"""

import concurrent.futures
import os
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("grpc")
pytest.importorskip("aio_pika")
pytest.importorskip("redis")
pytest.importorskip("pdfplumber")

sys.path.insert(0, str(Path(__file__).parent))
from desk_service_harness import Harness, services_up, with_config  # noqa: E402

pytestmark = [
    pytest.mark.real_clock,  # timestamps cross process boundaries (gRPC deadlines, job records)
    pytest.mark.skipif(os.environ.get("FORGEFLOW_SERVICE_LIVE") != "1" or not services_up(),
                       reason="set FORGEFLOW_SERVICE_LIVE=1 with Redis and RabbitMQ on loopback"),
]

Q = "total net sales"


@pytest.fixture
def service(tmp_path):
    harness = Harness(tmp_path)
    harness.start_model()
    harness.start_retrieval()
    harness.start_gateway()
    yield harness
    harness.close()


def kinds(events):
    return [e["type"] for e in events]


def test_one_question_streams_stages_then_a_checked_answer(service):
    service.start_workers(1)
    accepted = service.ask(Q)
    assert accepted.status_code == 202 and accepted.json()["state"] == "queued"
    events = service.events(accepted.json()["job_id"])
    names = kinds(events)
    assert names[:3] == ["queued", "started", "retrieved"] and names[-1] == "final"
    assert "token" in names and set(names[3:-1]) == {"token"}
    final = events[-1]["data"]
    assert final["status"] == "ANSWERED" and final["found"] is True
    draft = "".join(e["data"]["text"] for e in events if e["type"] == "token")
    assert draft.strip() == final["answer"]  # nothing was withdrawn (cited_answer strips outer spaces)
    assert {c["id"] for c in final["citations"]} <= {c["id"] for c in final["kept"]}
    assert final["timings"]["first_token_ms"] <= final["timings"]["answer_ms"]
    job = service.job(accepted.json()["job_id"])
    assert job["status"] == "done" and job["deliveries"] == 1 and job["result"]["answer"] == final["answer"]


def test_a_reconnecting_reader_resumes_after_the_last_event_it_saw(service):
    service.start_workers(1)
    job_id = service.ask(Q).json()["job_id"]
    everything = service.events(job_id)
    resumed = service.events(job_id, after=everything[2]["id"])
    assert [e["id"] for e in resumed] == [e["id"] for e in everything[3:]]


def test_a_repeated_question_is_answered_from_cache_without_the_model(service):
    service.start_workers(1)
    first = service.events(service.ask(Q).json()["job_id"])[-1]["data"]
    calls = service.model.calls
    again = service.ask(f"  {Q.replace(' ', '   ')} ")
    assert again.status_code == 200 and again.json()["state"] == "cached"
    events = service.events(again.json()["job_id"])
    assert kinds(events) == ["final"] and events[0]["data"]["cached"] is True
    assert events[0]["data"]["answer"] == first["answer"] and service.model.calls == calls
    # The gate changes the evidence, so it is a different key and a new job.
    assert service.ask(Q, gate=True).json()["state"] == "queued"


def test_identical_questions_in_flight_share_one_job_and_one_model_call(service):
    service.model.chunk_delay_s = 0.05  # long enough that all requests arrive while the first is running
    service.start_workers(1)
    with concurrent.futures.ThreadPoolExecutor(10) as pool:
        replies = [r.json() for r in pool.map(lambda _: service.ask("operating income"), range(10))]
    assert len({r["job_id"] for r in replies}) == 1
    assert sorted(r["state"] for r in replies) == ["joined"] * 9 + ["queued"]
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        streams = list(pool.map(lambda _: service.events(replies[0]["job_id"]), range(3)))
    assert all(s[-1]["type"] == "final" for s in streams) and service.model.calls == 1


def test_no_evidence_means_no_model_call(service):
    service.start_workers(1)
    final = service.events(service.ask("zzqx wvvy").json()["job_id"])[-1]["data"]
    assert final["status"] == "NO_HITS" and final["found"] is False and service.model.calls == 0


def test_a_streamed_draft_citing_something_it_was_not_given_is_withdrawn(service):
    service.model.mode = "made_up"
    service.start_workers(1)
    events = service.events(service.ask(Q).json()["job_id"])
    assert any(e["type"] == "token" for e in events)  # the reader saw a draft...
    final = events[-1]["data"]
    assert final["status"] == "UNSUPPORTED" and final["found"] is False and final["answer"] == ""
    assert final["rejected_citations"] == ["not-a-given-passage"]  # ...that the checks then refused


def test_unusable_model_output_is_reported_and_not_cached(service):
    service.model.mode = "garbage"
    service.start_workers(1)
    final = service.events(service.ask(Q).json()["job_id"])[-1]["data"]
    assert final["status"] == "ANSWER_ERROR" and final["found"] is False
    assert service.ask(Q).json()["state"] == "queued"  # not served from cache


def test_a_model_outage_is_retried_and_the_job_completes_when_it_recovers(service):
    service.model.down_calls = 1
    service.start_workers(1)
    events = service.events(service.ask(Q).json()["job_id"])
    assert kinds(events).count("started") == 2 and "retrying" in kinds(events)
    assert events[-1]["type"] == "final" and events[-1]["data"]["status"] == "ANSWERED"
    assert events[-1]["data"]["attempt"] == 2


def test_after_max_attempts_the_job_fails_into_the_dead_letter_queue(service):
    service.model.mode = "down"
    service.start_workers(1)
    job_id = service.ask(Q).json()["job_id"]
    events = service.events(job_id)
    assert kinds(events).count("started") == service.config.max_attempts
    assert events[-1]["type"] == "error" and events[-1]["data"]["code"] == "GAVE_UP"
    deadline = time.monotonic() + 5
    while service.queue_state(service.config.dead_queue)[0] < 1 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert service.queue_state(service.config.dead_queue)[0] == 1
    service.model.mode = "ok"
    retry = service.ask(Q).json()  # the in-flight marker was released: a new job, not a join to a dead one
    assert retry["state"] == "queued" and retry["job_id"] != job_id


def test_a_short_retrieval_restart_is_waited_out_within_the_deadline(service):
    service.start_workers(1)
    service.stop_retrieval()
    job_id = service.ask(Q).json()["job_id"]
    time.sleep(0.5)
    service.restart_retrieval()
    events = service.events(job_id)
    assert "retrying" not in kinds(events) and events[-1]["data"]["status"] == "ANSWERED"


def test_a_retrieval_outage_longer_than_the_deadline_is_retried(tmp_path):
    harness = Harness(tmp_path)
    with_config(harness, grpc_deadline_s=1.0)
    harness.start_model()
    harness.start_retrieval()
    harness.start_gateway()
    try:
        harness.start_workers(1)
        harness.stop_retrieval()
        job_id = harness.ask(Q).json()["job_id"]
        time.sleep(1.6)  # past the first attempt's deadline
        harness.restart_retrieval()
        events = harness.events(job_id)
        retry = next(e for e in events if e["type"] == "retrying")
        assert "DEADLINE_EXCEEDED" in retry["data"]["reason"]
        assert events[-1]["data"]["status"] == "ANSWERED" and events[-1]["data"]["attempt"] == 2
    finally:
        harness.close()


def test_a_worker_killed_mid_answer_loses_nothing(service):
    service.model.chunk_delay_s = 0.4  # the answer takes several seconds: time to kill the worker inside it
    doomed = service.start_worker_process()
    job_id = service.ask(Q).json()["job_id"]
    deadline = time.monotonic() + 20
    while service.model.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    doomed.kill()  # SIGKILL: no ack, no cleanup; RabbitMQ requeues the job when the connection drops
    doomed.wait()
    service.model.chunk_delay_s = 0.01
    service.start_workers(1)
    events = service.events(job_id)
    assert events[-1]["type"] == "final" and events[-1]["data"]["status"] == "ANSWERED"
    starts = [e for e in events if e["type"] == "started"]
    assert len(starts) == 2 and starts[0]["data"]["worker"] != starts[1]["data"]["worker"]
    assert service.job(job_id)["deliveries"] == 2


def test_a_full_queue_pushes_back_instead_of_accepting_work(tmp_path):
    harness = Harness(tmp_path, max_queue=2)
    harness.start_model()
    harness.start_retrieval()
    harness.start_gateway()  # no workers: nothing drains the queue
    try:
        replies = [harness.ask(f"question number {i}") for i in range(4)]
        assert [r.status_code for r in replies] == [202, 202, 429, 429]
        assert replies[2].headers["retry-after"] == "5" and replies[2].json()["detail"]["code"] == "QUEUE_FULL"
        assert harness.queue_state()[0] == 2
        # The refused question was not left registered as in flight.
        harness.start_workers(1)
        assert harness.ask("question number 2").json()["state"] == "queued"
    finally:
        harness.close()


def test_questions_before_the_index_is_ready_get_503(tmp_path):
    harness = Harness(tmp_path)
    harness.start_model()
    harness.start_retrieval(load=False)
    harness.start_gateway()
    try:
        reply = harness.ask(Q)
        assert reply.status_code == 503 and reply.json()["detail"]["code"] == "DESK_NOT_READY"
    finally:
        harness.close()


def test_reports_info_shows_queue_and_workers(service):
    service.start_workers(2)
    import httpx

    info = httpx.get(f"{service.url}/v2/reports", timeout=10, trust_env=False).json()
    assert info["service"] is True and info["status"]["state"] == "ready"
    assert info["queue"]["workers"] == 2 and info["queue"]["max"] == service.config.max_queue
    cite = service.events(service.ask(Q).json()["job_id"])[-1]["data"]["citations"][0]
    image = httpx.get(f"{service.url}/v2/reports/{cite['doc']}/pages/{cite['page']}.png", timeout=20,
                      trust_env=False)
    assert image.status_code == 200 and image.content[:4] == b"\x89PNG"
