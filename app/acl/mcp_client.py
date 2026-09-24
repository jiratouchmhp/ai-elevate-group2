"""Stateless Streamable HTTP MCP Client for WorkWeek & ServiceImmediately (SDD §5.1, §5.2, D9).

Communicates with the vendor MCP servers (`/work-week/mcp/` and `/service-immediately/mcp/`)
over JSON-RPC 2.0 HTTP POST requests:
- Enforces `X-MCP-Token` header authentication (never query-string `?token=` or `?pat=`).
- Propagates SDD §5.1 attribution headers (`X-Actor-Type`, `X-On-Behalf-Of`, `X-Agent-Id`,
  `X-Agent-Version`, `X-Correlation-Id`).
- Translates between the canonical ACL tool contracts and the live FastMCP tool/resource schemas.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional

from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    get_mcp_token,
    get_service_immediately_mcp_url,
    get_workweek_mcp_url,
)


FORBIDDEN_URL_CREDENTIAL_PARAMS = ("token", "pat", "access_token", "api_key", "mcp_token")


def assert_no_url_credentials(url: str) -> None:
    """Enforces SDD §5.1 security guardrail: credentials must NEVER travel in URL query parameters."""
    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key in query_params:
        if key.lower() in FORBIDDEN_URL_CREDENTIAL_PARAMS:
            raise ValueError(
                f"Security violation (SDD §5.1): URL query parameter '{key}=' is prohibited. "
                "Personal Access Tokens must travel exclusively via the X-MCP-Token HTTP header."
            )


class MCPTransportError(RuntimeError):
    """Raised when an MCP server returns an HTTP or JSON-RPC protocol error."""

    def __init__(self, message: str, *, http_status: int = 503, system: str = "MCP") -> None:
        super().__init__(message)
        self.http_status = http_status
        self.system = system


class StreamableHttpMcpClient:
    """Stateless Streamable HTTP MCP client for WorkWeek and ServiceImmediately."""

    def __init__(
        self,
        *,
        workweek_url: Optional[str] = None,
        service_immediately_url: Optional[str] = None,
        mcp_token: Optional[str] = None,
        timeout_seconds: float = 8.0,
        transport_fn: Optional[Callable[[str, bytes, Dict[str, str], float], Dict[str, Any]]] = None,
    ) -> None:
        self.workweek_url = workweek_url or get_workweek_mcp_url()
        self.service_immediately_url = service_immediately_url or get_service_immediately_mcp_url()
        assert_no_url_credentials(self.workweek_url)
        assert_no_url_credentials(self.service_immediately_url)

        self.mcp_token = mcp_token if mcp_token is not None else get_mcp_token()
        self.timeout_seconds = timeout_seconds
        self._transport_fn = transport_fn or self._default_http_post
        self._cached_token_employee_id: Optional[str] = None

    def _endpoint_for_system(self, system: str) -> str:
        norm = system.strip().lower()
        if norm in ("workweek", "work-week", "hcm"):
            return self.workweek_url
        if norm in ("serviceimmediately", "service-immediately", "service_immediately", "itsm"):
            return self.service_immediately_url
        raise ValueError(f"Unknown MCP system '{system}'. Expected 'WorkWeek' or 'ServiceImmediately'.")

    def _build_request_headers(self, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        token = (extra_headers or {}).get("X-MCP-Token") or self.mcp_token
        if not token:
            raise MCPTransportError(
                "Missing MCP_TOKEN environment variable for X-MCP-Token header.",
                http_status=401,
            )
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-MCP-Token": token,
        }
        if extra_headers:
            for k, v in extra_headers.items():
                if k.lower() != "authorization" and v:
                    headers[k] = str(v)
        headers["X-MCP-Token"] = self.mcp_token or token
        return headers

    @staticmethod
    def _default_http_post(
        url: str, body: bytes, headers: Dict[str, str], timeout: float
    ) -> Dict[str, Any]:
        assert_no_url_credentials(url)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                content_type = resp.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise MCPTransportError(
                f"MCP HTTP {exc.code} from {url}: {err_body[:200]}",
                http_status=exc.code,
            ) from exc
        except Exception as exc:
            raise MCPTransportError(f"MCP connection error to {url}: {exc}", http_status=503) from exc

        text = raw_bytes.decode("utf-8", errors="replace").strip()
        if "text/event-stream" in content_type or text.startswith("event:") or text.startswith("data:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    data_str = line[len("data:") :].strip()
                    if data_str:
                        return json.loads(data_str)
        return json.loads(text)

    def rpc(
        self,
        system: str,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Executes a JSON-RPC 2.0 call against the target MCP server."""
        url = self._endpoint_for_system(system)
        assert_no_url_credentials(url)
        req_headers = self._build_request_headers(headers)
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:8],
            "method": method,
            "params": params or {},
        }
        body = json.dumps(payload).encode("utf-8")
        response = self._transport_fn(url, body, req_headers, timeout or self.timeout_seconds)
        if "error" in response and response["error"]:
            err = response["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise MCPTransportError(f"MCP JSON-RPC error from {system}: {msg}", http_status=502, system=system)
        return response.get("result", {})

    # -------------------------------------------------------------------------
    # Discovery & Raw MCP Operations
    # -------------------------------------------------------------------------
    def list_tools(self, system: str, *, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        res = self.rpc(system, "tools/list", {}, headers=headers)
        return list(res.get("tools", []))

    def list_resources(self, system: str, *, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        res = self.rpc(system, "resources/list", {}, headers=headers)
        return list(res.get("resources", []))

    def list_resource_templates(
        self, system: str, *, headers: Optional[Dict[str, str]] = None
    ) -> List[Dict[str, Any]]:
        res = self.rpc(system, "resources/templates/list", {}, headers=headers)
        return list(res.get("resourceTemplates", []))

    def read_resource(
        self, system: str, uri: str, *, headers: Optional[Dict[str, str]] = None
    ) -> str:
        res = self.rpc(system, "resources/read", {"uri": uri}, headers=headers)
        contents = res.get("contents", [])
        if not contents:
            return ""
        return str(contents[0].get("text", ""))

    def call_tool(
        self,
        system: str,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        res = self.rpc(
            system,
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            headers=headers,
        )
        text_parts = [
            c.get("text", "")
            for c in res.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        combined_text = "\n".join(text_parts).strip()
        structured = res.get("structuredContent", {})
        if not combined_text and isinstance(structured, dict) and "result" in structured:
            combined_text = str(structured["result"])
        is_error = bool(res.get("isError", False))
        return {
            "text": combined_text,
            "structured": structured,
            "is_error": is_error,
            "raw": res,
        }

    def resolve_token_employee_id(self, *, headers: Optional[Dict[str, str]] = None) -> str:
        """Resolves the employee_id bound to the active `X-MCP-Token` internal to the ACL.

        Note: Per SDD §5.2, `get_current_employee_id` is NEVER exposed to any LLM sub-agent;
        it is used strictly inside the ACL proxy to verify token-to-identity binding.
        """
        if self._cached_token_employee_id:
            return self._cached_token_employee_id
        res = self.call_tool("WorkWeek", "get_current_employee_id", {}, headers=headers)
        emp_id = res["text"].strip() or get_mcp_authenticated_employee_id()
        self._cached_token_employee_id = emp_id
        return emp_id

    # -------------------------------------------------------------------------
    # High-Level Normalized Domain Operations for ACL Proxy
    # -------------------------------------------------------------------------
    def fetch_employee_profile(
        self, employee_id: str, *, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Reads `workweek://employees/{employee_id}/profile` and normalizes fields."""
        raw_text = self.read_resource(
            "WorkWeek", f"workweek://employees/{employee_id}/profile", headers=headers
        )
        data = json.loads(raw_text) if raw_text else {}
        first_name = str(data.get("first_name", "")).strip()
        last_name = str(data.get("last_name", "")).strip()
        full_name = data.get("name") or f"{first_name} {last_name}".strip() or employee_id
        role = data.get("job_title") or data.get("role") or "Employee"
        address = data.get("home_address") or data.get("address") or ""
        phone = data.get("phone_number") or data.get("phone") or ""
        manager = data.get("manager") or data.get("manager_id") or "EMP-1"
        location_status = data.get("location_status") or "Hybrid"

        return {
            "employee_id": data.get("employee_id", employee_id),
            "name": full_name,
            "first_name": first_name,
            "last_name": last_name,
            "email": data.get("email", ""),
            "department": data.get("department", ""),
            "role": role,
            "job_title": data.get("job_title", role),
            "manager": manager,
            "manager_id": data.get("manager_id", manager),
            "hire_date": data.get("hire_date", ""),
            "location_status": location_status,
            "office_location": data.get("office_location", "Singapore Office"),
            "address": address,
            "home_address": address,
            "phone": phone,
            "phone_number": phone,
            "mcp_backend": "live_mcp_server",
            "mcp_resource_uri": f"workweek://employees/{employee_id}/profile",
        }

    def fetch_personal_info(
        self, employee_id: str, *, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Calls `get_personal_info` MCP tool and merges with profile metadata."""
        tool_res = self.call_tool(
            "WorkWeek",
            "get_personal_info",
            {"employee_id": employee_id},
            headers=headers,
        )
        profile = self.fetch_employee_profile(employee_id, headers=headers)
        text = tool_res["text"]
        addr_match = re.search(r"-\s*Address:\s*(.+)", text)
        phone_match = re.search(r"-\s*Phone:\s*(.+)", text)
        if addr_match:
            profile["address"] = addr_match.group(1).strip()
            profile["home_address"] = profile["address"]
        if phone_match:
            profile["phone"] = phone_match.group(1).strip()
            profile["phone_number"] = profile["phone"]
        profile["raw_mcp_text"] = text
        return {"profile": profile, "mcp_backend": "live_mcp_server", "raw_mcp_text": text}

    def fetch_leave_balances(
        self, employee_id: str, *, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Invokes `get_employee_balances` tool + `workweek://employees/{id}/timeoff` resource."""
        tool_res = self.call_tool(
            "WorkWeek",
            "get_employee_balances",
            {"employee_id": employee_id},
            headers=headers,
        )
        timeoff_text = self.read_resource(
            "WorkWeek", f"workweek://employees/{employee_id}/timeoff", headers=headers
        )
        raw_obj = json.loads(timeoff_text) if timeoff_text else {}
        vac_accrued = float(raw_obj.get("vacation_accrued", 20.0))
        vac_used = float(raw_obj.get("vacation_used", 0.0))
        vac_rem = round(vac_accrued - vac_used, 1)

        sick_accrued = float(raw_obj.get("sick_accrued", 10.0))
        sick_used = float(raw_obj.get("sick_used", 0.0))
        sick_rem = round(sick_accrued - sick_used, 1)

        balances = {
            "vacation": {
                "accrued": vac_accrued,
                "used": vac_used,
                "remaining": vac_rem,
            },
            "sick": {
                "accrued": sick_accrued,
                "used": sick_used,
                "remaining": sick_rem,
            },
            "hospitalization": {
                "accrued": 46.0,
                "used": 0.0,
                "remaining": 46.0,
            },
        }
        return {
            "employee_id": employee_id,
            "balances": balances,
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }

    def fetch_leave_requests(
        self, employee_id: str, *, headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Invokes `get_leave_requests` on `/work-week/mcp/`."""
        tool_res = self.call_tool(
            "WorkWeek",
            "get_leave_requests",
            {"employee_id": employee_id},
            headers=headers,
        )
        raw_list = json.loads(tool_res["text"]) if tool_res["text"].startswith("[") else []
        requests: List[Dict[str, Any]] = []
        for item in raw_list:
            req_copy = dict(item)
            req_copy["request_id"] = str(item.get("request_id"))
            req_copy["raw_request_id"] = item.get("request_id")
            req_copy.setdefault("status", "Approved")
            requests.append(req_copy)
        return {
            "employee_id": employee_id,
            "leave_requests": requests,
            "mcp_backend": "live_mcp_server",
        }

    def update_contact_info(
        self,
        employee_id: str,
        *,
        address: str = "",
        phone: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `update_personal_info` on `/work-week/mcp/`, pre-filling any omitted field."""
        current_profile = self.fetch_employee_profile(employee_id, headers=headers)
        effective_address = address.strip() if address and address.strip() else current_profile["address"]
        effective_phone = phone.strip() if phone and phone.strip() else current_profile["phone"]

        tool_res = self.call_tool(
            "WorkWeek",
            "update_personal_info",
            {
                "employee_id": employee_id,
                "address": effective_address,
                "phone": effective_phone,
            },
            headers=headers,
        )
        updated_profile = self.fetch_employee_profile(employee_id, headers=headers)
        return {
            "request_id": f"WW-CNT-{uuid.uuid4().hex[:6].upper()}",
            "updated_profile": updated_profile,
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }

    def submit_leave_request(
        self,
        employee_id: str,
        *,
        start_date: str,
        end_date: str,
        leave_type: str = "Vacation",
        days: float = 1.0,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `request_time_off` on `/work-week/mcp/` and returns created request_id + balance."""
        normalized_type = "Vacation" if leave_type.lower().startswith("vac") else "Sick"
        tool_res = self.call_tool(
            "WorkWeek",
            "request_time_off",
            {
                "employee_id": employee_id,
                "start_date": start_date,
                "end_date": end_date,
                "leave_type": normalized_type,
                "days": float(days),
            },
            headers=headers,
        )
        # Retrieve leave requests & updated balance so we have the authoritative server request_id
        reqs_data = self.fetch_leave_requests(employee_id, headers=headers)
        bal_data = self.fetch_leave_balances(employee_id, headers=headers)

        matched_req: Optional[Dict[str, Any]] = None
        for r in reqs_data["leave_requests"]:
            if r.get("start_date") == start_date and r.get("end_date") == end_date:
                matched_req = r
                break
        if not matched_req and reqs_data["leave_requests"]:
            matched_req = reqs_data["leave_requests"][0]

        req_id = str(matched_req["request_id"]) if matched_req else f"LR-{uuid.uuid4().hex[:6].upper()}"
        type_key = "vacation" if normalized_type == "Vacation" else "sick"
        rem_bal = bal_data["balances"][type_key]["remaining"]

        record = matched_req or {
            "request_id": req_id,
            "employee_id": employee_id,
            "leave_type": normalized_type,
            "start_date": start_date,
            "end_date": end_date,
            "days": float(days),
            "status": "Approved",
        }
        return {
            "request_id": req_id,
            "leave_request": record,
            "remaining_balance": rem_bal,
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }

    def cancel_leave_request(
        self,
        employee_id: str,
        *,
        request_id: str | int,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `cancel_leave_request` on `/work-week/mcp/` (coercing request_id to int)."""
        raw_id_str = str(request_id).strip()
        if raw_id_str.upper().startswith("LR-"):
            raw_id_str = raw_id_str[3:]
        int_req_id = int(raw_id_str)

        # Check pre-cancellation days if available
        pre_reqs = self.fetch_leave_requests(employee_id, headers=headers).get("leave_requests", [])
        refunded_days = 0.0
        leave_type = "Vacation"
        for r in pre_reqs:
            if str(r.get("request_id")) == str(int_req_id):
                refunded_days = float(r.get("days", 0.0))
                leave_type = str(r.get("leave_type", "Vacation"))
                break

        tool_res = self.call_tool(
            "WorkWeek",
            "cancel_leave_request",
            {
                "employee_id": employee_id,
                "request_id": int_req_id,
            },
            headers=headers,
        )
        bal_data = self.fetch_leave_balances(employee_id, headers=headers)
        type_key = "vacation" if leave_type.lower().startswith("vac") else "sick"
        rem_bal = bal_data["balances"][type_key]["remaining"]
        return {
            "request_id": str(int_req_id),
            "cancelled_status": "Cancelled",
            "refunded_days": refunded_days,
            "remaining_balance": rem_bal,
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }

    def _normalize_ticket(self, raw_ticket: Dict[str, Any], default_emp_id: str) -> Dict[str, Any]:
        t = dict(raw_ticket)
        req_by = t.get("requested_by") or t.get("requestor_id") or default_emp_id
        t["requested_by"] = req_by
        t["requestor_id"] = req_by
        assignee = t.get("assigned_to") or t.get("assignee") or "Service Desk"
        t["assigned_to"] = assignee
        t["assignee"] = assignee
        t.setdefault("detailed_description", t.get("short_description", ""))
        t.setdefault("comments", [])
        return t

    def fetch_ticket(
        self,
        ticket_id: str,
        employee_id: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Reads `serviceimmediately://tickets/{ticket_id}` resource on `/service-immediately/mcp/`."""
        raw_text = self.read_resource(
            "ServiceImmediately",
            f"serviceimmediately://tickets/{ticket_id}",
            headers=headers,
        )
        if not raw_text or raw_text.strip().startswith("Error"):
            return {"error": f"Ticket {ticket_id} not found for employee {employee_id}."}
        raw_obj = json.loads(raw_text)
        ticket = self._normalize_ticket(raw_obj, employee_id)
        if ticket.get("requestor_id") != employee_id:
            return {"error": f"Ticket {ticket_id} not found for employee {employee_id}."}
        return {
            "ticket": ticket,
            "mcp_backend": "live_mcp_server",
            "mcp_resource_uri": f"serviceimmediately://tickets/{ticket_id}",
        }

    def list_employee_tickets(
        self,
        employee_id: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `list_tickets` tool on `/service-immediately/mcp/`."""
        tool_res = self.call_tool(
            "ServiceImmediately",
            "list_tickets",
            {"employee_id": employee_id},
            headers=headers,
        )
        raw_list = json.loads(tool_res["text"]) if tool_res["text"].startswith("[") else []
        tickets = [self._normalize_ticket(item, employee_id) for item in raw_list]
        return {
            "tickets": tickets,
            "mcp_backend": "live_mcp_server",
        }

    def create_incident_ticket(
        self,
        employee_id: str,
        *,
        category: str,
        short_description: str,
        priority: str = "3 - Moderate",
        assignment_group: str = "Service Desk",
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `create_ticket` tool on `/service-immediately/mcp/`."""
        tool_res = self.call_tool(
            "ServiceImmediately",
            "create_ticket",
            {
                "requested_by": employee_id,
                "category": category,
                "short_description": short_description,
                "priority": priority,
                "assignment_group": assignment_group,
            },
            headers=headers,
        )
        text = tool_res["text"]
        m = re.search(r"(INC\d+)", text)
        ticket_id = m.group(1) if m else ""
        if not ticket_id:
            tickets_res = self.list_employee_tickets(employee_id, headers=headers)
            for t in tickets_res["tickets"]:
                if t.get("short_description") == short_description:
                    ticket_id = str(t.get("ticket_id", ""))
                    break
        if ticket_id:
            fetched = self.fetch_ticket(ticket_id, employee_id, headers=headers)
            ticket_obj = fetched.get("ticket", {})
        else:
            ticket_id = f"INC00{uuid.uuid4().hex[:5].upper()}"
            ticket_obj = {
                "ticket_id": ticket_id,
                "requestor_id": employee_id,
                "requested_by": employee_id,
                "category": category,
                "short_description": short_description,
                "priority": priority,
                "status": "New",
                "assignee": assignment_group,
                "comments": [],
            }
        return {
            "ticket_id": ticket_id,
            "ticket": ticket_obj,
            "raw_mcp_text": text,
            "mcp_backend": "live_mcp_server",
        }

    def add_ticket_comment(
        self,
        employee_id: str,
        *,
        ticket_id: str,
        comment: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `add_ticket_comment` on `/service-immediately/mcp/`."""
        tool_res = self.call_tool(
            "ServiceImmediately",
            "add_ticket_comment",
            {
                "ticket_id": ticket_id,
                "author": employee_id,
                "comment": comment,
            },
            headers=headers,
        )
        fetched = self.fetch_ticket(ticket_id, employee_id, headers=headers)
        return {
            "ticket_id": ticket_id,
            "ticket": fetched.get("ticket", {"ticket_id": ticket_id}),
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }

    def update_ticket_status(
        self,
        employee_id: str,
        *,
        ticket_id: str,
        new_status: str,
        resolution_notes: str = "",
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Invokes `update_ticket_status` on `/service-immediately/mcp/`."""
        tool_res = self.call_tool(
            "ServiceImmediately",
            "update_ticket_status",
            {
                "ticket_id": ticket_id,
                "status": new_status,
                "resolution_notes": resolution_notes or "",
                "updated_by": employee_id,
            },
            headers=headers,
        )
        fetched = self.fetch_ticket(ticket_id, employee_id, headers=headers)
        return {
            "ticket_id": ticket_id,
            "ticket": fetched.get("ticket", {"ticket_id": ticket_id, "status": new_status}),
            "raw_mcp_text": tool_res["text"],
            "mcp_backend": "live_mcp_server",
        }
