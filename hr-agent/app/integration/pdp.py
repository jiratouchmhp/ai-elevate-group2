"""Policy Decision Point — deterministic pre-write validation (SDD §3.4, §3.8, §5.3).

The model decides *what the user wants*; this module decides *whether it is
allowed*. Every function is pure (facts are passed in by the ACL), so the rule set
is unit-testable to 100% branch coverage (SDD §7.3) and independent of any prompt.

`evaluate()` fails CLOSED: any exception becomes a DENY (SDD §5.4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

RULES_PATH = Path(__file__).with_name("rules.yaml")

ALLOW, DENY = "ALLOW", "DENY"


@dataclass
class Decision:
    outcome: str
    rule_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    modifications: dict[str, Any] = field(default_factory=dict)
    computed: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    def deny(self, rule_id: str, reason: str) -> Decision:
        self.outcome = DENY
        self.rule_ids.append(rule_id)
        self.reasons.append(reason)
        return self

    def warn(self, rule_id: str, message: str) -> None:
        self.rule_ids.append(f"{rule_id}:WARN")
        self.warnings.append(message)

    def modify(self, rule_id: str, key: str, value: Any, message: str) -> None:
        self.rule_ids.append(f"{rule_id}:MODIFY")
        self.modifications[key] = value
        self.warnings.append(message)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "rule_ids": self.rule_ids,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "modifications": self.modifications,
            "computed": self.computed,
        }


@lru_cache(maxsize=1)
def load_rules(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or RULES_PATH).read_text())


def rules_version() -> str:
    return str(load_rules()["rules_version"])


# --------------------------------------------------------------------------- helpers
def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def business_days(start: date, end: date) -> int:
    days, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def _has_any(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k in low for k in keywords)


# --------------------------------------------------------------------------- WorkWeek rules
INACTIVE_LEAVE_STATUSES = {"Cancelled", "Canceled", "Rejected", "Denied", "Withdrawn"}


def check_submit_leave(args: dict, balances: dict, today: date, existing: list | None = None) -> Decision:
    r = load_rules()["leave"]
    d = Decision(ALLOW)
    start, end = _parse_date(args.get("start_date")), _parse_date(args.get("end_date"))
    if start is None or end is None:
        return d.deny("LEAVE_FORMAT", "Dates must be real calendar dates in YYYY-MM-DD format.")
    leave_type = args.get("leave_type")
    if leave_type not in r["types"]:
        return d.deny("LEAVE_TYPE", f"Leave type must be one of {r['types']}.")
    if start > end:
        return d.deny("LEAVE_CHRONOLOGY", "The start date cannot be after the end date.")
    if start < today:
        return d.deny("LEAVE_CHRONOLOGY", "Leave cannot start in the past.")
    # LEAVE_OVERLAP — added from eval RCA (redteam rt-rb-004): a request duplicating an existing
    # active request passed every other rule because it fitted within the balance.
    for req in existing or []:
        if req.get("status") in INACTIVE_LEAVE_STATUSES:
            continue
        r_start, r_end = _parse_date(req.get("start_date")), _parse_date(req.get("end_date"))
        if r_start and r_end and start <= r_end and r_start <= end:
            return d.deny("LEAVE_OVERLAP", f"These dates overlap your existing request {req.get('request_id')} "
                                           f"({req.get('start_date')} to {req.get('end_date')}, {req.get('status')}).")

    if args.get("half_day"):
        if start != end:
            return d.deny("LEAVE_INCREMENT", "A half-day request must start and end on the same day.")
        days = 0.5
    else:
        days = float(business_days(start, end))
    if days <= 0:
        return d.deny("LEAVE_INCREMENT", "The requested range contains no working days.")
    d.computed["days"] = days

    remaining = float((balances.get(leave_type) or {}).get("remaining", 0))
    d.computed["remaining_before"] = remaining
    d.computed["remaining_after"] = remaining - days
    if days > remaining:
        return d.deny(
            "LEAVE_BALANCE_CAP",
            f"{days:g} {leave_type.lower()} day(s) requested but only {remaining:g} remaining.",
        )

    notice = (start - today).days
    if leave_type == "Vacation" and notice < r["notice_days_warn"]:
        d.warn(
            "LEAVE_NOTICE_15D",
            f"Only {notice} day(s) notice. Handbook §1.2/§20.4 requires manager approval at least "
            f"{r['notice_days_warn']} days in advance — please make sure your manager has agreed.",
        )
    return d


def check_cancel_leave(args: dict, requests: list[dict], employee_id: str, today: date) -> Decision:
    d = Decision(ALLOW)
    req = next((x for x in requests if x.get("request_id") == args.get("request_id")), None)
    if req is None or req.get("employee_id") != employee_id:
        return d.deny("OWNERSHIP", "No leave request with that reference exists on your record.")
    if req.get("status") not in ("Pending", "Approved"):
        return d.deny("LEAVE_CANCEL_STATE", f"Request is already {req.get('status')}.")
    start = _parse_date(req.get("start_date"))
    if start is not None and (start - today).days < load_rules()["leave"]["notice_days_warn"]:
        d.warn(
            "LEAVE_NOTICE_15D",
            "Handbook §1.2 asks for changes/cancellations at least 15 days before the leave starts.",
        )
    d.computed["refund_days"] = req.get("days")
    return d


def check_update_contact(args: dict) -> Decision:
    r = load_rules()["contact"]
    d = Decision(ALLOW)
    address, phone = args.get("address"), args.get("phone")
    if not address and not phone:
        return d.deny("CONTACT_FORMAT", "Provide a new home address and/or personal phone number.")
    if address is not None and len(address.strip()) < r["address_min_length"]:
        return d.deny("CONTACT_FORMAT", f"Address must be at least {r['address_min_length']} characters.")
    if phone is not None and not re.fullmatch(r["phone_regex"], phone):
        return d.deny(
            "CONTACT_FORMAT",
            "Phone number must be 7–20 characters of digits, spaces, dashes or parentheses, "
            "optionally starting with '+' (e.g. +65 9123 4567).",
        )
    return d


# --------------------------------------------------------------------------- ITSM rules
def check_create_incident(
    args: dict, recent_tickets: list[dict], profile: dict, now: datetime
) -> Decision:
    r = load_rules()
    t = r["ticket"]
    d = Decision(ALLOW)
    short = (args.get("short_description") or "").strip()
    if not short:
        return d.deny("TICKET_FORMAT", "A short description is required.")
    category = args.get("category")
    priority = args.get("priority") or "4 - Low"
    if category not in t["categories"]:
        return d.deny("TICKET_FORMAT", f"Category must be one of {t['categories']}.")
    if priority not in t["priorities"]:
        return d.deny("TICKET_FORMAT", f"Priority must be one of {t['priorities']}.")

    purpose = args.get("purpose") or "general"
    text = f"{short} {args.get('description') or ''}"

    if purpose == "equipment":
        e = r["equipment"]
        status = profile.get("location_status")
        if status not in e["eligible_location_status"]:
            return d.deny(
                "EQUIP_ELIGIBILITY",
                f"Home office equipment allowance requires 'Remote' or 'Hybrid' status; "
                f"your WorkWeek status is '{status}'.",
            )
        cost = args.get("estimated_cost_usd")
        if cost is not None and float(cost) > e["cap_usd"]:
            return d.deny("EQUIP_ELIGIBILITY", f"Estimated cost exceeds the ${e['cap_usd']} allowance.")
        if category != e["required_category"]:
            d.modify("EQUIP_ELIGIBILITY", "category", e["required_category"],
                     "Remote equipment orders must use the 'Facilities' category (§5.4).")
            category = e["required_category"]
        d.computed["ship_to"] = profile.get("address")
    elif purpose == "relocation":
        rl = r["relocation"]
        amount = args.get("estimated_cost_usd")
        if amount is not None and float(amount) > rl["cap_usd"]:
            return d.deny("RELOCATION_CAP", f"Relocation allowance is capped at ${rl['cap_usd']:,}.")
        if category != rl["required_category"]:
            d.modify("RELOCATION_CAP", "category", rl["required_category"],
                     "Badge pre-configuration tickets use the 'Facilities' category.")
            category = rl["required_category"]
        if priority != rl["required_priority"]:
            d.modify("RELOCATION_CAP", "priority", rl["required_priority"],
                     "Badge pre-configuration tickets are Priority '3 - Moderate'.")
            priority = rl["required_priority"]
    elif purpose == "email_delegation":
        ed = r["email_delegation"]
        if category != ed["required_category"]:
            d.modify("EMAIL_DELEGATION", "category", ed["required_category"],
                     "Email delegation requests use the 'HRSD' category (§2.2).")
            category = ed["required_category"]
        if priority != ed["required_priority"]:
            d.modify("EMAIL_DELEGATION", "priority", ed["required_priority"],
                     "Email delegation requests are Priority '3 - Moderate' (§2.2).")
            priority = ed["required_priority"]

    # TICKET_PRIORITY_FLOOR — downgrade with explanation (§5.5).
    if _has_any(text, t["low_priority_keywords"]) and priority != "4 - Low":
        d.modify("TICKET_PRIORITY_FLOOR", "priority", "4 - Low",
                 "Minor facility issues must be classified '4 - Low' (§5.5); priority downgraded.")
    elif priority == "1 - Critical" and not _has_any(text, t["critical_criteria"]):
        new = "2 - High" if _has_any(text, t["high_criteria"]) else "3 - Moderate"
        d.modify("TICKET_PRIORITY_FLOOR", "priority", new,
                 f"The description does not meet Critical criteria; priority set to '{new}' (§5.5).")
    elif priority == "2 - High" and not _has_any(text, t["high_criteria"] + t["critical_criteria"]):
        d.modify("TICKET_PRIORITY_FLOOR", "priority", "3 - Moderate",
                 "The description does not meet High criteria; priority set to '3 - Moderate' (§5.5).")

    # TICKET_DEDUPE — surface the existing ticket instead of creating a new one.
    window = timedelta(minutes=t["dedupe_window_minutes"])
    for tk in recent_tickets:
        created = tk.get("created_at")
        created_dt = datetime.fromisoformat(created) if isinstance(created, str) else created
        if (
            created_dt is not None
            and now - created_dt <= window
            and tk.get("category") == category
            and similarity(tk.get("short_description", ""), short) >= t["dedupe_similarity"]
        ):
            return d.deny(
                "TICKET_DEDUPE",
                f"A similar ticket {tk.get('ticket_id')} was raised {int((now - created_dt).total_seconds() // 60)} "
                f"minute(s) ago; I won't create a duplicate.",
            )
    d.computed["final_category"] = d.modifications.get("category", category)
    d.computed["final_priority"] = d.modifications.get("priority", priority)
    return d


def check_add_comment(args: dict, ticket: dict | None, employee_id: str) -> Decision:
    d = Decision(ALLOW)
    if not ticket or ticket.get("requestor_id") != employee_id:
        return d.deny("OWNERSHIP", "You can only comment on tickets you raised.")
    if not (args.get("comment") or "").strip():
        return d.deny("TICKET_FORMAT", "Comment text is required.")
    if ticket.get("status") == "Closed":
        return d.deny("TICKET_LIFECYCLE", "The ticket is Closed; open a new ticket instead.")
    return d


def check_update_status(
    args: dict, ticket: dict | None, employee_id: str, user_asserted_resolution: bool
) -> Decision:
    lifecycle = load_rules()["ticket"]["lifecycle"]
    d = Decision(ALLOW)
    if not ticket or ticket.get("requestor_id") != employee_id:
        return d.deny("OWNERSHIP", "You can only update tickets you raised.")
    target, current = args.get("new_status"), ticket.get("status")
    if target not in lifecycle:
        return d.deny("TICKET_LIFECYCLE", f"Status must be one of {lifecycle}.")
    idx = lifecycle.index(current) if current in lifecycle else -1
    if idx < 0 or lifecycle.index(target) != idx + 1:
        nxt = lifecycle[idx + 1] if 0 <= idx < len(lifecycle) - 1 else None
        return d.deny(
            "TICKET_LIFECYCLE",
            f"Tickets must move New → In Progress → Resolved → Closed without skipping states (§5.5). "
            f"'{current}' can only move to '{nxt}'." if nxt else f"'{current}' is a terminal state.",
        )
    if target == "Resolved" and not user_asserted_resolution:
        return d.deny("B8_NO_AUTO_RESOLVE", "A ticket is only marked Resolved when you confirm the issue is fixed.")
    return d


# --------------------------------------------------------------------------- dispatcher
_CHECKS = {
    "submit_leave": lambda a, f: check_submit_leave(a, f["balances"], f["today"], f.get("requests")),
    "cancel_leave": lambda a, f: check_cancel_leave(a, f["requests"], f["employee_id"], f["today"]),
    "update_contact": lambda a, f: check_update_contact(a),
    "create_incident": lambda a, f: check_create_incident(a, f["recent_tickets"], f["profile"], f["now"]),
    "add_comment": lambda a, f: check_add_comment(a, f.get("ticket"), f["employee_id"]),
    "update_status": lambda a, f: check_update_status(
        a, f.get("ticket"), f["employee_id"], bool(a.get("user_confirmed_resolved"))
    ),
}


def evaluate(action: str, args: dict, facts: dict) -> Decision:
    """Evaluate a mutating action. Unknown actions and internal errors fail closed."""
    check = _CHECKS.get(action)
    if check is None:
        return Decision(DENY, ["UNKNOWN_ACTION"], [f"Action '{action}' is not in the capability manifest."])
    try:
        return check(args, facts)
    except Exception as exc:  # fail closed (SDD §5.4)
        return Decision(DENY, ["PDP_ERROR"], [f"Rule evaluation failed: {type(exc).__name__}"])
