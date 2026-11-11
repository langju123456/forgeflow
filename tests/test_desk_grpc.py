"""The gRPC retrieval server over the synthetic report PDFs: same evidence as the in-process desk, identity from
metadata only, typed failures the worker can classify (UNAVAILABLE is transient, the rest final)."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("grpc")
pytest.importorskip("pdfplumber")
pytest.importorskip("reportlab")

import grpc  # noqa: E402

from forgeflow.desk_service.proto import desk_pb2, desk_pb2_grpc  # noqa: E402
from forgeflow.desk_service.retrieval_server import (  # noqa: E402
    SUBJECT_HEADER,
    RetrievalServicer,
    build_server,
)
from forgeflow.report_desk import ANALYST, ReportDesk  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from desk_service_harness import free_port, make_reports  # noqa: E402

ME = ((SUBJECT_HEADER, ANALYST.subject_id),)
# grpc turns a timeout into an absolute deadline from time.time() for its C core: keep the real clock.
pytestmark = pytest.mark.real_clock


@pytest.fixture(scope="module")
def desk(tmp_path_factory):
    folder = tmp_path_factory.mktemp("reports")
    sources = make_reports(folder)
    desk = ReportDesk(folder, tmp_path_factory.mktemp("work"), ollama_url="http://127.0.0.1:9", sources=sources)
    desk.load()
    return desk


def _serve(desk):
    target = f"127.0.0.1:{free_port()}"
    server, _ = build_server(RetrievalServicer(desk), target)
    server.start()
    channel = grpc.insecure_channel(target)
    return server, channel, desk_pb2_grpc.ReportRetrievalStub(channel)


@pytest.fixture(scope="module")
def stub(desk):
    server, channel, stub = _serve(desk)
    yield stub
    channel.close()
    server.stop(None)


def test_search_returns_the_same_evidence_as_the_in_process_desk(desk, stub):
    reply = stub.Search(desk_pb2.SearchRequest(question="total net sales", top_k=5), metadata=ME, timeout=10)
    local = desk.evidence("total net sales", top_k=5)
    assert [h.id for h in reply.kept] == [h.id for h in local["kept"]] and reply.kept
    first = reply.kept[0]
    assert first.content == local["kept"][0].content and first.citation.page >= 1 and len(first.citation.bbox) == 4
    assert reply.index_version == desk.index_version() and reply.mode == "lexical" and reply.gate_status == "OFF"


@pytest.mark.parametrize("metadata", [(), ((SUBJECT_HEADER, "intruder"),)])
def test_identity_comes_from_metadata_and_unknown_callers_are_refused(stub, metadata):
    with pytest.raises(grpc.RpcError) as error:
        stub.Search(desk_pb2.SearchRequest(question="total net sales"), metadata=metadata, timeout=10)
    assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.parametrize("request_", [desk_pb2.SearchRequest(question="   "),
                                      desk_pb2.SearchRequest(question="sales", top_k=50)])
def test_bad_requests_are_final_errors(stub, request_):
    with pytest.raises(grpc.RpcError) as error:
        stub.Search(request_, metadata=ME, timeout=10)
    assert error.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_a_server_still_loading_says_unavailable(tmp_path):
    folder = tmp_path / "reports"
    folder.mkdir()
    loading = ReportDesk(folder, tmp_path / "work", ollama_url="http://127.0.0.1:9", sources=make_reports(folder))
    server, channel, stub = _serve(loading)
    try:
        with pytest.raises(grpc.RpcError) as error:
            stub.Search(desk_pb2.SearchRequest(question="sales"), metadata=ME, timeout=10)
        assert error.value.code() == grpc.StatusCode.UNAVAILABLE  # the worker retries this one
        info = stub.Info(desk_pb2.InfoRequest(), metadata=ME, timeout=10)
        assert info.state == "starting" and info.index_version == ""
    finally:
        channel.close()
        server.stop(None)


def test_info_and_page_image(stub, desk):
    info = stub.Info(desk_pb2.InfoRequest(), metadata=ME, timeout=10)
    assert info.state == "ready" and info.index_version == desk.index_version() and info.rows > 0
    assert "path" not in info.reports_json
    hit = stub.Search(desk_pb2.SearchRequest(question="total net sales"), metadata=ME, timeout=10).kept[0]
    png = stub.PageImage(desk_pb2.PageImageRequest(doc=hit.citation.doc, page=hit.citation.page,
                                                   bbox=hit.citation.bbox), metadata=ME, timeout=20).png
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(grpc.RpcError) as error:
        stub.PageImage(desk_pb2.PageImageRequest(doc="nope", page=1), metadata=ME, timeout=10)
    assert error.value.code() == grpc.StatusCode.NOT_FOUND


def test_index_version_tracks_what_retrieval_can_return(desk):
    before = desk.index_version()
    desk.mode = "hybrid"
    try:
        assert desk.index_version() != before  # same rows, other ranking: cached answers must not carry over
    finally:
        desk.mode = "lexical"
    assert desk.index_version() == before
