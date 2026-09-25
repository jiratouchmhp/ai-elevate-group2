"""Anti-Corruption Layer — the only path from agents to systems of record (SDD §5.1, D9).

Responsibilities that must NOT live in a prompt:
  * identity injection — `employee_id` comes from the verified context, never from
    model-supplied arguments (§4.4, T-3)
  * attribution headers (FR-1.2): X-Actor-Type, X-On-Behalf-Of, X-Agent-Id/Version,
    X-Correlation-Id
  * PDP enforcement on every mutating call (§3.4) — at propose AND again at commit
  * confirm-before-write (B-3) via HMAC-signed intent tokens that can only be
    committed in a *later* invocation (i.e. after another user turn)
  * idempotency + saga ledger (§3.5/§3.6); writes are never auto-retried (§5.2)
  * retry-with-backoff for reads only (NFR-4.2); non-technical error copy (NFR-4.1)
  * a structured audit record for every attempt, allowed or denied (§4.6)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from app import clock, config
from app.integration import audit, pdp
from app.integration.backend_client import get_backend
from app.integration.errors import BackendError
from app.integration.ledger import get_ledger

READ_TIMEOUT_S, WRITE_TIMEOUT_S = 5.0, 8.0
READ_RETRIES = 3

FRIENDLY = {
    "work_week": "WorkWeek is temporarily unavailable, so I can't check or update your HR record right now. "
                 "Please try again shortly.",
    "service_immediately": "I can't reach the service desk right now. You can raise this directly in the "
                           "ServiceImmediately portal, or try again shortly.",
}

# Mutating action -> vendor op
WRITE_OPS = {
    "submit_leave": "work_week.request_time_off",
    "cancel_leave": "work_week.cancel_leave_request",
    "update_contact": "work_week.update_personal_info",
    "create_incident": "service_immediately.create_ticket",
    "add_comment": "service_immediately.add_ticket_comment",
    "update_status": "service_immediately.update_ticket_status",
}


@dataclass
class Ctx:
    employee_id: str
    session_id: str
    invocation_id: str
    agent_id: str
    correlation_id: str

    def headers(self) -> dict:
        return {
            "X-Actor-Type": "AUTOMATED_AGENT",
            "X-On-Behalf-Of": self.employee_id,
            "X-Agent-Id": self.agent_id,
            "X-Agent-Version": config.AGENT_VERSION,
            "X-Correlation-Id": self.correlation_id,
        }


def _system(op: str) -> str:
    return op.split(".", 1)[0]


def _audit(ctx: Ctx, **kw) -> dict:
    return audit.emit(session_id=ctx.session_id, employee_id=ctx.employee_id, agent_id=ctx.agent_id,
                      correlation_id=ctx.correlation_id, **kw)


# ------------------------------------------------------------------------ reads
async def read(ctx: Ctx, tool: str, op: str, **kwargs: Any) -> dict:
    backend = get_backend()
    last_err: BackendError | None = None
    for attempt in range(READ_RETRIES):
        try:
            data = await asyncio.wait_for(backend.call(op, ctx.employee_id, ctx.headers(), **kwargs), READ_TIMEOUT_S)
            _audit(ctx, event="tool_call", tool_invoked=tool, tool_args=kwargs, outcome="SUCCESS")
            return {"status": "success", "data": data}
        except TimeoutError:
            last_err = BackendError(504, "timeout")
        except BackendError as e:
            last_err = e
            if e.status < 500:
                break  # client errors are not transient
        await asyncio.sleep(min(0.2 * (2 ** attempt), 1.0))
    assert last_err is not None
    _audit(ctx, event="tool_call", tool_invoked=tool, tool_args=kwargs, outcome=f"ERROR_{last_err.status}")
    if last_err.status in (403, 404):
        return {"status": "not_found", "message": "No matching record was found on your account."}
    return {"status": "unavailable", "message": FRIENDLY[_system(op)]}


# ------------------------------------------------------------------------ facts for the PDP
async def _facts(ctx: Ctx, action: str, args: dict) -> dict:
    b, h, emp = get_backend(), ctx.headers(), ctx.employee_id
    facts: dict[str, Any] = {"employee_id": emp, "today": clock.today(), "now": clock.now()}
    # Independent reads run concurrently; asyncio.gather re-raises the first BackendError,
    # so the fail-closed behaviour of propose/commit is unchanged.
    if action == "submit_leave":
        facts["balances"], facts["requests"] = await asyncio.gather(
            b.call("work_week.get_employee_balances", emp, h),  # FR-3.4: always fresh
            b.call("work_week.get_leave_requests", emp, h),  # LEAVE_OVERLAP
        )
    elif action == "cancel_leave":
        facts["requests"] = await b.call("work_week.get_leave_requests", emp, h)
    elif action == "create_incident":
        facts["recent_tickets"], facts["profile"] = await asyncio.gather(
            b.call("service_immediately.list_tickets", emp, h),
            b.call("work_week.get_profile", emp, h),
        )
    elif action in ("add_comment", "update_status"):
        try:
            facts["ticket"] = await b.call("service_immediately.get_ticket", emp, h, ticket_id=args.get("ticket_id"))
        except BackendError as e:
            if e.status >= 500:
                raise
            facts["ticket"] = None
    return facts


# ------------------------------------------------------------------------ intent tokens
def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")
    mac = hmac.new(config.CONFIRM_TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def _verify(token: str) -> dict | None:
    try:
        body, mac = token.rsplit(".", 1)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(config.CONFIRM_TOKEN_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None


def _apply_modifications(action: str, args: dict, decision: pdp.Decision) -> dict:
    final = dict(args)
    final.update(decision.modifications)
    if action == "submit_leave":
        final["days"] = decision.computed.get("days")
    if decision.computed.get("ship_to"):
        final["ship_to"] = decision.computed["ship_to"]  # verified address from WorkWeek (§5.4)
    return final


# ------------------------------------------------------------------------ propose
async def propose(ctx: Ctx, action: str, args: dict) -> dict:
    tool = f"propose_{action}"
    try:
        facts = await _facts(ctx, action, args)
    except BackendError:
        _audit(ctx, event="tool_call", tool_invoked=tool, tool_args=args, outcome="ERROR_UNAVAILABLE",
               pdp_decision="DENY", pdp_rule_ids=["BACKEND_UNAVAILABLE"])
        return {"status": "unavailable", "message": FRIENDLY[_system(WRITE_OPS[action])]}

    decision = pdp.evaluate(action, args, facts)
    if not decision.allowed:
        _audit(ctx, event="pdp_decision", tool_invoked=tool, tool_args=args, pdp_decision="DENY",
               pdp_rule_ids=decision.rule_ids, outcome="DENIED")
        return {"status": "denied", "rule_ids": decision.rule_ids, "reasons": decision.reasons,
                "note": "Nothing was submitted. Explain the constraint to the user and propose an alternative."}

    final_args = _apply_modifications(action, args, decision)
    idem = hashlib.sha256(
        _canonical([ctx.employee_id, ctx.session_id, action, final_args]).encode()
    ).hexdigest()[:24]
    get_ledger().record_intent(ctx.session_id, ctx.employee_id, action, final_args, idem)
    token = _sign({
        "a": action, "args": final_args, "emp": ctx.employee_id, "sid": ctx.session_id,
        "inv": ctx.invocation_id, "iat": int(time.time()),
        "exp": int(time.time()) + config.CONFIRM_TOKEN_TTL_SECONDS, "key": idem,
    })
    _audit(ctx, event="pdp_decision", tool_invoked=tool, tool_args=final_args, pdp_decision="ALLOW",
           pdp_rule_ids=decision.rule_ids, outcome="PROPOSED")
    return {
        "status": "awaiting_confirmation",
        "action": action,
        "proposed": final_args,
        "warnings": decision.warnings,
        "computed": {k: v for k, v in decision.computed.items() if k != "ship_to"}
        | ({"ship_to": decision.computed["ship_to"]} if "ship_to" in decision.computed else {}),
        "intent_token": token,
        "instruction": "Show the user this exact summary (and any warnings) and ask them to confirm. "
                       "Only call commit_action with this intent_token AFTER the user explicitly says yes "
                       "in their next message.",
    }


# ------------------------------------------------------------------------ commit
def _deny_commit(ctx: Ctx, rule: str, reason: str, args: dict | None = None) -> dict:
    _audit(ctx, event="commit_blocked", tool_invoked="commit_action", tool_args=args or {},
           pdp_decision="DENY", pdp_rule_ids=[rule], outcome="DENIED")
    return {"status": "denied", "rule_ids": [rule], "reasons": [reason]}


async def commit(ctx: Ctx, intent_token: str) -> dict:
    payload = _verify(intent_token)
    if payload is None:
        return _deny_commit(ctx, "CONFIRM_TOKEN_INVALID", "The confirmation token is invalid or was altered.")
    action, args = payload["a"], payload["args"]
    if payload["exp"] < time.time():
        return _deny_commit(ctx, "CONFIRM_TOKEN_EXPIRED", "The proposal expired; please propose it again.", args)
    if payload["emp"] != ctx.employee_id:
        return _deny_commit(ctx, "OWNERSHIP", "This proposal belongs to a different user.", args)
    if payload["inv"] == ctx.invocation_id:
        return _deny_commit(
            ctx, "B3_CONFIRMATION_REQUIRED",
            "The user has not confirmed yet. Present the proposal and wait for the user's explicit 'yes'.", args)

    ledger = get_ledger()
    existing = ledger.intent_status(payload["key"])
    if existing and existing["status"] == "COMMITTED":
        _audit(ctx, event="tool_call", tool_invoked="commit_action", tool_args=args, pdp_decision="ALLOW",
               pdp_rule_ids=["IDEMPOTENT_REPLAY"], outcome="DUPLICATE_SUPPRESSED", backend_ref=existing["backend_ref"])
        return {"status": "already_committed", "backend_ref": existing["backend_ref"],
                "message": "This action was already completed; no duplicate was created."}

    op = WRITE_OPS[action]
    try:
        facts = await _facts(ctx, action, args)
    except BackendError:  # cannot re-validate -> never write blind
        return await _record_failure(ctx, action, args, payload["key"], BackendError(503, "unavailable"))
    decision = pdp.evaluate(action, args, facts)  # re-validate with fresh data
    if not decision.allowed:
        _audit(ctx, event="pdp_decision", tool_invoked="commit_action", tool_args=args, pdp_decision="DENY",
               pdp_rule_ids=decision.rule_ids, outcome="DENIED")
        return {"status": "denied", "rule_ids": decision.rule_ids, "reasons": decision.reasons}

    vendor_args = _vendor_args(action, args)
    try:
        data = await asyncio.wait_for(
            get_backend().call(op, ctx.employee_id, ctx.headers(), **vendor_args), WRITE_TIMEOUT_S)
    except (BackendError, TimeoutError) as e:  # NO retry for writes (§5.2)
        err = e if isinstance(e, BackendError) else BackendError(504, "timeout")
        return await _record_failure(ctx, action, args, payload["key"], err)

    ref = _backend_ref(action, data, args)
    ledger.mark_intent(payload["key"], "COMMITTED", ref)
    prior = ledger.saga_for_session(ctx.session_id, ctx.employee_id)
    compensation = (
        action == "cancel_leave" and prior["state"] == "PARTIALLY_COMPLETE"
        and any(s.get("backend_ref") == args.get("request_id") for s in prior["steps"])
    )
    saga = ledger.append_step(ctx.session_id, ctx.employee_id, {
        "action": action, "status": "COMMITTED", "backend_ref": ref, "compensation": compensation})
    _audit(ctx, event="tool_call", tool_invoked=f"commit_action:{action}", tool_args=args,
           pdp_decision="ALLOW", pdp_rule_ids=decision.rule_ids, outcome="COMMITTED", backend_ref=ref,
           extra={"saga_id": saga["saga_id"], "saga_state": saga["state"]})
    return {"status": "committed", "action": action, "backend_ref": ref, "result": data,
            "saga_ref": saga["saga_id"] if saga["state"] in ("PARTIALLY_COMPLETE", "COMPENSATED") else None,
            "saga_state": saga["state"]}


def _vendor_args(action: str, args: dict) -> dict:
    if action == "submit_leave":
        return {k: args[k] for k in ("start_date", "end_date", "leave_type", "days")}
    if action == "cancel_leave":
        return {"request_id": args["request_id"]}
    if action == "update_contact":
        return {k: args.get(k) for k in ("address", "phone") if args.get(k) is not None}
    if action == "create_incident":
        desc = args.get("description") or ""
        if args.get("ship_to"):
            desc = f"{desc}\nShip to verified WorkWeek address: {args['ship_to']}".strip()
        return {"category": args["category"], "short_description": args["short_description"],
                "priority": args.get("priority") or "4 - Low", "description": desc}
    if action == "add_comment":
        return {"ticket_id": args["ticket_id"], "comment": args["comment"]}
    if action == "update_status":
        return {"ticket_id": args["ticket_id"], "new_status": args["new_status"],
                "resolution_notes": args.get("resolution_notes") or ""}
    raise KeyError(action)


def _backend_ref(action: str, data: Any, args: dict) -> str | None:
    if isinstance(data, dict):
        return data.get("request_id") or data.get("ticket_id") or args.get("ticket_id")
    return None


async def _record_failure(ctx: Ctx, action: str, args: dict, key: str, err: BackendError) -> dict:
    ledger = get_ledger()
    ledger.mark_intent(key, "FAILED" if err.status != 504 else "UNKNOWN_RECONCILE")
    saga = ledger.append_step(ctx.session_id, ctx.employee_id, {
        "action": action, "status": "FAILED", "error": err.status})
    committed = [s for s in saga["steps"] if s["status"] == "COMMITTED" and not s.get("compensation")]
    task_id = None
    if saga["state"] == "PARTIALLY_COMPLETE":
        summary = (f"Partial completion for {ctx.employee_id}: committed "
                   f"{[s['action'] + ':' + str(s['backend_ref']) for s in committed]}; failed {action}.")
        task_id = ledger.open_reconciliation(saga["saga_id"], ctx.employee_id, summary)
    _audit(ctx, event="tool_call", tool_invoked=f"commit_action:{action}", tool_args=args, pdp_decision="ALLOW",
           outcome=f"BACKEND_ERROR_{err.status}", extra={"saga_id": saga["saga_id"], "saga_state": saga["state"],
                                                         "hr_ops_task": task_id})
    result: dict[str, Any] = {
        "status": "failed",
        "action": action,
        "message": FRIENDLY[_system(WRITE_OPS[action])],
        "retried": False,
        "note": "Writes are never retried automatically (to avoid duplicates).",
    }
    if task_id:
        result |= {
            "saga_ref": saga["saga_id"],
            "saga_state": "PARTIALLY_COMPLETE",
            "hr_ops_task": task_id,
            "completed_steps": [{"action": s["action"], "backend_ref": s["backend_ref"]} for s in committed],
            "compensation_offer": [
                {"action": "cancel_leave", "request_id": s["backend_ref"]}
                for s in committed if s["action"] == "submit_leave"
            ],
            "instruction": "Tell the user exactly what succeeded and what failed, give the saga and HR Ops "
                           "references, and OFFER (do not perform) the compensation. Never undo automatically.",
        }
    return result
