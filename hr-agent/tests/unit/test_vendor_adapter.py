"""VendorAdapter: real vendor wire contract -> canonical ACL/PDP shapes (SDD §5.1)."""

from __future__ import annotations

import asyncio
import json

import pytest

from app import config
from app.integration.backend_client import McpBackend, VendorAdapter, parse_balances, parse_personal_info
from app.integration.errors import BackendError

EMP = "EMP-829"
BALANCES = ("Employee EMP-829 Leave Balances:\n- Vacation: 15.0 days remaining (5.0/20.0 used)\n"
            "- Sick: 10.0 days remaining (0.0/10.0 used)")
PERSONAL = "Personal info for EMP-829:\n- Address: 1 Raffles Place, Singapore 048616\n- Phone: +65-6521-0000"
PROFILE = {"employee_id": EMP, "first_name": "Jiratouch", "last_name": "Employee", "email": "j@x.com",
           "job_title": "Engineer", "department": "Eng", "role": "IC", "hire_date": "2024-01-01",
           "manager_id": "EMP-1", "home_address": "1 Raffles Place", "phone_number": "+65-6521-0000",
           "supervisory_org": "Singapore Office"}
TICKET = {"ticket_id": "INC0004799", "requested_by": EMP, "category": "Hardware",
          "short_description": "Laptop broken", "status": "New", "priority": "3 - Moderate",
          "assignment_group": "Service Desk", "assigned_to": "ITIL User",
          "created_at": "2026-09-22T08:27:07.367281", "updated_at": "2026-09-22T08:27:07.367281",
          "comments": []}


class FakeWire:
    def __init__(self, tools: dict | None = None, resources: dict | None = None) -> None:
        self.tools, self.resources, self.calls = tools or {}, resources or {}, []

    async def tool(self, system, name, args):
        self.calls.append((system, name, dict(args)))
        v = self.tools[name]
        return v(args) if callable(v) else v

    async def resource(self, system, uri):
        self.calls.append((system, "resource", uri))
        return self.resources.get(uri, json.dumps({"error": f"{uri} not found"}))


def run(adapter, op, **kw):
    return asyncio.run(adapter.call(op, EMP, **kw))


def test_parse_balances_and_personal_info():
    assert parse_balances(BALANCES) == {"Vacation": {"accrued": 20.0, "used": 5.0, "remaining": 15.0},
                                        "Sick": {"accrued": 10.0, "used": 0.0, "remaining": 10.0}}
    assert parse_personal_info(PERSONAL) == {"address": "1 Raffles Place, Singapore 048616",
                                             "phone": "+65-6521-0000"}
    assert parse_personal_info(json.dumps({"home_address": "A st", "phone_number": "1"})) == {
        "address": "A st", "phone": "1"}
    with pytest.raises(BackendError):
        parse_balances("garbage")


def test_reads_inject_employee_id_and_normalise():
    w = FakeWire(tools={"get_employee_balances": BALANCES, "get_personal_info": PERSONAL,
                        "get_leave_requests": json.dumps([{"request_id": 3198, "employee_id": EMP,
                                                           "start_date": "2026-10-01", "end_date": "2026-10-02",
                                                           "leave_type": "Vacation", "days": 2}]),
                        "list_tickets": json.dumps([TICKET | {"short_description": "printer not found"}])})
    a = VendorAdapter(w)
    assert run(a, "work_week.get_employee_balances")["Vacation"]["remaining"] == 15.0
    reqs = run(a, "work_week.get_leave_requests")
    assert reqs[0]["request_id"] == "3198" and reqs[0]["status"] == "Approved"
    tickets = run(a, "service_immediately.list_tickets")  # "not found" inside a record is not an error
    assert tickets[0]["ticket_id"] == "INC0004799" and tickets[0]["created_at"].endswith("+08:00")
    assert all(c[2] == {"employee_id": EMP} for c in w.calls)


def test_profile_mapping_and_location_default(monkeypatch):
    uri = f"workweek://employees/{EMP}/profile"
    a = VendorAdapter(FakeWire(resources={uri: json.dumps(PROFILE)}))
    p = run(a, "work_week.get_profile")
    assert p["name"] == "Jiratouch Employee" and p["address"] == "1 Raffles Place"
    assert p["location_status"] is None  # fail-closed by default
    monkeypatch.setattr(config, "VENDOR_DEFAULT_LOCATION_STATUS", "Hybrid")
    assert run(a, "work_week.get_profile")["location_status"] == "Hybrid"


def test_not_found_text_maps_to_404():
    a = VendorAdapter(FakeWire(tools={"get_employee_balances": "Employee EMP-829 not found."}))
    with pytest.raises(BackendError) as e:
        run(a, "work_week.get_employee_balances")
    assert e.value.status == 404
    with pytest.raises(BackendError) as e:
        run(a, "service_immediately.get_ticket", ticket_id="INC9")
    assert e.value.status == 404


