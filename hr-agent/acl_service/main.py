"""hr-acl — the Integration Plane service on Cloud Run (SDD §1.3 plane 4, D7, D9).

A curated MCP surface in front of the vendor MCP servers. Every call:
  1. is admitted by Cloud Run IAM (only the agent identity holds `run.invoker`);
  2. derives identity ONLY from the HMAC-signed `X-HR-Identity` envelope;
  3. runs through the unchanged ACL: attribution headers, PDP on propose AND commit,
     HMAC intent tokens, idempotency + saga ledger (Firestore), audit to Cloud Logging;
  4. forwards to the vendor with the PAT from Secret Manager (`X-MCP-Token`).

Vendor tools that must never be reachable (feedback, identity, token management) are
simply not proxied: `acl_read` only accepts the read ops listed below.

    uv run uvicorn acl_service.main:app --port 8080
"""

from __future__ import annotations

import json

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app import config
from app.integration import acl
from app.integration.acl_client import ENVELOPE_HEADER, unseal

READ_OPS = {
    "work_week.get_profile", "work_week.get_personal_info", "work_week.get_employee_balances",
    "work_week.get_leave_requests", "service_immediately.get_ticket", "service_immediately.list_tickets",
}
WRITE_ACTIONS = set(acl.WRITE_OPS)

mcp = FastMCP("hr-acl")


def _ctx():
    h = get_http_headers(include_all=True)
    ctx = unseal(h.get(ENVELOPE_HEADER.lower()))
    if ctx is None:
        raise ToolError("[401] Missing or invalid identity envelope")
    return ctx


@mcp.tool
async def acl_read(tool: str, op: str, args: dict | None = None) -> dict:
    """Read from a system of record on behalf of the enveloped employee."""
    if op not in READ_OPS:
        raise ToolError(f"[403] Operation {op} is not exposed")
    return await acl.read(_ctx(), tool, op, **(args or {}))


@mcp.tool
async def acl_propose(action: str, args: dict) -> dict:
    """Validate a mutating action with the PDP and return a signed intent token (no write)."""
    if action not in WRITE_ACTIONS:
        raise ToolError(f"[403] Action {action} is not exposed")
    return await acl.propose(_ctx(), action, args)


@mcp.tool
async def acl_commit(intent_token: str) -> dict:
    """Execute a previously proposed action (re-validated; never retried)."""
    return await acl.commit(_ctx(), intent_token)


mcp_app = mcp.http_app(path="/mcp/", stateless_http=True)


async def healthz(_request):
    return JSONResponse({"status": "ok", "backend_mode": config.BACKEND_MODE,
                         "ledger": config.LEDGER_BACKEND, "rules_version": acl.pdp.rules_version()})


# /healthz serves the Cloud Run probe and local tests; externally, run.app's front end
# reserves paths ending in "z" (404 before reaching the container), so /health is the alias.
app = Starlette(routes=[Route("/healthz", healthz), Route("/health", healthz), Mount("/", app=mcp_app)],
                lifespan=mcp_app.lifespan)

if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    print(json.dumps({"hr_acl": "starting"}))
    uvicorn.run(app, host="0.0.0.0", port=8080)
