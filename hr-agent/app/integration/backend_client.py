"""Backend transports for the Anti-Corruption Layer (SDD §5.1).

* `InProcessBackend` — calls the mock domain directly (default; deterministic eval).
* `McpBackend` — MCP over stateless streamable HTTP to `/work-week/mcp/` and
  `/service-immediately/mcp/` with a per-persona PAT in `X-MCP-Token` (GFE
  intercepts `Authorization`, §5.1). Talks to the real vendor host through
  `VendorAdapter`, which translates the vendor's wire contract (explicit
  `employee_id`/`requested_by` params, prose/JSON-string results, resources for
  single-record reads) into the canonical shapes the PDP and agents consume.

Both expose the same `call(op, caller, headers, **kwargs)` contract using vendor
operation names, so traces stay comparable to backend logs (§5.2).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol

from app import clock, config
from app.integration.errors import BackendError

# op -> (system path, vendor tool name)
VENDOR_OPS: dict[str, tuple[str, str]] = {
    "work_week.get_profile": ("work-week", "get_profile"),
    "work_week.get_personal_info": ("work-week", "get_personal_info"),
    "work_week.get_employee_balances": ("work-week", "get_employee_balances"),
    "work_week.get_leave_requests": ("work-week", "get_leave_requests"),
    "work_week.update_personal_info": ("work-week", "update_personal_info"),
    "work_week.request_time_off": ("work-week", "request_time_off"),
    "work_week.cancel_leave_request": ("work-week", "cancel_leave_request"),
    "service_immediately.get_ticket": ("service-immediately", "get_ticket"),
    "service_immediately.list_tickets": ("service-immediately", "list_tickets"),
    "service_immediately.create_ticket": ("service-immediately", "create_ticket"),
    "service_immediately.add_ticket_comment": ("service-immediately", "add_ticket_comment"),
    "service_immediately.update_ticket_status": ("service-immediately", "update_ticket_status"),
}

WW, SI = "work-week", "service-immediately"


class Backend(Protocol):
    async def call(self, op: str, caller: str, headers: dict, **kwargs: Any) -> Any: ...


class InProcessBackend:
    async def call(self, op: str, caller: str, headers: dict, **kwargs: Any) -> Any:
        from mock_backends.domain import (
            get_mock,  # dev/eval only — not shipped in prod images
        )

        if op not in VENDOR_OPS:
            raise BackendError(403, f"Operation {op} is not exposed by the ACL")
        method = getattr(get_mock(), VENDOR_OPS[op][1])
        return method(caller, headers=headers, **kwargs)


# --------------------------------------------------------------------------- vendor adapter
class Wire(Protocol):
    """Raw vendor transport: returns the text payload of a tool call / resource read."""

    async def tool(self, system: str, name: str, args: dict) -> str: ...

    async def resource(self, system: str, uri: str) -> str: ...


_NOT_FOUND = re.compile(r"\bnot found\b|\bdoes not exist\b|\bno such\b", re.I)
_FAILURE_PREFIX = re.compile(
    r"^\s*(error|failed|failure|invalid|insufficient|cannot|can't|unable|denied|forbidden|unauthori[sz]ed)\b", re.I)
_BALANCE_LINE = re.compile(
    r"^\s*-?\s*(?P<type>[A-Za-z ]+?):\s*(?P<remaining>-?[\d.]+)\s*days?\s*remaining\s*"
    r"\(\s*(?P<used>-?[\d.]+)\s*/\s*(?P<accrued>-?[\d.]+)\s*used\s*\)", re.I | re.M)
_KV_LINE = re.compile(r"^\s*-?\s*(?P<key>[A-Za-z _]+?)\s*:\s*(?P<val>.+?)\s*$", re.M)
_TICKET_ID = re.compile(r"\bINC\d+\b")
_REQUEST_ID = re.compile(r"request(?:[ _]?id)?\s*(?:#|:|=)?\s*(\d+)", re.I)


def _check_text(text: str) -> None:
    """The vendor reports business failures as plain (non-error) text or `{"error": ...}`."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and obj.get("error"):
        msg = str(obj["error"])
        raise BackendError(404 if _NOT_FOUND.search(msg) else 422, msg)
    if isinstance(obj, (dict, list)):
        return  # structured payload — never scan record content for failure phrases
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    if _NOT_FOUND.search(first):
        raise BackendError(404, text)
    if _FAILURE_PREFIX.match(first):
        raise BackendError(422, text)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        raise BackendError(502, f"Unexpected vendor response: {text[:200]}") from exc


