"""Opt-in, read-only: McpBackend + VendorAdapter against the real vendor host.

Runs only with `HR_LIVE_VENDOR=1` and `BACKEND_SHARED_TOKEN` + `BACKEND_BASE_URL` set
(e.g. `HR_LIVE_VENDOR=1 uv run --env-file .env pytest tests/integration/test_mcp_backend.py`).
Never performs writes.
"""

from __future__ import annotations

import os

import pytest

from app.integration.backend_client import McpBackend
from app.integration.errors import BackendError

pytestmark = pytest.mark.skipif(
    os.getenv("HR_LIVE_VENDOR") != "1" or not os.getenv("BACKEND_SHARED_TOKEN"),
    reason="live vendor check is opt-in (HR_LIVE_VENDOR=1 + BACKEND_SHARED_TOKEN)",
)

EMP = os.getenv("DEMO_EMPLOYEE_ID", "EMP-829")
H = {"X-Actor-Type": "AUTOMATED_AGENT", "X-On-Behalf-Of": EMP, "X-Agent-Id": "t", "X-Correlation-Id": "c"}


def _backend() -> McpBackend:
    return McpBackend(base_url=os.environ["BACKEND_BASE_URL"], tokens={EMP: os.environ["BACKEND_SHARED_TOKEN"]})


@pytest.mark.asyncio
async def test_live_reads_normalise_to_canonical_shapes():
    b = _backend()
    prof = await b.call("work_week.get_profile", EMP, H)
    assert prof["employee_id"] == EMP and "address" in prof
    bal = await b.call("work_week.get_employee_balances", EMP, H)
    assert {"accrued", "used", "remaining"} <= set(bal["Vacation"])
    for r in await b.call("work_week.get_leave_requests", EMP, H):
        assert isinstance(r["request_id"], str) and r["status"]
    tickets = await b.call("service_immediately.list_tickets", EMP, H)
    if tickets:
        t = await b.call("service_immediately.get_ticket", EMP, H, ticket_id=tickets[0]["ticket_id"])
        assert t["requestor_id"] == EMP


@pytest.mark.asyncio
async def test_live_errors_carry_status():
    b = _backend()
    with pytest.raises(BackendError) as e:
        await b.call("service_immediately.get_ticket", EMP, H, ticket_id="INC9999999")
    assert e.value.status == 404
    with pytest.raises(BackendError) as e:
        await b.call("work_week.get_employee_feedback", EMP, H)  # not in VENDOR_OPS
    assert e.value.status == 403
