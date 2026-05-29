"""
Regression tests for the MCP server's stateless HTTP configuration.

Background: the backend runs on Cloud Run with --max-instances > 1 and per-request
load balancing. FastMCP's default stateful mode keeps the MCP session (Mcp-Session-Id)
in memory on the single instance that handled `initialize`, so a follow-up tool call
routed to a different instance found no session and returned 404 ("Session terminated").

The fix is stateless_http=True: each request is fully self-contained and any instance
can serve it. These tests lock that configuration in place.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def test_mcp_server_is_configured_stateless():
    """The MCP server must run stateless so no instance affinity is required.

    If this flips back to False, tool calls will intermittently 404 on Cloud Run
    whenever a request lands on an instance that didn't create the session.
    """
    from src.handlers.mcp import mcp_server

    assert mcp_server.settings.stateless_http is True
    assert mcp_server.settings.json_response is True


@pytest.mark.asyncio
async def test_tool_call_without_session_id_is_served():
    """A tools/call carrying no Mcp-Session-Id must be served, not rejected.

    This simulates the real failure: a follow-up request routed to a fresh Cloud Run
    instance that never saw the `initialize` handshake. In stateful mode the server
    rejects this with a 4xx ("Missing session ID"); in stateless mode it processes it.
    """
    from src.handlers import mcp as mcp_module

    app = mcp_module._build_mcp_asgi_app()

    fake_execute = AsyncMock(return_value={"ok": True, "result": "stub"})

    with patch.object(mcp_module, "decode_firebase_claims", return_value={"uid": "test-uid"}), \
         patch.object(mcp_module.ToolExecutor, "execute", fake_execute):
        async with mcp_module.mcp_server.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                headers = {
                    "Authorization": "Bearer fake-token",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                }
                resp = await client.post(
                    "/",
                    headers=headers,
                    content=json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "web_surf", "arguments": {"query": "test"}},
                    }),
                )

    # The bug surfaced as a 404. Statelessness means the request is served (200).
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    fake_execute.assert_awaited_once()
    tool_name = fake_execute.await_args.args[0]
    assert tool_name == "web_surf"
