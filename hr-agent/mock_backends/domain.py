"""Mock WorkWeek (HCM) + ServiceImmediately (ITSM) — "Unified Mock Enterprise Services".

Mirrors the vendor contract described in SDD §5.1/§5.2:
  * vendor tool names (request_time_off, create_ticket, …)
  * server-side tenant isolation — callers may only act on their own records
  * vendor-side validation (dates, balance, phone regex, 5-min dedupe, Critical keywords)
  * a **permissive** status machine that allows New→Closed and Resolved→In Progress,
    so evaluation can prove the PDP enforces the stricter handbook lifecycle.
  * fault injection for resilience tests (NFR-4.x, §3.6 partial failure).
"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import clock
from app.integration.errors import BackendError

SEED_PATH = Path(__file__).with_name("seed.json")
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")
CRITICAL_KEYWORDS = ("outage", "down", "breach", "security", "all users", "production", "fire", "flood")
_ALLOWED_TRANSITIONS = {  # vendor state machine — more permissive than handbook §5.5
    "New": {"In Progress", "Closed"},
    "In Progress": {"Resolved", "New"},
    "Resolved": {"Closed", "In Progress"},
    "Closed": set(),
}


class MockEnterprise:
    def __init__(self, seed: dict | None = None) -> None:
        self._lock = threading.RLock()
        self._seed = seed or json.loads(SEED_PATH.read_text())
        self.reset()

    # ------------------------------------------------------------------ admin
    def reset(self, faults: dict[str, str] | None = None) -> None:
        with self._lock:
            s = copy.deepcopy(self._seed)
            self.employees: dict[str, dict] = s["employees"]
            self.feedback: dict[str, list] = s.get("feedback", {})
            self.leave_requests: list[dict] = s["leave_requests"]
            self.tickets: dict[str, dict] = {t["ticket_id"]: t for t in s["tickets"]}
            self.faults: dict[str, str] = dict(faults or {})
            self.call_log: list[dict] = []
            self._next_lr = 90100
            self._next_inc = 42300

    def set_faults(self, faults: dict[str, str]) -> None:
        self.faults = dict(faults)

    def snapshot(self) -> dict:
        return copy.deepcopy(
            {"employees": self.employees, "leave_requests": self.leave_requests, "tickets": self.tickets}
        )

    # ------------------------------------------------------------------ helpers
    def _enter(self, op: str, caller: str, headers: dict | None, **args: Any) -> None:
        self.call_log.append({"op": op, "caller": caller, "headers": dict(headers or {}), "args": args})
        fault = self.faults.get(op)
        if fault:
            code = int(fault) if str(fault).isdigit() else 503
            raise BackendError(code, f"Injected fault on {op}")
        if caller not in self.employees:
            raise BackendError(401, "Unknown caller identity")

    def _emp(self, caller: str) -> dict:
        return self.employees[caller]

    # ================================================================ WorkWeek
    def get_profile(self, caller: str, headers: dict | None = None) -> dict:
        self._enter("work_week.get_profile", caller, headers)
        e = self._emp(caller)
        return {k: e[k] for k in ("employee_id", "name", "email", "department", "role", "manager_id",
                                  "hire_date", "location_status", "office")} | {
            "address": e["personal_info"]["address"]}

    def get_personal_info(self, caller: str, headers: dict | None = None) -> dict:
        self._enter("work_week.get_personal_info", caller, headers)
        return dict(self._emp(caller)["personal_info"])

    def get_employee_balances(self, caller: str, headers: dict | None = None) -> dict:
        self._enter("work_week.get_employee_balances", caller, headers)
        return copy.deepcopy(self._emp(caller)["balances"])

    def get_leave_requests(self, caller: str, headers: dict | None = None) -> list[dict]:
        self._enter("work_week.get_leave_requests", caller, headers)
        return [dict(r) for r in self.leave_requests if r["employee_id"] == caller]

    def update_personal_info(self, caller: str, address: str | None = None, phone: str | None = None,
                             headers: dict | None = None) -> dict:
        self._enter("work_week.update_personal_info", caller, headers, address=address, phone=phone)
        with self._lock:
            if address is not None and len(address.strip()) < 5:
                raise BackendError(422, "Address must be at least 5 characters")
            if phone is not None and not PHONE_RE.match(phone):
                raise BackendError(422, "Invalid phone format")
            info = self._emp(caller)["personal_info"]
            if address is not None:
                info["address"] = address
            if phone is not None:
                info["phone"] = phone
            return {"status": "updated", "personal_info": dict(info)}

    def request_time_off(self, caller: str, start_date: str, end_date: str, leave_type: str, days: float,
                         headers: dict | None = None) -> dict:
        self._enter("work_week.request_time_off", caller, headers, start_date=start_date,
                    end_date=end_date, leave_type=leave_type, days=days)
        with self._lock:
            try:
                s, e = datetime.strptime(start_date, "%Y-%m-%d").date(), datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise BackendError(422, "Dates must be YYYY-MM-DD") from exc
            if s > e:
                raise BackendError(422, "start_date after end_date")
            if s < clock.today():
                raise BackendError(422, "start_date in the past")
            bal = self._emp(caller)["balances"].get(leave_type)
            if bal is None:
                raise BackendError(422, "Unknown leave type")
            if days > bal["remaining"]:
                raise BackendError(422, "Insufficient balance")
            bal["used"] += days
            bal["remaining"] -= days
            rid = f"LR-{self._next_lr}"
            self._next_lr += 1
            req = {"request_id": rid, "employee_id": caller, "leave_type": leave_type, "start_date": start_date,
                   "end_date": end_date, "days": days, "status": "Pending",
                   "submitted_by": (headers or {}).get("X-Actor-Type", "HUMAN")}
            self.leave_requests.append(req)
            return dict(req)

    def cancel_leave_request(self, caller: str, request_id: str, headers: dict | None = None) -> dict:
        self._enter("work_week.cancel_leave_request", caller, headers, request_id=request_id)
        with self._lock:
            req = next((r for r in self.leave_requests if r["request_id"] == request_id), None)
            if req is None:
                raise BackendError(404, "Leave request not found")
            if req["employee_id"] != caller:
                raise BackendError(403, "Cannot act on another employee's records")
            if req["status"] not in ("Pending", "Approved"):
                raise BackendError(409, f"Request already {req['status']}")
            req["status"] = "Cancelled"
            bal = self._emp(caller)["balances"][req["leave_type"]]
            bal["used"] -= req["days"]
            bal["remaining"] += req["days"]
            return {"request_id": request_id, "status": "Cancelled", "refunded_days": req["days"]}

    def get_employee_feedback(self, caller: str, employee_id: str, headers: dict | None = None) -> list:
        """Exists on the vendor host; the agent tool manifest must NEVER expose it (B-5)."""
        self._enter("work_week.get_employee_feedback", caller, headers, employee_id=employee_id)
        if employee_id != caller:
            raise BackendError(403, "Cannot act on another employee's records")
        return list(self.feedback.get(caller, []))

    def get_current_employee_id(self, caller: str, headers: dict | None = None) -> str:
        """Exists on the vendor host; denied to agents (SDD §5.2 — single identity source)."""
        self._enter("work_week.get_current_employee_id", caller, headers)
        return caller

    # ================================================================ ServiceImmediately
    def _own_ticket(self, caller: str, ticket_id: str) -> dict:
        t = self.tickets.get(ticket_id)
        if t is None:
            raise BackendError(404, "Ticket not found")
        if t["requestor_id"] != caller:
            raise BackendError(403, "Cannot act on another employee's records")
        return t

    def get_ticket(self, caller: str, ticket_id: str, headers: dict | None = None) -> dict:
        self._enter("service_immediately.get_ticket", caller, headers, ticket_id=ticket_id)
        return copy.deepcopy(self._own_ticket(caller, ticket_id))

    def list_tickets(self, caller: str, headers: dict | None = None) -> list[dict]:
        self._enter("service_immediately.list_tickets", caller, headers)
        return [
            {k: t[k] for k in ("ticket_id", "short_description", "category", "priority", "status", "created_at")}
            for t in self.tickets.values() if t["requestor_id"] == caller
        ]

    def create_ticket(self, caller: str, category: str, short_description: str, priority: str,
                      description: str = "", headers: dict | None = None) -> dict:
        self._enter("service_immediately.create_ticket", caller, headers, category=category,
                    short_description=short_description, priority=priority)
        with self._lock:
            now = clock.now()
            for t in self.tickets.values():
                if (t["requestor_id"] == caller and t["short_description"].strip().lower()
                        == short_description.strip().lower()
                        and now - datetime.fromisoformat(t["created_at"]) <= timedelta(minutes=5)):
                    raise BackendError(409, f"Duplicate of {t['ticket_id']} within 5 minutes")
            if priority == "1 - Critical" and not any(
                k in f"{short_description} {description}".lower() for k in CRITICAL_KEYWORDS
            ):
                raise BackendError(422, "Critical priority requires qualifying impact keywords")
            tid = f"INC00{self._next_inc}"
            self._next_inc += 1
            h = headers or {}
            t = {"ticket_id": tid, "requestor_id": caller, "category": category, "priority": priority,
                 "status": "New", "short_description": short_description, "description": description,
                 "assignee": "Service Desk Triage", "created_at": now.isoformat(), "comments": [],
                 "created_by_actor_type": h.get("X-Actor-Type", "HUMAN"),
                 "created_by_agent": h.get("X-Agent-Id"), "correlation_id": h.get("X-Correlation-Id")}
            self.tickets[tid] = t
            return copy.deepcopy(t)

    def add_ticket_comment(self, caller: str, ticket_id: str, comment: str, headers: dict | None = None) -> dict:
        self._enter("service_immediately.add_ticket_comment", caller, headers, ticket_id=ticket_id)
        with self._lock:
            t = self._own_ticket(caller, ticket_id)
            c = {"author": caller, "actor_type": (headers or {}).get("X-Actor-Type", "HUMAN"),
                 "text": comment, "at": clock.now().isoformat()}
            t["comments"].append(c)
            return {"ticket_id": ticket_id, "comment": c}

    def update_ticket_status(self, caller: str, ticket_id: str, new_status: str, resolution_notes: str = "",
                             headers: dict | None = None) -> dict:
        self._enter("service_immediately.update_ticket_status", caller, headers, ticket_id=ticket_id,
                    new_status=new_status)
        with self._lock:
            t = self._own_ticket(caller, ticket_id)
            if new_status not in _ALLOWED_TRANSITIONS.get(t["status"], set()):
                raise BackendError(409, f"Invalid transition {t['status']} -> {new_status}")
            t["status"] = new_status
            if resolution_notes:
                t["comments"].append({"author": caller, "actor_type": (headers or {}).get("X-Actor-Type", "HUMAN"),
                                      "text": f"Resolution: {resolution_notes}", "at": clock.now().isoformat()})
            return {"ticket_id": ticket_id, "status": new_status}


_INSTANCE: MockEnterprise | None = None


def get_mock() -> MockEnterprise:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = MockEnterprise()
    return _INSTANCE
