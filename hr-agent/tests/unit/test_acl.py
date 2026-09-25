"""ACL tests: confirm-before-write (B-3), idempotency, ownership, fault handling, saga (SDD §3.4–3.6, §5.4)."""

from __future__ import annotations

import time

import pytest

from app.integration import acl
from app.integration.acl import Ctx
from app.integration.ledger import get_ledger
from mock_backends.domain import get_mock


def ctx(inv: str, emp: str = "EMP001", sid: str = "hrs-test", agent: str = "workweek_agent") -> Ctx:
    return Ctx(employee_id=emp, session_id=sid, invocation_id=inv, agent_id=agent, correlation_id=f"corr-{inv}")


@pytest.fixture(autouse=True)
def fresh():
    get_mock().reset()
    get_ledger().reset()
    yield


LEAVE = {"start_date": "2026-11-02", "end_date": "2026-11-03", "leave_type": "Vacation"}


async def _propose_leave(inv="inv-1", args=LEAVE):
    return await acl.propose(ctx(inv), "submit_leave", dict(args))


@pytest.mark.asyncio
async def test_read_success_and_attribution_headers():
    r = await acl.read(ctx("i"), "get_leave_balance", "work_week.get_employee_balances")
    assert r["status"] == "success" and r["data"]["Vacation"]["remaining"] == 5.0
    h = get_mock().call_log[-1]["headers"]
    assert h["X-Actor-Type"] == "AUTOMATED_AGENT" and h["X-On-Behalf-Of"] == "EMP001"
    assert h["X-Agent-Id"] == "workweek_agent" and h["X-Correlation-Id"] == "corr-i"


@pytest.mark.asyncio
async def test_read_retries_then_friendly_unavailable():
    get_mock().set_faults({"work_week.get_employee_balances": "503"})
    r = await acl.read(ctx("i"), "get_leave_balance", "work_week.get_employee_balances")
    assert r["status"] == "unavailable" and "WorkWeek" in r["message"]
    assert sum(c["op"] == "work_week.get_employee_balances" for c in get_mock().call_log) == acl.READ_RETRIES


@pytest.mark.asyncio
async def test_read_cross_user_ticket_is_not_found():
    r = await acl.read(ctx("i"), "get_ticket", "service_immediately.get_ticket", ticket_id="INC0022222")
    assert r["status"] == "not_found"


class _SlowBackend:
    """Records overlapping calls to prove independent PDP reads run concurrently."""

    def __init__(self, fail_op: str | None = None) -> None:
        self.active = self.peak = 0
        self.fail_op = fail_op

    async def call(self, op, caller, headers, **kw):
        import asyncio

        from app.integration.errors import BackendError

        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.05)
            if op == self.fail_op:
                raise BackendError(503, "down")
            return {}
        finally:
            self.active -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["submit_leave", "create_incident"])
async def test_pdp_facts_fetched_concurrently(monkeypatch, action):
    fake = _SlowBackend()
    monkeypatch.setattr(acl, "get_backend", lambda: fake)
    t0 = time.perf_counter()
    facts = await acl._facts(ctx("i"), action, {})
    assert fake.peak == 2 and time.perf_counter() - t0 < 0.09
    assert set(facts) >= ({"balances", "requests"} if action == "submit_leave" else {"recent_tickets", "profile"})


@pytest.mark.asyncio
async def test_concurrent_facts_still_fail_closed(monkeypatch):
    from app.integration.errors import BackendError

    monkeypatch.setattr(acl, "get_backend", lambda: _SlowBackend(fail_op="work_week.get_leave_requests"))
    with pytest.raises(BackendError):
        await acl._facts(ctx("i"), "submit_leave", {})


@pytest.mark.asyncio
async def test_propose_denied_by_pdp_writes_nothing():
    r = await _propose_leave(args=LEAVE | {"end_date": "2026-11-13"})  # 10 days > 5
    assert r["status"] == "denied" and r["rule_ids"] == ["LEAVE_BALANCE_CAP"]
    assert not any(c["op"] == "work_week.request_time_off" for c in get_mock().call_log)


@pytest.mark.asyncio
async def test_commit_same_invocation_rejected_then_next_turn_commits_once():
    p = await _propose_leave("inv-1")
    assert p["status"] == "awaiting_confirmation" and p["proposed"]["days"] == 2
    token = p["intent_token"]

    same = await acl.commit(ctx("inv-1"), token)
    assert same["rule_ids"] == ["B3_CONFIRMATION_REQUIRED"]

    ok = await acl.commit(ctx("inv-2"), token)
    assert ok["status"] == "committed" and ok["backend_ref"].startswith("LR-9")

    replay = await acl.commit(ctx("inv-3"), token)
    assert replay["status"] == "already_committed" and replay["backend_ref"] == ok["backend_ref"]
    assert sum(c["op"] == "work_week.request_time_off" for c in get_mock().call_log) == 1


