"""End-to-end: agent-side acl_client (INTEGRATION_MODE=remote) -> hr-acl service over MCP.

The service runs as a separate process with its own in-process vendor mock + SQLite ledger,
exactly as it would on Cloud Run (minus IAM, which needs https).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

from app.integration import acl_client
from app.integration.acl import Ctx


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def acl_url():
    port = _free_port()
    env = os.environ | {"BACKEND_MODE": "inprocess", "LEDGER_BACKEND": "sqlite",
                        "HR_RUNTIME_DIR": tempfile.mkdtemp(prefix="hr-acl-svc-"), "AUDIT_SINK": "file"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "acl_service.main:app", "--port", str(port),
                             "--log-level", "warning"], env=env)
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def remote(acl_url, monkeypatch):
    monkeypatch.setattr(acl_client.config, "INTEGRATION_MODE", "remote")
    monkeypatch.setattr(acl_client.config, "ACL_URL", acl_url)
    return acl_url


def ctx(inv: str, emp: str = "EMP001") -> Ctx:
    return Ctx(employee_id=emp, session_id="hrs-svc", invocation_id=inv, agent_id="workweek_agent",
               correlation_id=f"corr-{inv}")


@pytest.mark.parametrize("path", ["/healthz", "/health"])
def test_healthz(acl_url, path):
    r = httpx.get(f"{acl_url}{path}")
    assert r.status_code == 200 and r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_remote_read(remote):
    r = await acl_client.read(ctx("r1"), "get_leave_balance", "work_week.get_employee_balances")
    assert r["status"] == "success" and r["data"]["Vacation"]["remaining"] == 5.0


@pytest.mark.asyncio
async def test_remote_propose_then_commit_next_turn(remote):
    p = await acl_client.propose(ctx("p1"), "submit_leave",
                                 {"start_date": "2026-11-02", "end_date": "2026-11-03", "leave_type": "Vacation"})
    assert p["status"] == "awaiting_confirmation", p
    c = await acl_client.commit(ctx("p2"), p["intent_token"])
    assert c["status"] == "committed" and c["backend_ref"].startswith("LR-")
    again = await acl_client.commit(ctx("p3"), p["intent_token"])
    assert again["status"] == "already_committed"


@pytest.mark.asyncio
async def test_remote_rejects_unexposed_ops(remote):
    r = await acl_client.read(ctx("x1"), "t", "work_week.get_employee_feedback")
    assert r["status"] == "error" and "403" in r["message"]
    r = await acl_client.propose(ctx("x2"), "delete_mcp_token", {})
    assert r["status"] == "error" and "403" in r["message"]


@pytest.mark.asyncio
async def test_remote_requires_valid_envelope(remote, monkeypatch):
    monkeypatch.setattr(acl_client, "seal", lambda _ctx: "forged.deadbeef")
    r = await acl_client.read(ctx("e1"), "get_leave_balance", "work_week.get_employee_balances")
    assert r["status"] == "error" and "401" in r["message"]
