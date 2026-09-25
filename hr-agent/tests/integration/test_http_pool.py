"""Pooled MCP HTTP transport (latency Phase 2): separate MCP sessions reuse one TCP/TLS
connection, while headers stay per call (the identity envelope / PAT is never shared)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_request

from app.integration.http_pool import aclose_all, pooled_client_factory


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_url():
    mcp = FastMCP("probe")

    @mcp.tool
    def whoami() -> dict:
        req = get_http_request()
        return {"port": req.client.port, "token": req.headers.get("x-mcp-token")}

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp/", stateless_http=True),
                                           host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp/"
    server.should_exit = True
    t.join(timeout=5)


async def _call(url: str, token: str, pooled: bool) -> dict:
    kw = {"httpx_client_factory": pooled_client_factory} if pooled else {}
    async with Client(StreamableHttpTransport(url, headers={"X-MCP-Token": token}, **kw)) as c:
        res = await c.call_tool("whoami", {})
    return res.data if res.data is not None else res.structured_content


@pytest.mark.asyncio
async def test_pooled_sessions_reuse_connection_with_per_call_headers(mcp_url):
    try:
        a = await _call(mcp_url, "tok-A", pooled=True)
        b = await _call(mcp_url, "tok-B", pooled=True)
        assert a["port"] == b["port"]  # same client socket => no new TCP/TLS handshake
        assert (a["token"], b["token"]) == ("tok-A", "tok-B")  # credentials stay per call
        # Concurrent calls (asyncio.gather in the ACL) still work on the shared pool.
        c, d = await asyncio.gather(_call(mcp_url, "tok-C", True), _call(mcp_url, "tok-D", True))
        assert {c["token"], d["token"]} == {"tok-C", "tok-D"}
    finally:
        await aclose_all()


@pytest.mark.asyncio
async def test_unpooled_baseline_opens_new_connections(mcp_url):
    a = await _call(mcp_url, "t", pooled=False)
    b = await _call(mcp_url, "t", pooled=False)
    assert a["port"] != b["port"]
