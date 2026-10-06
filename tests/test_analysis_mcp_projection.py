"""Offline validation of archived real MCP receipts; no new protocol execution."""

import copy
import json
from pathlib import Path

import pytest

from forgeflow.analysis_context import _mcp_projection
from forgeflow.contracts import Scope, canonical

ARCHIVE = Path(__file__).resolve().parents[1] / "deliveries/2026-10-05/implementation-evidence"


def receipt(fixture="pass"):
    data = json.loads((ARCHIVE / f"chain-{fixture}.json").read_text())
    return Scope.model_validate(data["scope"]), data["run"]


@pytest.mark.parametrize("fixture", ["pass", "blocked", "unknown", "timeout"])
def test_archived_real_handshake_and_tool_receipt_has_no_status_field(fixture):
    scope, run = receipt(fixture)
    assert "status" not in run["result_json"]["mcp"]
    assert _mcp_projection(scope, run["id"], run) == "RECORDED"


@pytest.mark.parametrize("corruption", ["scope", "missing_call", "hash", "response_id", "persisted_result"])
def test_incomplete_foreign_or_changed_receipt_is_unknown(corruption):
    scope, source = receipt()
    run = copy.deepcopy(source)
    proof = run["result_json"]["mcp"]
    if corruption == "scope":
        proof["binding"]["scope"]["tenant_id"] = "other-tenant"
    elif corruption == "missing_call":
        proof["messages"] = [
            row for row in proof["messages"] if row.get("message", {}).get("method") != "tools/call"
        ]
    elif corruption == "hash":
        proof["messages"][0]["sha256"] = "0" * 64
    elif corruption == "response_id":
        proof["messages"][-1]["message"]["id"] = "unmatched-response"
    else:
        run["result_json"]["cleanup"] = "UNKNOWN"
    assert _mcp_projection(scope, run["id"], run) == "UNKNOWN"


def test_protocol_recording_does_not_replace_hardware_gate_or_driver_label():
    scope, run = receipt("unknown")
    assert _mcp_projection(scope, run["id"], run) == "RECORDED"
    assert run["assessment_json"]["status"] == "UNKNOWN"
    run["result_json"]["mcp"] = {"status": "TEST_DRIVER"}
    assert _mcp_projection(scope, run["id"], run) == "TEST_DRIVER"


@pytest.mark.parametrize("corruption", ["response_before_request", "unrequested_response"])
def test_self_hashed_but_impossible_response_witness_is_unknown(corruption):
    import hashlib

    scope, source = receipt()
    run = copy.deepcopy(source)
    messages = run["result_json"]["mcp"]["messages"]
    if corruption == "response_before_request":
        messages.insert(-1, messages.pop())
    else:
        message = {"jsonrpc": "2.0", "id": "never-requested", "result": {}}
        raw = canonical(message).encode()
        messages.append(
            {
                "direction": "server_to_client",
                "message": message,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    assert _mcp_projection(scope, run["id"], run) == "UNKNOWN"


@pytest.mark.parametrize("corruption", ["notification_id", "request_result", "response_method"])
def test_valid_hash_does_not_make_invalid_jsonrpc_semantics_recorded(corruption):
    import hashlib

    scope, source = receipt()
    run = copy.deepcopy(source)
    messages = run["result_json"]["mcp"]["messages"]
    if corruption == "notification_id":
        entry = next(row for row in messages if row["message"].get("method") == "notifications/initialized")
        entry["message"]["id"] = "illegal-notification-id"
    elif corruption == "request_result":
        entry = next(row for row in messages if row["message"].get("method") == "tools/call")
        entry["message"]["result"] = {}
    else:
        entry = messages[-1]
        entry["message"]["method"] = "tools/call"
    raw = canonical(entry["message"]).encode()
    entry["sha256"], entry["bytes"] = hashlib.sha256(raw).hexdigest(), len(raw)
    assert _mcp_projection(scope, run["id"], run) == "UNKNOWN"