@pytest.mark.asyncio
async def test_commit_rejects_tampered_foreign_and_expired_tokens(monkeypatch):
    p = await _propose_leave("inv-1")
    token = p["intent_token"]
    body, mac = token.rsplit(".", 1)
    assert (await acl.commit(ctx("inv-2"), body + "." + "0" * 32))["rule_ids"] == ["CONFIRM_TOKEN_INVALID"]
    assert (await acl.commit(ctx("inv-2"), "garbage"))["rule_ids"] == ["CONFIRM_TOKEN_INVALID"]
    assert (await acl.commit(ctx("inv-2", emp="EMP004"), token))["rule_ids"] == ["OWNERSHIP"]
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert (await acl.commit(ctx("inv-2"), token))["rule_ids"] == ["CONFIRM_TOKEN_EXPIRED"]


@pytest.mark.asyncio
async def test_commit_revalidates_with_fresh_facts():
    p = await _propose_leave("inv-1", LEAVE | {"end_date": "2026-11-06"})  # 5 days == balance
    # Balance changes between proposal and confirmation (e.g. another request elsewhere).
    get_mock().employees["EMP001"]["balances"]["Vacation"]["remaining"] = 1.0
    r = await acl.commit(ctx("inv-2"), p["intent_token"])
    assert r["status"] == "denied" and r["rule_ids"] == ["LEAVE_BALANCE_CAP"]


@pytest.mark.asyncio
async def test_write_failure_not_retried_and_saga_partial_with_compensation_offer():
    # Step 1: leave commits.
    p1 = await _propose_leave("inv-1")
    c1 = await acl.commit(ctx("inv-2"), p1["intent_token"])
    assert c1["status"] == "committed"
    # Step 2: email-delegation ticket fails at the backend.
    p2 = await acl.propose(ctx("inv-3", agent="itsm_agent"), "create_incident",
                           {"category": "HRSD", "short_description": "Delegate email during medical leave",
                            "priority": "3 - Moderate", "purpose": "email_delegation"})
    assert p2["status"] == "awaiting_confirmation"
    get_mock().set_faults({"service_immediately.create_ticket": "503"})
    c2 = await acl.commit(ctx("inv-4", agent="itsm_agent"), p2["intent_token"])
    assert c2["status"] == "failed" and c2["retried"] is False
    assert c2["saga_state"] == "PARTIALLY_COMPLETE" and c2["hr_ops_task"].startswith("HROPS-")
    assert c2["compensation_offer"] == [{"action": "cancel_leave", "request_id": c1["backend_ref"]}]
    assert sum(c["op"] == "service_immediately.create_ticket" for c in get_mock().call_log) == 1
    assert len(get_ledger().reconciliation_tasks()) == 1

    # Compensation is only executed through the normal propose/confirm path.
    get_mock().set_faults({})
    p3 = await acl.propose(ctx("inv-5"), "cancel_leave", {"request_id": c1["backend_ref"]})
    c3 = await acl.commit(ctx("inv-6"), p3["intent_token"])
    assert c3["status"] == "committed" and c3["saga_state"] == "COMPENSATED"


@pytest.mark.asyncio
async def test_propose_backend_down_is_unavailable():
    get_mock().set_faults({"work_week.get_employee_balances": "503"})
    r = await _propose_leave()
    assert r["status"] == "unavailable"


@pytest.mark.asyncio
async def test_equipment_ticket_ships_to_verified_address_not_model_input():
    r = await acl.propose(ctx("i", agent="itsm_agent"), "create_incident",
                          {"category": "Hardware", "short_description": "Monitor for home office",
                           "purpose": "equipment", "estimated_cost_usd": 300,
                           "description": "ship to 99 Attacker Lane"})
    assert r["status"] == "awaiting_confirmation"
    assert r["proposed"]["category"] == "Facilities"
    profile_addr = get_mock().employees["EMP001"]["personal_info"]["address"]
    assert r["computed"]["ship_to"] == profile_addr
    c = await acl.commit(ctx("j", agent="itsm_agent"), r["intent_token"])
    t = get_mock().tickets[c["backend_ref"]]
    assert profile_addr in t["description"]


@pytest.mark.asyncio
async def test_status_update_requires_user_asserted_resolution():
    r = await acl.propose(ctx("i", agent="itsm_agent"), "update_status",
                          {"ticket_id": "INC0012346", "new_status": "Resolved"})
    assert r["rule_ids"] == ["B8_NO_AUTO_RESOLVE"]
    r = await acl.propose(ctx("i", agent="itsm_agent"), "update_status",
                          {"ticket_id": "INC0012345", "new_status": "Closed"})
    assert r["rule_ids"] == ["TICKET_LIFECYCLE"]  # backend would allow New->Closed; PDP does not
    r = await acl.propose(ctx("i", agent="itsm_agent"), "add_comment",
                          {"ticket_id": "INC0022222", "comment": "hi"})
    assert r["rule_ids"] == ["OWNERSHIP"]