def _iso_local(ts: Any) -> Any:
    """Vendor timestamps are naive UTC; the PDP compares against tz-aware SGT `clock.now()`."""
    if not isinstance(ts, str) or not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(clock.TZ).isoformat()


def parse_balances(text: str) -> dict:
    out: dict[str, dict] = {}
    for m in _BALANCE_LINE.finditer(text):
        out[m["type"].strip().title()] = {
            "accrued": float(m["accrued"]), "used": float(m["used"]), "remaining": float(m["remaining"])}
    if not out:
        raise BackendError(502, f"Unexpected balance format: {text[:200]}")
    return out


def parse_personal_info(text: str) -> dict:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return {"address": obj.get("address") or obj.get("home_address"),
                    "phone": obj.get("phone") or obj.get("phone_number")}
    except (ValueError, TypeError):
        pass
    kv = {m["key"].strip().lower().replace(" ", "_"): m["val"] for m in _KV_LINE.finditer(text)}
    return {"address": kv.get("address") or kv.get("home_address"),
            "phone": kv.get("phone") or kv.get("phone_number")}


def _leave(r: dict) -> dict:
    return {"request_id": str(r.get("request_id")), "employee_id": r.get("employee_id"),
            "leave_type": r.get("leave_type"), "start_date": r.get("start_date"),
            "end_date": r.get("end_date"), "days": r.get("days"),
            # The vendor has no approval workflow/status field; an existing record is live.
            "status": r.get("status") or "Approved"}


def _ticket(t: dict) -> dict:
    return {"ticket_id": t.get("ticket_id"), "requestor_id": t.get("requested_by"),
            "category": t.get("category"), "priority": t.get("priority"), "status": t.get("status"),
            "short_description": t.get("short_description"), "description": t.get("description", ""),
            "assignee": t.get("assigned_to") or t.get("assignment_group"),
            "created_at": _iso_local(t.get("created_at")), "updated_at": _iso_local(t.get("updated_at")),
            "comments": t.get("comments", [])}


