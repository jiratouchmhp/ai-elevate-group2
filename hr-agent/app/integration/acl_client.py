"""Integration-plane client used by agent tools (SDD D7/D9).

`INTEGRATION_MODE=inprocess` (default, eval/dev): call `app.integration.acl` directly.
`INTEGRATION_MODE=remote` (deployed): call the `hr-acl` Cloud Run service over MCP.

Remote calls carry two credentials:
  * `Authorization: Bearer <Google ID token>` — Cloud Run IAM (`run.invoker`) admits only
    the agent's identity; nobody else can reach the ACL.
  * `X-HR-Identity` — an HMAC-signed envelope binding {employee_id, session_id,
    invocation_id, agent_id, correlation_id}. The ACL derives identity ONLY from this
    envelope (never from tool arguments), so a prompt cannot change who is acted for.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app import config
from app.integration.acl import Ctx

ENVELOPE_HEADER = "X-HR-Identity"
ENVELOPE_TTL_S = 300


# ------------------------------------------------------------------ identity envelope
def seal(ctx: Ctx) -> str:
    body = base64.urlsafe_b64encode(json.dumps({
        "emp": ctx.employee_id, "sid": ctx.session_id, "inv": ctx.invocation_id,
        "agent": ctx.agent_id, "corr": ctx.correlation_id, "iat": int(time.time()),
    }, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(config.IDENTITY_ENVELOPE_SECRET, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unseal(envelope: str | None) -> Ctx | None:
    if not envelope or "." not in envelope:
        return None
    body, mac = envelope.rsplit(".", 1)
    expected = hmac.new(config.IDENTITY_ENVELOPE_SECRET, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        d = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if abs(time.time() - d.get("iat", 0)) > ENVELOPE_TTL_S:
        return None
    return Ctx(employee_id=d["emp"], session_id=d["sid"], invocation_id=d["inv"],
               agent_id=d["agent"], correlation_id=d["corr"])


# ------------------------------------------------------------------ remote transport
_id_token_cache: dict[str, tuple[str, float]] = {}


def _id_token(audience: str) -> str:  # pragma: no cover - needs GCP metadata/ADC
    tok, exp = _id_token_cache.get(audience, ("", 0.0))
    if tok and exp - time.time() > 60:
        return tok
    import google.auth.transport.requests
    from google.oauth2 import id_token

    tok = id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)
    _id_token_cache[audience] = (tok, time.time() + 3000)
    return tok


async def _remote(tool: str, ctx: Ctx, args: dict) -> dict:  # pragma: no cover - exercised in cloud
    import asyncio

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from app.integration.http_pool import pooled_client_factory

    base = config.ACL_URL.rstrip("/")
    headers = {ENVELOPE_HEADER: seal(ctx)}
    if base.startswith("https://"):
        # Cached; on refresh the fetch is a blocking metadata-server call — keep it off the loop.
        headers["Authorization"] = f"Bearer {await asyncio.to_thread(_id_token, base)}"
    try:
        async with Client(StreamableHttpTransport(f"{base}/mcp/", headers=headers,
                                                  httpx_client_factory=pooled_client_factory)) as c:
            res = await c.call_tool(tool, args, raise_on_error=False)
    except Exception:
        return {"status": "unavailable",
                "message": "The HR integration service is temporarily unavailable. Please try again shortly."}
    if res.is_error:
        text = " ".join(getattr(x, "text", "") for x in res.content)
        return {"status": "error", "message": text[:300]}
    return res.data if res.data is not None else res.structured_content


# ------------------------------------------------------------------ public API (mirrors acl)
async def read(ctx: Ctx, tool: str, op: str, **kwargs: Any) -> dict:
    if config.INTEGRATION_MODE == "remote":
        return await _remote("acl_read", ctx, {"tool": tool, "op": op, "args": kwargs})
    from app.integration import acl
    return await acl.read(ctx, tool, op, **kwargs)


async def propose(ctx: Ctx, action: str, args: dict) -> dict:
    if config.INTEGRATION_MODE == "remote":
        return await _remote("acl_propose", ctx, {"action": action, "args": args})
    from app.integration import acl
    return await acl.propose(ctx, action, args)


async def commit(ctx: Ctx, intent_token: str) -> dict:
    if config.INTEGRATION_MODE == "remote":
        return await _remote("acl_commit", ctx, {"intent_token": intent_token})
    from app.integration import acl
    return await acl.commit(ctx, intent_token)
