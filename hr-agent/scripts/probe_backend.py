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
    # 1. Probe WorkWeek
    ww_url = f"{base_url.rstrip('/')}/work-week/mcp/"
    emp_id = os.environ.get("DEMO_EMPLOYEE_ID", "EMP-829")
    try:
        async with Client(StreamableHttpTransport(ww_url, headers={"X-MCP-Token": token})) as c:
            tools = await c.list_tools()
            print(f"\n[work-week] {len(tools)} tools: {sorted(t.name for t in tools)}")

            # Identify employee from token
            if any(t.name == "get_current_employee_id" for t in tools):
                res = await c.call_tool("get_current_employee_id", {}, raise_on_error=False)
                sc = res.structured_content or {}
                if isinstance(sc, dict) and sc.get("result"):
                    emp_id = str(sc["result"])
                elif res.content:
                    emp_id = "".join(getattr(x, "text", "") for x in res.content).strip() or emp_id
                print(f"[work-week] get_current_employee_id -> {emp_id}")

            # Read profile resource
            try:
                contents = await c.read_resource(f"workweek://employees/{emp_id}/profile")
                profile_raw = "".join(getattr(x, "text", "") for x in contents)
                print(f"[work-week] profile resource -> {profile_raw.strip()[:600]}")
            except Exception as exc:
                print(f"[work-week] profile resource failed: {exc}")

            # Read balances
            res = await c.call_tool("get_employee_balances", {"employee_id": emp_id}, raise_on_error=False)
            data = res.data if res.data is not None else res.structured_content
            txt = "".join(getattr(x, "text", "") for x in res.content) if res.content else ""
            print(f"[work-week] get_employee_balances -> {data or txt}")
    except Exception as exc:
        print(f"\n[work-week] FAILED: {type(exc).__name__}: {exc}")

    # 2. Probe ServiceImmediately
    si_url = f"{base_url.rstrip('/')}/service-immediately/mcp/"
    try:
        async with Client(StreamableHttpTransport(si_url, headers={"X-MCP-Token": token})) as c:
            tools = await c.list_tools()
            print(f"\n[service-immediately] {len(tools)} tools: {sorted(t.name for t in tools)}")
            res = await c.call_tool("list_tickets", {"employee_id": emp_id}, raise_on_error=False)
            data = res.data if res.data is not None else res.structured_content
            txt = "".join(getattr(x, "text", "") for x in res.content) if res.content else ""
            print(f"[service-immediately] list_tickets -> {json.dumps(data or txt, default=str)[:600]}")
    except Exception as exc:
        print(f"\n[service-immediately] FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("BACKEND_BASE_URL", ""))
    a = ap.parse_args()
    if not a.base_url:
        raise SystemExit("Provide --base-url or BACKEND_BASE_URL")
    asyncio.run(main(a.base_url))