class VendorAdapter:
    """Maps canonical ACL operations onto the real vendor contract (SDD §5.1 ACL)."""

    def __init__(self, wire: Wire) -> None:
        self.wire = wire

    async def _tool(self, system: str, name: str, args: dict) -> str:
        text = await self.wire.tool(system, name, args)
        _check_text(text)
        return text

    async def call(self, op: str, caller: str, **kw: Any) -> Any:
        handler = getattr(self, "_" + op.split(".", 1)[1], None)
        if op not in VENDOR_OPS or handler is None:
            raise BackendError(403, f"Operation {op} is not exposed by the ACL")
        return await handler(caller, **kw)

    # ------------------------------------------------------------------ WorkWeek reads
    async def _get_profile(self, caller: str) -> dict:
        text = await self.wire.resource(WW, f"workweek://employees/{caller}/profile")
        _check_text(text)
        p = _json(text)
        return {
            "employee_id": p.get("employee_id"),
            "name": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or p.get("name"),
            "email": p.get("email"), "department": p.get("department"),
            "role": p.get("role") or p.get("job_title"), "job_title": p.get("job_title"),
            "manager_id": p.get("manager_id"), "hire_date": p.get("hire_date"),
            # Not modelled by the vendor; None keeps equipment eligibility fail-closed (§5.4).
            "location_status": p.get("location_status") or config.VENDOR_DEFAULT_LOCATION_STATUS or None,
            "office": p.get("supervisory_org"),
            "address": p.get("home_address") or p.get("address"),
            "phone": p.get("phone_number") or p.get("phone"),
        }

    async def _get_personal_info(self, caller: str) -> dict:
        return parse_personal_info(await self._tool(WW, "get_personal_info", {"employee_id": caller}))

    async def _get_employee_balances(self, caller: str) -> dict:
        return parse_balances(await self._tool(WW, "get_employee_balances", {"employee_id": caller}))

    async def _get_leave_requests(self, caller: str) -> list[dict]:
        rows = _json(await self._tool(WW, "get_leave_requests", {"employee_id": caller}))
        return [_leave(r) for r in rows if r.get("employee_id") in (None, caller)]

    # ------------------------------------------------------------------ WorkWeek writes
    async def _update_personal_info(self, caller: str, address: str | None = None,
                                    phone: str | None = None) -> dict:
        if address is None or phone is None:  # vendor requires both fields
            current = await self._get_personal_info(caller)
            address = current.get("address") if address is None else address
            phone = current.get("phone") if phone is None else phone
        text = await self._tool(WW, "update_personal_info",
                                {"employee_id": caller, "address": address, "phone": phone})
        return {"status": "updated", "personal_info": {"address": address, "phone": phone}, "message": text}

    async def _request_time_off(self, caller: str, start_date: str, end_date: str, leave_type: str,
                                days: float) -> dict:
        text = await self._tool(WW, "request_time_off", {
            "employee_id": caller, "start_date": start_date, "end_date": end_date,
            "leave_type": leave_type, "days": days})
        rid = None
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("request_id") is not None:
                rid = str(obj["request_id"])
        except (ValueError, TypeError):
            m = _REQUEST_ID.search(text)
            rid = m.group(1) if m else None
        if rid is None:  # resolve the reference from the ledger of record
            match = [r for r in await self._get_leave_requests(caller)
                     if (r["start_date"], r["end_date"], r["leave_type"]) == (start_date, end_date, leave_type)]
            if match:
                rid = max(match, key=lambda r: int(r["request_id"]) if r["request_id"].isdigit() else -1)[
                    "request_id"]
        return {"request_id": rid, "employee_id": caller, "leave_type": leave_type, "start_date": start_date,
                "end_date": end_date, "days": days, "status": "Approved", "message": text}

    async def _cancel_leave_request(self, caller: str, request_id: str) -> dict:
        try:
            rid = int(str(request_id).strip())
        except ValueError as exc:
            raise BackendError(404, f"Leave request {request_id} not found") from exc
        text = await self._tool(WW, "cancel_leave_request", {"employee_id": caller, "request_id": rid})
        return {"request_id": str(rid), "status": "Cancelled", "message": text}

    # ------------------------------------------------------------------ ServiceImmediately
    async def _get_ticket(self, caller: str, ticket_id: str) -> dict:
        text = await self.wire.resource(SI, f"serviceimmediately://tickets/{ticket_id}")
        _check_text(text)
        t = _ticket(_json(text))
        if t["requestor_id"] != caller:
            raise BackendError(403, "Cannot act on another employee's records")
        return t

    async def _list_tickets(self, caller: str) -> list[dict]:
        rows = _json(await self._tool(SI, "list_tickets", {"employee_id": caller}))
        return [{k: v for k, v in _ticket(t).items() if k in (
            "ticket_id", "short_description", "category", "priority", "status", "created_at")}
            for t in rows if t.get("requested_by") in (None, caller)]

    async def _create_ticket(self, caller: str, category: str, short_description: str, priority: str,
                             description: str = "") -> dict:
        text = await self._tool(SI, "create_ticket", {
            "requested_by": caller, "category": category, "short_description": short_description,
            "priority": priority})
        m = _TICKET_ID.search(text)
        tid = m.group(0) if m else None
        if tid is None:
            same = [t for t in await self._list_tickets(caller) if t["short_description"] == short_description]
            tid = max(same, key=lambda t: t.get("created_at") or "")["ticket_id"] if same else None
        if tid and description.strip():  # vendor has no description field — keep detail in the activity log
            await self._tool(SI, "add_ticket_comment", {"ticket_id": tid, "author": caller,
                                                         "comment": description.strip()})
        return {"ticket_id": tid, "requestor_id": caller, "category": category, "priority": priority,
                "status": "New", "short_description": short_description, "description": description,
                "message": text}

    async def _add_ticket_comment(self, caller: str, ticket_id: str, comment: str) -> dict:
        await self._get_ticket(caller, ticket_id)  # ownership enforced before the write
        text = await self._tool(SI, "add_ticket_comment", {"ticket_id": ticket_id, "author": caller,
                                                            "comment": comment})
        return {"ticket_id": ticket_id, "comment": {"author": caller, "text": comment}, "message": text}

    async def _update_ticket_status(self, caller: str, ticket_id: str, new_status: str,
                                    resolution_notes: str = "") -> dict:
        await self._get_ticket(caller, ticket_id)
        text = await self._tool(SI, "update_ticket_status", {
            "ticket_id": ticket_id, "status": new_status, "resolution_notes": resolution_notes or "",
            "updated_by": caller})
        return {"ticket_id": ticket_id, "status": new_status, "message": text}


