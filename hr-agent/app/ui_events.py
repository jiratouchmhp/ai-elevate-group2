"""ADK event -> AG-UI event translation for the chat UI (SDD §3.10).

Pure and model-free so it can be unit tested. The BFF (`app/ui_bff.py`) feeds every ADK
`Event` from `Runner.run_async` through `AguiTranslator.translate()` and streams the
resulting dicts as SSE. Event names/shapes follow the AG-UI protocol; the three
`CUSTOM` events (`citation`, `transaction`, `guardrail_block`) carry the HR-specific
trust affordances. No business logic lives here — it only re-shapes what the agent and
the ACL already decided.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from app.guardrails.spii import redact

SPECIALISTS = {"policy_agent", "workweek_agent", "itsm_agent"}

TOOL_LABELS: dict[str, str] = {
    "policy_agent": "Consulting the policy specialist",
    "workweek_agent": "Consulting the WorkWeek specialist",
    "itsm_agent": "Consulting the service-desk specialist",
    "search_policy": "Searching the Employee Policy Handbook",
    "get_profile": "Checking your WorkWeek profile",
    "get_personal_info": "Reading your contact details in WorkWeek",
    "get_leave_balance": "Checking your leave balance in WorkWeek",
    "get_leave_requests": "Listing your leave requests",
    "propose_leave": "Validating your leave request against policy",
    "propose_cancel_leave": "Validating the leave cancellation",
    "propose_contact_update": "Validating your contact-detail change",
    "list_tickets": "Listing your ServiceImmediately tickets",
    "get_ticket": "Opening the ticket in ServiceImmediately",
    "propose_incident": "Validating the new ticket against policy",
    "propose_comment": "Preparing your ticket comment",
    "propose_status_update": "Validating the ticket status change",
    "commit_action": "Submitting your confirmed request",
}

ACTION_TITLES: dict[str, str] = {
    "submit_leave": "Submit leave request",
    "cancel_leave": "Cancel leave request",
    "update_contact": "Update contact details",
    "create_incident": "Create service-desk ticket",
    "add_comment": "Add ticket comment",
    "update_status": "Change ticket status",
}

# Which node of the UI "Agent Flow" graph a tool call lights up. Specialists map to themselves;
# backend tools map to the system of record they reach (via the ACL/PDP for workweek/itsm).
TOOL_SYSTEM: dict[str, str] = {
    "policy_agent": "policy_agent", "workweek_agent": "workweek_agent", "itsm_agent": "itsm_agent",
    "search_policy": "handbook",
    **dict.fromkeys(("get_profile", "get_personal_info", "get_leave_balance", "get_leave_requests",
                     "propose_leave", "propose_cancel_leave", "propose_contact_update"), "workweek"),
    **dict.fromkeys(("list_tickets", "get_ticket", "propose_incident", "propose_comment",
                     "propose_status_update"), "itsm"),
}
_COMMIT_SYSTEM = {"workweek_agent": "workweek", "itsm_agent": "itsm"}


def tool_system(name: str, author: str) -> str | None:
    if name == "commit_action":
        return _COMMIT_SYSTEM.get(author)
    return TOOL_SYSTEM.get(name)


# ------------------------------------------------------------------ generative-UI widgets
# Strict allow-list of fields per widget (FR-1.4): home address, phone, email and NRIC never
# reach the widget stream; free text (ticket descriptions/comments) is SPII-redacted.
MAX_ROWS = 10
_TICKET_FIELDS = ("ticket_id", "short_description", "category", "priority", "status", "created_at", "assignee")
_PROFILE_FIELDS = ("name", "department", "role", "hire_date", "location_status", "office", "manager_name")
_LEAVE_REQ_FIELDS = ("request_id", "leave_type", "start_date", "end_date", "days", "status")


def _clean(text: Any) -> str | None:
    if text is None:
        return None
    return redact(str(text), phones=True, emails=True)[0]


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(data: Any, *keys: str) -> list[dict]:
    """Accept a bare list or a dict wrapping one (vendor MCP and mock shapes differ)."""
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                data = data[k]
                break
    return [r for r in data if isinstance(r, dict)][:MAX_ROWS] if isinstance(data, list) else []


def _pick(row: dict, fields: tuple[str, ...]) -> dict:
    return {k: _jsonable(row[k]) for k in fields if row.get(k) not in (None, "")}


def _ticket_lite(row: dict) -> dict:
    out = _pick(row, _TICKET_FIELDS)
    if "short_description" in out:
        out["short_description"] = _clean(out["short_description"])
    return out


def _balance_items(data: Any) -> list[tuple[str, dict]]:
    src = data.get("balances", data) if isinstance(data, dict) else data
    if isinstance(src, dict):
        return [(str(k), v) for k, v in src.items() if isinstance(v, dict)]
    if isinstance(src, list):
        return [(str(r.get("leave_type") or r.get("type") or "Leave"), r) for r in src if isinstance(r, dict)]
    return []


def _w_leave_balance(data: Any) -> dict | None:
    balances = []
    for name, r in _balance_items(data)[:MAX_ROWS]:
        accrued = _num(r.get("accrued", r.get("entitlement")))
        used = _num(r.get("used", r.get("taken")))
        pending = _num(r.get("pending"))
        remaining = _num(r.get("remaining"))
        if remaining is None and accrued is not None:
            remaining = accrued - (used or 0) - (pending or 0)
        if accrued is None and remaining is None:
            continue
        balances.append({"type": name, "accrued": accrued, "used": used, "pending": pending, "remaining": remaining})
    return {"balances": balances} if balances else None


def _w_leave_requests(data: Any) -> dict | None:
    return {"requests": [_pick(r, _LEAVE_REQ_FIELDS) for r in _rows(data, "requests", "leave_requests", "items")]}


def _w_ticket_list(data: Any) -> dict | None:
    return {"tickets": [_ticket_lite(r) for r in _rows(data, "tickets", "items", "results")]}


def _comment_by(c: dict, requestor: Any) -> str:
    if str(c.get("actor_type", "")).upper().startswith("AUTOMATED"):
        return "assistant"
    return "you" if requestor and c.get("author") == requestor else "support"


def _w_ticket_detail(data: Any) -> dict | None:
    t = data.get("ticket", data) if isinstance(data, dict) else None
    if not isinstance(t, dict) or not t.get("ticket_id"):
        return None
    out = _ticket_lite(t)
    if t.get("description"):
        out["description"] = _clean(t["description"])
    out["comments"] = [
        {"by": _comment_by(c, t.get("requestor_id")), "text": _clean(c.get("text") or c.get("comment")),
         "at": _jsonable(c.get("at") or c.get("created_at"))}
        for c in _rows(t.get("comments") or [])
    ]
    return out


def _w_profile(data: Any) -> dict | None:
    return (_pick(data, _PROFILE_FIELDS) or None) if isinstance(data, dict) else None


WIDGETS: dict[str, tuple[str, Callable[[Any], dict | None]]] = {
    "get_leave_balance": ("leave_balance", _w_leave_balance),
    "get_leave_requests": ("leave_requests", _w_leave_requests),
    "list_tickets": ("ticket_list", _w_ticket_list),
    "get_ticket": ("ticket_detail", _w_ticket_detail),
    "get_profile": ("profile", _w_profile),
    # get_personal_info is deliberately absent: address/phone are SPII (FR-1.4).
}

# Non-technical failure copy (SDD §5.4 / NFR-4.1). Never surface stack traces.
RUN_ERROR_MESSAGE = (
    "Sorry — I couldn't complete that just now. Nothing was submitted on your behalf. "
    "Please try again in a moment, or contact HR if it keeps happening."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class AguiTranslator:
    """Stateful per-run translator (one instance per /api/chat request)."""

    def __init__(self, thread_id: str, run_id: str | None = None) -> None:
        self.thread_id = thread_id
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self._open_msg: str | None = None  # message id currently streaming
        self._streamed_partial = False
        self._streamed_text = ""  # partial deltas of the open message, to detect a screened final
        self._calls: dict[str, dict] = {}  # tool_call_id -> {name, args, t0}
        self._cited: set[str] = set()

    # ------------------------------------------------------------------ framing
    def _ev(self, type_: str, **fields: Any) -> dict:
        return {"type": type_, "timestamp": _now_ms(), **fields}

    def start(self) -> list[dict]:
        return [self._ev("RUN_STARTED", threadId=self.thread_id, runId=self.run_id)]

    def finish(self) -> list[dict]:
        return [*self._close_text(), self._ev("RUN_FINISHED", threadId=self.thread_id, runId=self.run_id)]

    def error(self, code: str = "AGENT_ERROR") -> list[dict]:
        return [*self._close_text(), self._ev("RUN_ERROR", message=RUN_ERROR_MESSAGE, code=code)]

    def guardrail_block(self, record: dict) -> list[dict]:
        verdicts = record.get("guardrail_verdicts") or [{}]
        categories = verdicts[0].get("categories") if isinstance(verdicts[0], dict) else None
        return [self._ev("CUSTOM", name="guardrail_block", value={
            "stage": record.get("tool_invoked"),
            "categories": categories or [],
            "wellbeing": bool(categories and "self_harm" in categories),
        })]

    # ------------------------------------------------------------------ text
    def _open_text(self) -> list[dict]:
        if self._open_msg:
            return []
        self._open_msg = f"msg-{uuid.uuid4().hex[:12]}"
        self._streamed_partial = False
        self._streamed_text = ""
        return [self._ev("TEXT_MESSAGE_START", messageId=self._open_msg, role="assistant")]

    def _close_text(self) -> list[dict]:
        if not self._open_msg:
            return []
        mid, self._open_msg = self._open_msg, None
        return [self._ev("TEXT_MESSAGE_END", messageId=mid)]

    def _text(self, text: str, partial: bool) -> list[dict]:
        out: list[dict] = []
        if partial:
            out += self._open_text()
            self._streamed_partial = True
            self._streamed_text += text
            out.append(self._ev("TEXT_MESSAGE_CONTENT", messageId=self._open_msg, delta=text))
            return out
        # Final (aggregated) event: if we already streamed its partials, just close — unless the
        # output guardrail changed the final text (Model Armor screens only the final response),
        # in which case the screened text supersedes what was streamed.
        if self._open_msg and self._streamed_partial:
            if text != self._streamed_text:
                out.append(self._ev("CUSTOM", name="message_replace",
                                    value={"messageId": self._open_msg, "text": text}))
            return out + self._close_text()
        out += self._open_text()
        out.append(self._ev("TEXT_MESSAGE_CONTENT", messageId=self._open_msg, delta=text))
        out += self._close_text()
        return out

    # ------------------------------------------------------------------ tools
    def _tool_start(self, call_id: str, name: str, args: dict, author: str) -> list[dict]:
        self._calls[call_id] = {"name": name, "args": args, "t0": time.monotonic()}
        return [self._ev("TOOL_CALL_START", toolCallId=call_id, toolCallName=name,
                         label=TOOL_LABELS.get(name, f"Running {name}"), agent=author,
                         system=tool_system(name, author), parentMessageId=self._open_msg)]

    def _tool_end(self, call_id: str, name: str, response: dict, author: str) -> list[dict]:
        call = self._calls.pop(call_id, None) or {"name": name, "args": {}, "t0": time.monotonic()}
        status = response.get("status") if isinstance(response, dict) else None
        if name == "search_policy" and isinstance(response, dict):
            status = "success" if response.get("sufficient") else "no_match"
        out = [self._ev("TOOL_CALL_END", toolCallId=call_id, toolCallName=name, agent=author,
                        system=tool_system(name, author), status=status or "done",
                        latencyMs=int((time.monotonic() - call["t0"]) * 1000))]
        if not isinstance(response, dict):
            return out
        if name in WIDGETS and status == "success":
            out += self._widget(call_id, name, response.get("data"), author)
        elif name == "search_policy":
            out += self._citations(response)
        elif name.startswith("propose_"):
            out += self._proposal(response, author)
        elif name == "commit_action":
            out += self._transaction(call["args"].get("proposal_id"), response)
        return out

    def _widget(self, call_id: str, name: str, data: Any, author: str) -> list[dict]:
        kind, build = WIDGETS[name]
        try:
            payload = build(data)
        except Exception:  # a malformed vendor payload must never break the chat stream
            payload = None
        if payload is None:
            return []
        return [self._ev("CUSTOM", name="widget", value={"id": call_id, "kind": kind, "agent": author,
                                                          "data": payload})]

    def _citations(self, response: dict) -> list[dict]:
        out = []
        for r in response.get("results") or []:
            anchor = r.get("anchor")
            if not anchor or anchor in self._cited:
                continue
            self._cited.add(anchor)
            out.append(self._ev("CUSTOM", name="citation", value={
                "anchor": anchor, "label": r.get("citation"), "section": r.get("section"),
                "semantic_topic": r.get("semantic_topic"), "score": r.get("relevance_score"),
            }))
        return out

    def _proposal(self, response: dict, author: str) -> list[dict]:
        status = response.get("status")
        if status == "awaiting_confirmation" and response.get("proposal_id"):
            pid = response["proposal_id"]
            action = response.get("action")
            value = _jsonable({
                "proposal_id": pid, "action": action, "title": ACTION_TITLES.get(action, action),
                "agent": author, "proposed": response.get("proposed") or {},
                "computed": response.get("computed") or {}, "warnings": response.get("warnings") or [],
                "status": "awaiting_confirmation",
            })
            return [self._ev("STATE_DELTA", delta=[{"op": "add", "path": f"/pending_proposals/{pid}",
                                                    "value": value}])]
        if status == "denied":
            return [self._ev("CUSTOM", name="transaction", value=_jsonable({
                "proposal_id": None, "stage": "propose", "status": "denied",
                "rule_ids": response.get("rule_ids") or [], "reasons": response.get("reasons") or [],
            }))]
        return []

    def _transaction(self, pid: str | None, response: dict) -> list[dict]:
        status = response.get("status")
        out = [self._ev("CUSTOM", name="transaction", value=_jsonable({
            "proposal_id": pid, "stage": "commit", "status": status, "action": response.get("action"),
            "backend_ref": response.get("backend_ref"), "rule_ids": response.get("rule_ids") or [],
            "reasons": response.get("reasons") or [], "message": response.get("message"),
        }))]
        awaiting_yes = status == "denied" and "B3_CONFIRMATION_REQUIRED" in (response.get("rule_ids") or [])
        if pid and not awaiting_yes:
            out.append(self._ev("STATE_DELTA", delta=[{"op": "remove", "path": f"/pending_proposals/{pid}"}]))
        return out

    # ------------------------------------------------------------------ main
    def translate(self, event: Any) -> list[dict]:
        out: list[dict] = []
        author = getattr(event, "author", "") or ""
        partial = bool(getattr(event, "partial", False))
        content = getattr(event, "content", None)
        for part in (getattr(content, "parts", None) or []):
            fc = getattr(part, "function_call", None)
            fr = getattr(part, "function_response", None)
            if fc is not None and not partial:
                out += self._close_text()
                out += self._tool_start(fc.id or f"call-{uuid.uuid4().hex[:8]}", fc.name,
                                        _jsonable(dict(fc.args or {})), author)
            elif fr is not None:
                out += self._tool_end(fr.id or "", fr.name, _jsonable(fr.response or {}), author)
            elif getattr(part, "text", None) and not getattr(part, "thought", False):
                if author in SPECIALISTS or author == "user":
                    continue  # specialist drafts are composed by the orchestrator; don't double-render
                out += self._text(part.text, partial)
        return out
