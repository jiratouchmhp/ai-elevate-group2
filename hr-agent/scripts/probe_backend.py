"""Read-only connectivity probe for the vendor MCP backends.

Lists the tools each vendor MCP server publishes and calls `get_profile` to reveal
which persona the configured PAT resolves to. Performs NO writes.

    uv run --env-file .env python scripts/probe_backend.py --base-url https://mock-saas.<domain>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os


async def main(base_url: str) -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    token = os.environ.get("BACKEND_SHARED_TOKEN", "")
    if not token:
        raise SystemExit("BACKEND_SHARED_TOKEN is not set (put it in .env)")
    print(f"token: {token[:6]}…{token[-4:]}  base: {base_url}")
    for system, probe in (("work-week", "get_profile"), ("service-immediately", "list_tickets")):
        url = f"{base_url.rstrip('/')}/{system}/mcp/"
        try:
            async with Client(StreamableHttpTransport(url, headers={"X-MCP-Token": token})) as c:
                tools = await c.list_tools()
                print(f"\n[{system}] {len(tools)} tools: {sorted(t.name for t in tools)}")
                res = await c.call_tool(probe, {}, raise_on_error=False)
                data = res.data if res.data is not None else res.structured_content
                print(f"[{system}] {probe} ->", json.dumps(data, default=str)[:800] if not res.is_error
                      else f"ERROR {[getattr(x, 'text', '') for x in res.content]}")
        except Exception as exc:  # report and continue
            print(f"\n[{system}] FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("BACKEND_BASE_URL", ""))
    a = ap.parse_args()
    if not a.base_url:
        raise SystemExit("Provide --base-url or BACKEND_BASE_URL")
    asyncio.run(main(a.base_url))
