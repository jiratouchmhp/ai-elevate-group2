"""PDP rule tests — 100% branch coverage gate (SDD §7.3).

Every rule is exercised on both its allow and deny/modify side, plus the fail-closed
dispatcher. Facts are passed explicitly, so these tests need no backend or model.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.integration import pdp

TODAY = date(2026, 10, 5)  # Monday
NOW = datetime(2026, 10, 5, 10, 0, 0)
BAL = {"Vacation": {"remaining": 5}, "Sick": {"remaining": 12}}


# ---------------------------------------------------------------- helpers / plumbing
def test_helpers_and_versions():
    assert pdp.business_days(date(2026, 10, 5), date(2026, 10, 11)) == 5  # Mon..Sun
    assert pdp.similarity("", "anything") == 0.0
    assert pdp.similarity("monitor request", "monitor request") == 1.0
    assert pdp.rules_version() == "1.1.0"
    assert pdp.load_rules(str(pdp.RULES_PATH))["rules_version"] == "1.1.0"
    d = pdp.Decision(pdp.ALLOW)
    d.warn("X", "w")
    assert d.to_dict()["rule_ids"] == ["X:WARN"] and d.allowed


@pytest.mark.parametrize("value", [None, 20261005, "2026/10/05", "2026-02-30"])
def test_parse_date_rejects(value):
    assert pdp._parse_date(value) is None


# ---------------------------------------------------------------- submit_leave
def _leave(**kw):
    args = {"start_date": "2026-11-02", "end_date": "2026-11-03", "leave_type": "Vacation"} | kw
    return pdp.check_submit_leave(args, BAL, TODAY)


@pytest.mark.parametrize("kw,rule", [
    ({"start_date": "bad"}, "LEAVE_FORMAT"),
    ({"end_date": None}, "LEAVE_FORMAT"),
    ({"leave_type": "Unpaid"}, "LEAVE_TYPE"),
    ({"start_date": "2026-11-05", "end_date": "2026-11-03"}, "LEAVE_CHRONOLOGY"),
    ({"start_date": "2026-10-01", "end_date": "2026-10-02"}, "LEAVE_CHRONOLOGY"),
    ({"half_day": True}, "LEAVE_INCREMENT"),
    ({"start_date": "2026-11-07", "end_date": "2026-11-08"}, "LEAVE_INCREMENT"),  # weekend only
    ({"start_date": "2026-11-02", "end_date": "2026-11-13"}, "LEAVE_BALANCE_CAP"),  # 10 > 5
])
def test_submit_leave_denials(kw, rule):
    d = _leave(**kw)
    assert not d.allowed and d.rule_ids == [rule]


def test_submit_leave_allowed_with_notice_warning():
    d = _leave(start_date="2026-10-12", end_date="2026-10-13")  # 7 days notice
    assert d.allowed and d.computed["days"] == 2 and d.computed["remaining_after"] == 3
    assert d.rule_ids == ["LEAVE_NOTICE_15D:WARN"]


def test_submit_leave_allowed_no_warning_and_half_day():
    assert _leave().rule_ids == []  # 28 days notice
    d = _leave(start_date="2026-11-02", end_date="2026-11-02", half_day=True)
    assert d.allowed and d.computed["days"] == 0.5


def test_submit_leave_sick_short_notice_no_warning_and_missing_balance():
    d = pdp.check_submit_leave({"start_date": "2026-10-06", "end_date": "2026-10-06", "leave_type": "Sick"},
                               BAL, TODAY)
    assert d.allowed and d.warnings == []
    d = pdp.check_submit_leave({"start_date": "2026-11-02", "end_date": "2026-11-02", "leave_type": "Sick"},
                               {}, TODAY)
    assert d.rule_ids == ["LEAVE_BALANCE_CAP"]


def test_submit_leave_overlap_rule():
    existing = [
        {"request_id": "LR-1001", "start_date": "2026-12-21", "end_date": "2026-12-23", "status": "Approved"},
        {"request_id": "LR-1002", "start_date": "2026-11-02", "end_date": "2026-11-03", "status": "Cancelled"},
        {"request_id": "LR-1003", "start_date": None, "end_date": "2026-11-20", "status": "Pending"},
    ]
    dup = pdp.check_submit_leave({"start_date": "2026-12-22", "end_date": "2026-12-22", "leave_type": "Vacation"},
                                 BAL, TODAY, existing)
    assert not dup.allowed and dup.rule_ids == ["LEAVE_OVERLAP"] and "LR-1001" in dup.reasons[0]
    # cancelled request, malformed request and non-overlapping dates do not block
    ok = pdp.check_submit_leave({"start_date": "2026-11-02", "end_date": "2026-11-03", "leave_type": "Vacation"},
                                BAL, TODAY, existing)
    assert ok.allowed
    assert pdp.evaluate("submit_leave", {"start_date": "2026-12-21", "end_date": "2026-12-21",
                                         "leave_type": "Vacation"},
                        {"balances": BAL, "today": TODAY, "requests": existing}).rule_ids == ["LEAVE_OVERLAP"]

# ---------------------------------------------------------------- cancel_leave
REQS = [
    {"request_id": "LR-1", "employee_id": "EMP001", "status": "Approved", "start_date": "2026-12-21", "days": 3},
    {"request_id": "LR-2", "employee_id": "EMP001", "status": "Pending", "start_date": "2026-10-08", "days": 1},
    {"request_id": "LR-3", "employee_id": "EMP001", "status": "Cancelled", "start_date": "2026-12-01"},
    {"request_id": "LR-4", "employee_id": "EMP004", "status": "Approved", "start_date": "2026-12-01"},
    {"request_id": "LR-5", "employee_id": "EMP001", "status": "Approved", "start_date": None, "days": 1},
]


@pytest.mark.parametrize("rid,rule", [("LR-404", "OWNERSHIP"), ("LR-4", "OWNERSHIP"), ("LR-3", "LEAVE_CANCEL_STATE")])
def test_cancel_denials(rid, rule):
    d = pdp.check_cancel_leave({"request_id": rid}, REQS, "EMP001", TODAY)
    assert d.rule_ids == [rule]


def test_cancel_allowed_variants():
    far = pdp.check_cancel_leave({"request_id": "LR-1"}, REQS, "EMP001", TODAY)
    assert far.allowed and far.warnings == [] and far.computed["refund_days"] == 3
    near = pdp.check_cancel_leave({"request_id": "LR-2"}, REQS, "EMP001", TODAY)
    assert near.allowed and near.rule_ids == ["LEAVE_NOTICE_15D:WARN"]
    undated = pdp.check_cancel_leave({"request_id": "LR-5"}, REQS, "EMP001", TODAY)
    assert undated.allowed and undated.warnings == []


# ---------------------------------------------------------------- update_contact
@pytest.mark.parametrize("args,ok", [
    ({}, False),
    ({"address": "abc"}, False),
    ({"phone": "12ab"}, False),
    ({"address": "10 Anson Road, Singapore 079903"}, True),
    ({"phone": "+65 9123 4567"}, True),
    ({"address": "10 Anson Road", "phone": "+65 9123 4567"}, True),
])
def test_update_contact(args, ok):
    assert pdp.check_update_contact(args).allowed is ok


# ---------------------------------------------------------------- create_incident
HYBRID = {"location_status": "Hybrid", "address": "1 Test St"}
ONSITE = {"location_status": "On-site"}


def _inc(recent=None, profile=HYBRID, **kw):
    args = {"category": "Hardware", "short_description": "Laptop fan noisy", "priority": "3 - Moderate"} | kw
    return pdp.check_create_incident(args, recent or [], profile, NOW)


@pytest.mark.parametrize("kw,rule", [
    ({"short_description": "  "}, "TICKET_FORMAT"),
    ({"category": "Snacks"}, "TICKET_FORMAT"),
    ({"priority": "0 - Urgent"}, "TICKET_FORMAT"),
    ({"purpose": "equipment", "estimated_cost_usd": 800, "category": "Facilities"}, "EQUIP_ELIGIBILITY"),
    ({"purpose": "relocation", "estimated_cost_usd": 12000}, "RELOCATION_CAP"),
])
def test_incident_denials(kw, rule):
    assert _inc(**kw).rule_ids == [rule]


def test_equipment_ineligible_onsite():
    d = _inc(profile=ONSITE, purpose="equipment", category="Facilities")
    assert d.rule_ids == ["EQUIP_ELIGIBILITY"]


def test_equipment_modifies_category_and_ships_to_verified_address():
    d = _inc(purpose="equipment", short_description="Monitor for home office", estimated_cost_usd=300)
    assert d.allowed and d.modifications["category"] == "Facilities"
    assert d.computed["ship_to"] == "1 Test St" and d.computed["final_category"] == "Facilities"
    d2 = _inc(purpose="equipment", category="Facilities", short_description="Monitor")
    assert d2.modifications == {}


def test_relocation_and_email_delegation_normalisation():
    d = _inc(purpose="relocation", short_description="Badge for new office", priority="4 - Low",
             estimated_cost_usd=5000)
    assert d.modifications == {"category": "Facilities", "priority": "3 - Moderate"}
    d = _inc(purpose="relocation", category="Facilities", short_description="Badge for new office")
    assert d.modifications == {}
    d = _inc(purpose="email_delegation", short_description="Delegate email during medical leave")
    assert d.modifications == {"category": "HRSD"}  # priority already 3 - Moderate
    d = _inc(purpose="email_delegation", category="HRSD", priority="4 - Low", short_description="Delegate email")
    assert d.modifications == {"priority": "3 - Moderate"}


@pytest.mark.parametrize("kw,expected", [
    ({"short_description": "Squeaky chair", "priority": "2 - High", "category": "Facilities"}, "4 - Low"),
    ({"short_description": "Squeaky chair", "priority": "4 - Low", "category": "Facilities"}, None),
    ({"short_description": "Printer jam", "priority": "1 - Critical"}, "3 - Moderate"),
    ({"short_description": "VPN broken, cannot work", "priority": "1 - Critical", "category": "Network"}, "2 - High"),
    ({"short_description": "Ransomware on shared drive", "priority": "1 - Critical"}, None),
    ({"short_description": "Printer jam", "priority": "2 - High"}, "3 - Moderate"),
    ({"short_description": "Laptop not booting", "priority": "2 - High"}, None),
    ({"short_description": "Laptop fan noisy", "priority": None}, None),  # default 4 - Low
])
def test_priority_floor(kw, expected):
    d = _inc(**kw)
    assert d.allowed
    assert d.modifications.get("priority") == expected


def test_dedupe_window():
    recent_dup = [{"ticket_id": "INC1", "category": "Hardware", "short_description": "Laptop fan noisy",
                   "created_at": (NOW - timedelta(minutes=2)).isoformat()}]
    d = _inc(recent=recent_dup)
    assert d.rule_ids == ["TICKET_DEDUPE"] and "INC1" in d.reasons[0]
    others = [
        {"ticket_id": "INC2", "category": "Hardware", "short_description": "Laptop fan noisy",
         "created_at": NOW - timedelta(minutes=30)},                       # outside window (datetime)
        {"ticket_id": "INC3", "category": "Software", "short_description": "Laptop fan noisy",
         "created_at": NOW - timedelta(minutes=1)},                        # other category
        {"ticket_id": "INC4", "category": "Hardware", "short_description": "Keyboard missing keys",
         "created_at": NOW - timedelta(minutes=1)},                        # dissimilar
        {"ticket_id": "INC5", "category": "Hardware", "short_description": "Laptop fan noisy",
         "created_at": None},                                              # no timestamp
    ]
    assert _inc(recent=others).allowed


# ---------------------------------------------------------------- add_comment / update_status
TICKET = {"ticket_id": "INC9", "requestor_id": "EMP001", "status": "New"}


@pytest.mark.parametrize("ticket,comment,rule", [
    (None, "hi", "OWNERSHIP"),
    (TICKET | {"requestor_id": "EMP004"}, "hi", "OWNERSHIP"),
    (TICKET, "  ", "TICKET_FORMAT"),
    (TICKET | {"status": "Closed"}, "hi", "TICKET_LIFECYCLE"),
])
def test_add_comment_denials(ticket, comment, rule):
    assert pdp.check_add_comment({"comment": comment}, ticket, "EMP001").rule_ids == [rule]


def test_add_comment_ok():
    assert pdp.check_add_comment({"comment": "Any update?"}, TICKET, "EMP001").allowed


@pytest.mark.parametrize("current,target,asserted,rule", [
    ("New", "In Progress", False, None),
    ("In Progress", "Resolved", True, None),
    ("Resolved", "Closed", False, None),
    ("In Progress", "Resolved", False, "B8_NO_AUTO_RESOLVE"),
    ("New", "Closed", False, "TICKET_LIFECYCLE"),         # skip
    ("Closed", "New", False, "TICKET_LIFECYCLE"),         # terminal
    ("Weird", "In Progress", False, "TICKET_LIFECYCLE"),  # unknown current
    ("New", "Done", False, "TICKET_LIFECYCLE"),           # unknown target
])
def test_update_status(current, target, asserted, rule):
    d = pdp.check_update_status({"new_status": target}, TICKET | {"status": current}, "EMP001", asserted)
    assert (d.rule_ids == [rule]) if rule else d.allowed


def test_update_status_ownership():
    assert pdp.check_update_status({"new_status": "In Progress"}, None, "EMP001", False).rule_ids == ["OWNERSHIP"]


# ---------------------------------------------------------------- dispatcher
def test_evaluate_dispatch_unknown_and_fail_closed():
    facts = {"balances": BAL, "today": TODAY, "requests": REQS, "employee_id": "EMP001",
             "recent_tickets": [], "profile": HYBRID, "now": NOW, "ticket": TICKET}
    assert pdp.evaluate("submit_leave", {"start_date": "2026-11-02", "end_date": "2026-11-02",
                                         "leave_type": "Vacation"}, facts).allowed
    assert pdp.evaluate("cancel_leave", {"request_id": "LR-1"}, facts).allowed
    assert pdp.evaluate("update_contact", {"phone": "+65 9123 4567"}, facts).allowed
    assert pdp.evaluate("create_incident", {"category": "Other", "short_description": "Question"}, facts).allowed
    assert pdp.evaluate("add_comment", {"comment": "x"}, facts).allowed
    assert pdp.evaluate("update_status", {"new_status": "In Progress"}, facts).allowed
    assert pdp.evaluate("approve_leave", {}, facts).rule_ids == ["UNKNOWN_ACTION"]
    assert pdp.evaluate("submit_leave", {}, {}).rule_ids == ["PDP_ERROR"]