class _FastMcpWire:  # pragma: no cover - network transport, exercised in integration runs
    def __init__(self, base_url: str, token: str, headers: dict) -> None:
        self.base_url, self.token, self.headers = base_url, token, headers

    def _client(self, system: str):
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        from app.integration.http_pool import pooled_client_factory

        return Client(StreamableHttpTransport(
            f"{self.base_url}/{system}/mcp/", headers={"X-MCP-Token": self.token, **self.headers},
            httpx_client_factory=pooled_client_factory))

    async def tool(self, system: str, name: str, args: dict) -> str:
        async with self._client(system) as client:
            result = await client.call_tool(name, args, raise_on_error=False)
        text = " ".join(getattr(c, "text", "") or "" for c in result.content)
        if result.is_error:
            status = 503
            if text.startswith("[") and "]" in text:
                try:
                    status = int(text[1:text.index("]")])
                except ValueError:
                    pass
            elif "validation error" in text.lower():
                status = 422
            raise BackendError(status, text)
        sc = result.structured_content
        if isinstance(sc, dict) and isinstance(sc.get("result"), str):
            return sc["result"]
        return text

    async def resource(self, system: str, uri: str) -> str:
        async with self._client(system) as client:
            try:
                contents = await client.read_resource(uri)
            except Exception as exc:
                msg = str(exc)
                raise BackendError(404 if _NOT_FOUND.search(msg) else 503, msg) from exc
        return "".join(getattr(c, "text", "") or "" for c in contents)


class McpBackend:
    def __init__(self, base_url: str | None = None, tokens: dict[str, str] | None = None,
                 wire_factory: Any = None) -> None:
        self.base_url = (base_url or config.BACKEND_BASE_URL).rstrip("/")
        self.tokens = tokens or (json.loads(config.BACKEND_TOKENS_JSON) if config.BACKEND_TOKENS_JSON else {})
        self.wire_factory = wire_factory or _FastMcpWire

    async def call(self, op: str, caller: str, headers: dict, **kwargs: Any) -> Any:
        if op not in VENDOR_OPS:
            raise BackendError(403, f"Operation {op} is not exposed by the ACL")
        token = self.tokens.get(caller) or config.BACKEND_SHARED_TOKEN
        if not token:
            raise BackendError(401, f"No credential provisioned for persona {caller} (OQ-12)")
        adapter = VendorAdapter(self.wire_factory(self.base_url, token, dict(headers or {})))
        return await adapter.call(op, caller, **kwargs)


def get_backend() -> Backend:
    return McpBackend() if config.BACKEND_MODE == "mcp" else InProcessBackend()
