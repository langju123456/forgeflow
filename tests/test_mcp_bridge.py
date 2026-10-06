import asyncio

import pytest
from test_sandbox import spec

from forgeflow.contracts import Principal, Scope
from forgeflow.mcp_bridge import MCPBridge


def principal(role="hardware", scope=None):
    return Principal(subject_id=role, kind="agent", roles={role}, scopes=(scope or spec().scope,))


@pytest.mark.asyncio
async def test_real_mcp_handshake_hidden_tool_direct_denial():
    result, transcript = await MCPBridge().call(principal("pm"), spec(), "sandbox.execute", {})
    assert result["error"] == "DENIED"
    assert transcript["transport"] == "stdio"
    assert transcript["independent_server_process"] is True
    assert transcript["tools"] == []
    methods = [x.get("message", {}).get("method") for x in transcript["messages"]]
    assert [m for m in methods if m] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert transcript["protocol_version"] == "2025-11-25"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"template_id": "evt-check", "input_manifest_id": "wrong", "resource_profile": "checker-v1"},
        {
            "template_id": "evt-check",
            "input_manifest_id": spec().input_hash,
            "resource_profile": "checker-v1",
            "tenant_id": "other",
        },
        {
            "template_id": "evt-check",
            "input_manifest_id": spec().input_hash,
            "resource_profile": "network-on",
        },
    ],
)
async def test_structured_arguments_cannot_change_identity_or_resource(arguments):
    result, _ = await MCPBridge().call(principal(), spec(), "sandbox.execute", arguments)
    assert result["error"] == "DENIED"


@pytest.mark.asyncio
async def test_scoped_session_wrong_project_denied():
    other = Scope(tenant_id="t", workspace_id="w", project_id="other")
    result, transcript = await MCPBridge().call(principal(scope=other), spec(), "sandbox.execute", {})
    assert result["error"] == "DENIED"
    assert transcript["tools"] == []


@pytest.mark.asyncio
async def test_missing_scan_never_executes():
    request = spec()
    result, transcript = await MCPBridge().call(
        principal(),
        request,
        "sandbox.execute",
        {
            "template_id": request.template_id,
            "input_manifest_id": request.input_hash,
            "resource_profile": "checker-v1",
        },
    )
    assert result["error"] == "DENIED"
    assert transcript["tools"] == ["sandbox.execute"]
    assert "scan receipt" in result["reason"]


@pytest.mark.asyncio
async def test_concurrent_independent_sessions_cannot_switch_scope_or_role():
    first = spec()
    second_scope = Scope(tenant_id="tenant-b", workspace_id="w", project_id="aurora")
    second = first.model_copy(update={"scope": second_scope, "input_hash": "different-b-input"})
    calls = [
        MCPBridge().call(
            principal("hardware"),
            first,
            "sandbox.execute",
            {
                "template_id": first.template_id,
                "input_manifest_id": second.input_hash,
                "resource_profile": "checker-v1",
            },
        ),
        MCPBridge().call(
            principal("pm", second_scope),
            second,
            "sandbox.execute",
            {
                "template_id": second.template_id,
                "input_manifest_id": second.input_hash,
                "resource_profile": "checker-v1",
                "role": "sre",
                "tenant_id": "t",
            },
        ),
    ]
    (a, a_transcript), (b, b_transcript) = await asyncio.gather(*calls)
    assert a["error"] == b["error"] == "DENIED"
    assert a_transcript["tools"] == ["sandbox.execute"]
    assert b_transcript["tools"] == []
    assert a_transcript["binding"]["scope"] == first.scope.model_dump()
    assert b_transcript["binding"]["scope"] == second_scope.model_dump()
    assert a_transcript["binding"]["subject_id"] == "hardware"
    assert b_transcript["binding"]["subject_id"] == "pm"
