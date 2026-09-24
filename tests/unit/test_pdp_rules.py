"""Unit Tests for the Deterministic Policy Decision Point (PDP) (SDD §3.8, §5.3, §7.3).

Verifies 100% of the 9 deterministic business rules + B-3 confirmation gate + B-8 resolution gate + §5.4 fail-closed:
1. LEAVE_BALANCE_CAP (§1.2, §20)
2. LEAVE_CHRONOLOGY (§1.2)
3. LEAVE_NOTICE_15D (§1.2, §5.3 soft warning)
4. TICKET_LIFECYCLE (§5.5 sequential New -> In Progress -> Resolved -> Closed; blocks New -> Closed)
5. TICKET_PRIORITY_FLOOR (§5.5 programmatic downgrade to '4 - Low' for squeaky chair / minor issues)
6. TICKET_DEDUPE (FR-4.3 5-minute duplicate scan in Transaction Ledger)
7. EQUIP_ELIGIBILITY (§5.4 Remote/Hybrid status, $500 USD cap, Facilities category)
8. RELOCATION_CAP (§5.5 $10,000 USD cap, Facilities category, Priority '3 - Moderate')
9. CONTACT_FORMAT (FR-3.3 address >= 5 chars, phone regex)
"""

from datetime import date
import unittest

from app.ledger.transaction_ledger import TransactionLedger
from app.pdp.rules_engine import PolicyDecisionPoint