def test_failure_prose_maps_to_422():
    a = VendorAdapter(FakeWire(tools={"request_time_off": "Error: insufficient balance"}))
    with pytest.raises(BackendError) as e:
        run(a, "work_week.request_time_off", start_date="2026-10-01", end_date="2026-10-01",
            leave_type="Vacation", days=1)
    assert e.value.status == 422


def test_get_ticket_enforces_ownership():
    uri = "serviceimmediately://tickets/INC1"
    a = VendorAdapter(FakeWire(resources={uri: json.dumps(TICKET | {"ticket_id": "INC1", "requested_by": "X"})}))
    with pytest.raises(BackendError) as e:
        run(a, "service_immediately.get_ticket", ticket_id="INC1")
    assert e.value.status == 403


def test_update_personal_info_fills_missing_field():
    w = FakeWire(tools={"get_personal_info": PERSONAL, "update_personal_info": "Successfully updated."})
    out = run(VendorAdapter(w), "work_week.update_personal_info", phone="+65 9123 4567")
    assert w.calls[-1][2] == {"employee_id": EMP, "address": "1 Raffles Place, Singapore 048616",
                              "phone": "+65 9123 4567"}
    assert out["personal_info"]["phone"] == "+65 9123 4567"


def test_request_time_off_reference_from_text_or_ledger():
    w = FakeWire(tools={"request_time_off": "Leave request 4001 submitted."})
    assert run(VendorAdapter(w), "work_week.request_time_off", start_date="2026-10-01",
               end_date="2026-10-02", leave_type="Vacation", days=2)["request_id"] == "4001"
    rows = [{"request_id": 7, "employee_id": EMP, "start_date": "2026-10-01", "end_date": "2026-10-02",
             "leave_type": "Vacation", "days": 2}]
    w = FakeWire(tools={"request_time_off": "Time off submitted successfully.",
                        "get_leave_requests": json.dumps(rows)})
    assert run(VendorAdapter(w), "work_week.request_time_off", start_date="2026-10-01",
               end_date="2026-10-02", leave_type="Vacation", days=2)["request_id"] == "7"


def test_cancel_sends_integer_request_id():
    w = FakeWire(tools={"cancel_leave_request": "Cancelled request 3198."})
    out = run(VendorAdapter(w), "work_week.cancel_leave_request", request_id="3198")
    assert w.calls[-1][2] == {"employee_id": EMP, "request_id": 3198} and out["request_id"] == "3198"
    with pytest.raises(BackendError):
        run(VendorAdapter(w), "work_week.cancel_leave_request", request_id="LR-1")


def test_create_ticket_folds_description_into_comment():
    w = FakeWire(tools={"create_ticket": "Created ticket INC0005000.", "add_ticket_comment": "ok"})
    out = run(VendorAdapter(w), "service_immediately.create_ticket", category="Facilities",
              short_description="Monitor", priority="4 - Low", description="Ship to: 1 Raffles")
    assert out["ticket_id"] == "INC0005000"
    assert w.calls[0][2] == {"requested_by": EMP, "category": "Facilities", "short_description": "Monitor",
                             "priority": "4 - Low"}
    assert w.calls[1][2] == {"ticket_id": "INC0005000", "author": EMP, "comment": "Ship to: 1 Raffles"}


def test_status_and_comment_writes_use_vendor_params():
    uri = "serviceimmediately://tickets/INC0004799"
    w = FakeWire(tools={"update_ticket_status": "Updated.", "add_ticket_comment": "Added."},
                 resources={uri: json.dumps(TICKET)})
    a = VendorAdapter(w)
    run(a, "service_immediately.update_ticket_status", ticket_id="INC0004799", new_status="In Progress")
    assert w.calls[-1][2] == {"ticket_id": "INC0004799", "status": "In Progress", "resolution_notes": "",
                              "updated_by": EMP}
    run(a, "service_immediately.add_ticket_comment", ticket_id="INC0004799", comment="hi")
    assert w.calls[-1][2] == {"ticket_id": "INC0004799", "author": EMP, "comment": "hi"}


def test_mcp_backend_wires_token_and_rejects_unknown_ops(monkeypatch):
    seen = {}

    def factory(base, token, headers):
        seen.update(base=base, token=token, headers=headers)
        return FakeWire(tools={"get_employee_balances": BALANCES})

    b = McpBackend(base_url="https://vendor/", tokens={EMP: "tok"}, wire_factory=factory)
    asyncio.run(b.call("work_week.get_employee_balances", EMP, {"X-Actor-Type": "AGENT"}))
    assert seen == {"base": "https://vendor", "token": "tok", "headers": {"X-Actor-Type": "AGENT"}}
    with pytest.raises(BackendError) as e:
        asyncio.run(b.call("work_week.get_current_employee_id", EMP, {}))
    assert e.value.status == 403
    monkeypatch.setattr(config, "BACKEND_SHARED_TOKEN", "")
    with pytest.raises(BackendError) as e:
        asyncio.run(McpBackend(base_url="x", tokens={}, wire_factory=factory).call(
            "work_week.get_employee_balances", "EMP-2", {}))
    assert e.value.status == 401
