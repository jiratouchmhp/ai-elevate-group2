"""Shared, pooled HTTP transport for outbound MCP calls (latency optimisation, Phase 2).

Every MCP tool call used to open a fresh `httpx` client, so each call paid a new TCP +
TLS handshake — twice per tool call in the deployed topology (agent -> hr-acl -> vendor).
fastmcp closes the client it gets from `httpx_client_factory` at the end of each session,
so instead of sharing a client we share the *connection pool*: every per-call client is
built on one pooled transport (wrapped so the per-call `aclose()` leaves it open).

Security properties are unchanged: headers (identity envelope, ID token, X-MCP-Token) and
auth are still set per client/per call; only TCP/TLS connections are reused.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

import httpx2

_LIMITS = httpx2.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60.0)
_DEFAULT_TIMEOUT = httpx2.Timeout(30.0, read=300.0)  # same as mcp.create_mcp_http_client

# Connection pools are bound to the event loop that created them (one loop in production,
# one per test in pytest-asyncio), so keep one pool per running loop.
_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx2.AsyncHTTPTransport] = weakref.WeakKeyDictionary()


class _SharedTransport(httpx2.AsyncBaseTransport):
    """Delegates to the loop's pooled transport; closing a per-call client keeps the pool."""

    def __init__(self, inner: httpx2.AsyncHTTPTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:  # the pool outlives each MCP session
        return None


def _pool() -> httpx2.AsyncHTTPTransport:
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        pool = _pools[loop] = httpx2.AsyncHTTPTransport(limits=_LIMITS)
    return pool


def pooled_client_factory(
    headers: dict[str, str] | None = None,
    auth: httpx2.Auth | None = None,
    timeout: httpx2.Timeout | None = None,
    **_: Any,  # e.g. follow_redirects: MCP follows same-origin redirects itself (mcp default: off)
) -> httpx2.AsyncClient:
    """`StreamableHttpTransport(httpx_client_factory=...)` hook backed by the shared pool."""
    return httpx2.AsyncClient(transport=_SharedTransport(_pool()), headers=headers, auth=auth,
                              timeout=timeout or _DEFAULT_TIMEOUT)


async def aclose_all() -> None:
    """Close the current loop's pool (app shutdown)."""
    loop = asyncio.get_running_loop()
    pool = _pools.pop(loop, None)
    if pool is not None:
        await pool.aclose()