class TestPolicyDecisionPointRules(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = TransactionLedger()
        self.pdp = PolicyDecisionPoint(
            ledger=self.ledger,
            engine_available=True,
            reference_today=date(2026, 9, 24),
        )

    def test_leave_balance_cap_denies_when_exceeding_accrued(self) -> None:
        dec = self.pdp.evaluate(
            "submit_leave",
            {
                "start_date": "2026-10-20",
                "end_date": "2026-10-30",
                "leave_type": "Vacation",
                "days": 8.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"balances": {"vacation": {"remaining": 5.0}}},
        )
        self.assertFalse(dec.allowed)
        self.assertEqual(dec.status, "DENY")
        self.assertEqual(dec.rule_id, "LEAVE_BALANCE_CAP")
        self.assertIn("5.0", dec.user_message)

    def test_leave_chronology_denies_past_and_inverted_dates(self) -> None:
        # Past date
        past_dec = self.pdp.evaluate(
            "submit_leave",
            {
                "start_date": "2026-09-10",
                "end_date": "2026-09-12",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"balances": {"vacation": {"remaining": 10.0}}},
        )
        self.assertFalse(past_dec.allowed)
        self.assertEqual(past_dec.rule_id, "LEAVE_CHRONOLOGY")

        # start > end
        inv_dec = self.pdp.evaluate(
            "submit_leave",
            {
                "start_date": "2026-10-20",
                "end_date": "2026-10-18",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"balances": {"vacation": {"remaining": 10.0}}},
        )
        self.assertFalse(inv_dec.allowed)
        self.assertEqual(inv_dec.rule_id, "LEAVE_CHRONOLOGY")

    def test_leave_notice_15d_warns_rather_than_denying(self) -> None:
        # 2026-09-28 is only 4 days ahead of reference_today (2026-09-24) -> WARN (§5.3)
        dec = self.pdp.evaluate(
            "submit_leave",
            {
                "start_date": "2026-09-28",
                "end_date": "2026-09-29",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"balances": {"vacation": {"remaining": 5.0}}},
        )
        self.assertTrue(dec.allowed)
        self.assertEqual(dec.status, "ALLOW_WITH_WARNING")
        self.assertEqual(dec.rule_id, "LEAVE_NOTICE_15D")
        self.assertTrue(len(dec.warnings) == 1)
        self.assertIn("15 days", dec.warnings[0])

    def test_confirm_before_write_b3_gate(self) -> None:
        dec = self.pdp.evaluate(
            "submit_leave",
            {
                "start_date": "2026-10-20",
                "end_date": "2026-10-21",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": False,
            },
            employee_id="EMP-SG-001",
            context_data={"balances": {"vacation": {"remaining": 5.0}}},
        )
        self.assertFalse(dec.allowed)
        self.assertEqual(dec.status, "CONFIRMATION_REQUIRED")
        self.assertEqual(dec.rule_id, "B-3_CONFIRM_BEFORE_WRITE")
        self.assertIsNotNone(dec.confirmation_card)
        self.assertEqual(dec.confirmation_card["resulting_balance_after"], 3.0)

    def test_ticket_lifecycle_blocks_new_to_closed_shortcut_and_enforces_b8(self) -> None:
        # Handbook §5.5 prohibits skipping states (New -> Closed), even though backend allows it
        skip_dec = self.pdp.evaluate(
            "update_status",
            {
                "ticket_id": "INC123456",
                "new_status": "Closed",
                "user_asserted_resolution": True,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"current_status": "New"},
        )
        self.assertFalse(skip_dec.allowed)
        self.assertEqual(skip_dec.rule_id, "TICKET_LIFECYCLE")
        self.assertIn("In Progress", skip_dec.user_message)

        # Boundary B-8: Cannot transition In Progress -> Resolved unless user_asserted_resolution=True
        b8_dec = self.pdp.evaluate(
            "update_status",
            {
                "ticket_id": "INC123457",
                "new_status": "Resolved",
                "user_asserted_resolution": False,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"current_status": "In Progress"},
        )
        self.assertFalse(b8_dec.allowed)
        self.assertEqual(b8_dec.rule_id, "B-8_NO_AUTO_RESOLUTION")

        # Valid transition with user_asserted_resolution=True & confirmed=True
        ok_dec = self.pdp.evaluate(
            "update_status",
            {
                "ticket_id": "INC123457",
                "new_status": "Resolved",
                "user_asserted_resolution": True,
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
            context_data={"current_status": "In Progress"},
        )
        self.assertTrue(ok_dec.allowed)

    def test_ticket_priority_floor_downgrades_squeaky_chair_to_low(self) -> None:
        dec = self.pdp.evaluate(
            "create_incident",
            {
                "category": "Facilities",
                "short_description": "Critical: my office chair squeaks loudly",
                "priority": "1 - Critical",
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
        )
        self.assertTrue(dec.allowed)
        self.assertEqual(dec.status, "ALLOW_WITH_WARNING")
        self.assertEqual(dec.rule_id, "TICKET_PRIORITY_FLOOR")
        self.assertEqual(dec.modified_args["priority"], "4 - Low")

    def test_ticket_dedupe_blocks_within_5_minute_window(self) -> None:
        self.ledger.record_intent(
            idempotency_key="idem-test1",
            employee_id="EMP-SG-001",
            tool_name="create_incident",
            payload={"category": "IT", "short_description": "VPN connection keeps dropping"},
        )
        self.ledger.mark_committed("idem-test1", "INC0099111")

        dup_dec = self.pdp.evaluate(
            "create_incident",
            {
                "category": "IT",
                "short_description": "VPN connection keeps dropping",
                "priority": "3 - Moderate",
                "confirmed": True,
            },
            employee_id="EMP-SG-001",
        )
        self.assertFalse(dup_dec.allowed)
        self.assertEqual(dup_dec.rule_id, "TICKET_DEDUPE")
        self.assertEqual(dup_dec.existing_backend_ref, "INC0099111")

    def test_equip_eligibility_and_relocation_cap(self) -> None:
        # On-Site employee is rejected for Home Office Equipment Allowance (§5.4)
        onsite_dec = self.pdp.evaluate(
            "create_incident",
            {
                "category": "Facilities",
                "short_description": "Order home office monitor",
                "workflow_type": "equipment_procurement",
                "estimated_cost_usd": 350.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-002",
            context_data={
                "location_status": "On-Site",
                "address": "45 Tampines Ave 4, Singapore 529681",
            },
        )
        self.assertFalse(onsite_dec.allowed)
        self.assertEqual(onsite_dec.rule_id, "EQUIP_ELIGIBILITY")

        # Relocation exceeding $10,000 USD cap is denied (§5.5)
        reloc_dec = self.pdp.evaluate(
            "create_incident",
            {
                "category": "Facilities",
                "short_description": "London relocation allowance and building badge",
                "workflow_type": "relocation",
                "relocation_amount_usd": 12500.0,
                "confirmed": True,
            },
            employee_id="EMP-SG-003",
        )
        self.assertFalse(reloc_dec.allowed)
        self.assertEqual(reloc_dec.rule_id, "RELOCATION_CAP")

    def test_contact_format_and_pdp_fail_closed(self) -> None:
        bad_contact = self.pdp.evaluate(
            "update_contact",
            {"address": "123", "phone": "invalid-phone", "confirmed": True},
            employee_id="EMP-SG-001",
        )
        self.assertFalse(bad_contact.allowed)
        self.assertEqual(bad_contact.rule_id, "CONTACT_FORMAT")

        # Fail closed (§5.4)
        unavailable_pdp = PolicyDecisionPoint(engine_available=False)
        fail_closed_dec = unavailable_pdp.evaluate(
            "submit_leave",
            {"start_date": "2026-10-20", "end_date": "2026-10-21", "days": 2.0, "confirmed": True},
            employee_id="EMP-SG-001",
        )
        self.assertFalse(fail_closed_dec.allowed)
        self.assertEqual(fail_closed_dec.rule_id, "PDP_FAIL_CLOSED")
        self.assertEqual(fail_closed_dec.user_message, "I can't complete that action right now.")


if __name__ == "__main__":
    unittest.main()
