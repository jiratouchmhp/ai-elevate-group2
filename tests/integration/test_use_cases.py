"""Integration Tests for End-to-End Use Cases UC-1.1..UC-1.3 and UC-2.1..UC-2.3 (SDD §3, §9).

Verifies:
- UC-1.1 Policy Q&A with clickable citation anchors and grounded refusal on unanswerable queries
- UC-1.2 WorkWeek Leave balance check + leave submission with B-3 confirmation card
- UC-1.3 ServiceImmediately status check + priority downgrade + lifecycle validation
- UC-2.1 Cross-system Equipment Procurement (Policy -> WorkWeek -> ServiceImmediately)
  including read-only eligibility rejection for On-Site employees (EMP-SG-002)
- UC-2.2 Cross-system Medical Leave including §3.6 Partial Completion Saga (`SAGA-xxxx`),
  HR Ops Queue notification, non-auto-reversal, and user-confirmed undo (`cancel_leave` -> `COMPENSATED`)
- UC-2.3 Cross-system International Relocation ($10,000 USD cap, C-1 semantic citation,
  WorkWeek address update, and ServiceImmediately Facilities badge ticket at Priority '3 - Moderate')
"""

import unittest

from app.acl.mcp_proxy import AntiCorruptionLayerProxy, MockVendorBackendState
from app.agent import HRMultiAgentRuntime, root_agent
from app.governance.audit_logger import AuditLogger
from app.ledger.transaction_ledger import SagaState, TransactionLedger
from app.pdp.rules_engine import PolicyDecisionPoint


class TestEndToEndUseCases(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockVendorBackendState()
        self.ledger = TransactionLedger()
        self.audit = AuditLogger()
        self.pdp = PolicyDecisionPoint(ledger=self.ledger)
        self.acl = AntiCorruptionLayerProxy(
            pdp=self.pdp,
            ledger=self.ledger,
            audit_logger=self.audit,
            backend_state=self.backend,
        )
        self.runtime = HRMultiAgentRuntime(
            acl_proxy=self.acl,
            ledger=self.ledger,
            audit_logger=self.audit,
        )

    def test_agent_roster_and_negative_authority_architecture(self) -> None:
        # Root orchestrator holds zero tools (delegation only, §3.1)
        self.assertEqual(len(root_agent.tools), 0)
        self.assertEqual(len(root_agent.sub_agents), 3)

    def test_uc_1_1_policy_qa_and_grounded_refusal(self) -> None:
        res = self.runtime.run_turn("What is the bereavement leave policy?")
        self.assertFalse(res.refusal)
        self.assertGreater(len(res.citations), 0)
        self.assertIn("policy_agent", res.delegated_agents)
        self.assertIn("search_policy", res.tool_trajectory)

        # Unanswerable question must trigger first-class refusal (FR-5.4)
        ref_res = self.runtime.run_turn("What is the monthly parking subsidy?")
        self.assertTrue(ref_res.refusal)
        self.assertIn("REFUSE", ref_res.response_text)

    def test_uc_1_2_workweek_self_service_with_confirmation_gate(self) -> None:
        # Turn 1: Unconfirmed write returns STATE_DELTA confirmation card (B-3)
        t1 = self.runtime.run_turn(
            "Please submit a time-off request for 2 days.",
            authenticated_employee_id="EMP-SG-001",
            confirmed=False,
        )
        self.assertIsNotNone(t1.confirmation_card)
        self.assertEqual(t1.confirmation_card["action"], "submit_leave")

        # Turn 2: Confirmed write commits to WorkWeek and updates remaining balance from 5.0 -> 3.0
        t2 = self.runtime.run_turn(
            "Please submit a time-off request for 2 days.",
            authenticated_employee_id="EMP-SG-001",
            confirmed=True,
        )
        self.assertIn("LR-", t2.response_text)
        self.assertEqual(self.backend.balances["EMP-SG-001"]["vacation"]["remaining"], 3.0)

    def test_uc_1_3_service_immediately_priority_downgrade_and_lifecycle(self) -> None:
        t_create = self.runtime.run_turn(
            "Create an IT ticket with Critical priority because my chair squeaks.",
            authenticated_employee_id="EMP-SG-001",
            confirmed=True,
        )
        self.assertIn("4 - Low", t_create.response_text)

        # Attempting to jump New -> Closed on INC123456 is blocked by PDP TICKET_LIFECYCLE
        t_close = self.runtime.run_turn(
            "Close ticket INC123456 immediately.",
            authenticated_employee_id="EMP-SG-001",
            confirmed=True,
            user_asserted_resolution=True,
        )
        self.assertIn("progress sequentially", t_close.response_text)

    def test_uc_2_1_equipment_procurement_eligible_vs_ineligible(self) -> None:
        # Eligible Hybrid employee (EMP-SG-001)
        ok_res = self.runtime.run_turn(
            "I'm eligible for a home office monitor — verify my remote status and order one for me.",
            authenticated_employee_id="EMP-SG-001",
            confirmed=True,
        )
        self.assertEqual(
            ok_res.delegated_agents,
            ["policy_agent", "workweek_agent", "service_immediately_agent"],
        )
        self.assertEqual(
            ok_res.tool_trajectory,
            ["search_policy", "get_profile", "create_incident"],
        )
        self.assertIn("INC00", ok_res.response_text)

        # Ineligible On-Site employee (EMP-SG-002): stops after read-only verification with ZERO writes!
        inelig_res = self.runtime.run_turn(
            "I'm eligible for a home office monitor — verify my remote status and order one for me.",
            authenticated_employee_id="EMP-SG-002",
            confirmed=True,
        )
        self.assertEqual(inelig_res.tool_trajectory, ["search_policy", "get_profile"])
        self.assertIn("not eligible", inelig_res.response_text)

    def test_uc_2_2_medical_leave_partial_completion_and_user_confirmed_compensation(self) -> None:
        # Simulate ServiceImmediately outage after WorkWeek succeeds (§3.6)
        self.backend.service_immediately_available = False

        partial_res = self.runtime.run_turn(
            "I need to take short-term medical leave starting next Monday. What is the process, and can you set it up for me?",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-uc22",
            confirmed=True,
        )
        self.assertEqual(partial_res.saga_state, SagaState.PARTIALLY_COMPLETE.value)
        self.assertIsNotNone(partial_res.saga_id)
        # Verify WorkWeek leave was submitted (not auto-reversed without confirmation per B-3)
        self.assertEqual(self.backend.balances["EMP-SG-001"]["sick"]["remaining"], 7.0)
        self.assertEqual(len(self.ledger.hr_ops_queue), 1)

        # User explicitly asks to undo ("Then cancel the leave too.") with confirmation
        undo_res = self.runtime.run_turn(
            "Then cancel the leave too.",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-uc22",
            confirmed=True,
        )
        self.assertEqual(undo_res.saga_state, SagaState.COMPENSATED.value)
        # Verify 5.0 sick days were refunded back to 12.0!
        self.assertEqual(self.backend.balances["EMP-SG-001"]["sick"]["remaining"], 12.0)

    def test_uc_2_3_relocation_orchestration(self) -> None:
        reloc_res = self.runtime.run_turn(
            "I'm transferring to the London office next month. Can you tell me the relocation allowance, update my record, and get my building access sorted?",
            authenticated_employee_id="EMP-SG-003",
            confirmed=True,
        )
        self.assertEqual(
            reloc_res.delegated_agents,
            ["policy_agent", "workweek_agent", "service_immediately_agent"],
        )
        self.assertEqual(
            reloc_res.tool_trajectory,
            ["search_policy", "update_contact", "create_incident"],
        )
        self.assertIn("$10,000 USD", reloc_res.response_text)
        self.assertIn("INC00", reloc_res.response_text)
        # Verify C-1 semantic topic citation is attached
        self.assertEqual(
            reloc_res.citations[0]["semantic_topic"],
            "International Relocation & Building Access Policy",
        )


    def test_multi_turn_b3_confirmation_state_machine_yes_and_no(self) -> None:
        # Turn 1: Unconfirmed request stores pending_confirmation in session state
        t1 = self.runtime.run_turn(
            "Please submit a time-off request for 2 days.",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-b3-yes",
            confirmed=False,
        )
        self.assertIsNotNone(t1.confirmation_card)
        state_yes = self.runtime.get_session_state("sess-b3-yes", "EMP-SG-001")
        self.assertIsNotNone(state_yes["pending_confirmation"])

        # Turn 2: User replies "Yes" -> consumes pending_confirmation and executes submit_leave
        t2 = self.runtime.run_turn(
            "Yes",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-b3-yes",
        )
        self.assertIn("submit_leave", t2.tool_trajectory)
        self.assertIn("LR-", t2.response_text)
        self.assertIsNone(state_yes["pending_confirmation"])

        # Turn 1b: Unconfirmed request followed by "No" cancels without calling write tool
        self.runtime.run_turn(
            "Please submit a time-off request for 1 day.",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-b3-no",
            confirmed=False,
        )
        t_no = self.runtime.run_turn(
            "No",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-b3-no",
        )
        self.assertEqual(t_no.tool_trajectory, [])
        self.assertIn("Cancelled pending", t_no.response_text)

    def test_single_domain_routing_for_all_12_authorized_tools(self) -> None:
        r_prof = self.runtime.run_turn("Show my WorkWeek profile.", session_id="s-prof")
        self.assertIn("get_profile", r_prof.tool_trajectory)

        r_pi = self.runtime.run_turn("What is my personal contact info?", session_id="s-pi")
        self.assertIn("get_personal_info", r_pi.tool_trajectory)

        r_lr = self.runtime.run_turn("List my leave requests.", session_id="s-lr")
        self.assertIn("get_leave_requests", r_lr.tool_trajectory)

        r_uc = self.runtime.run_turn(
            "Update my address to 88 Marina Blvd, Singapore 018981.",
            session_id="s-uc",
            confirmed=True,
        )
        self.assertIn("update_contact", r_uc.tool_trajectory)

        r_lt = self.runtime.run_turn("List my open IT tickets.", session_id="s-lt")
        self.assertIn("list_tickets", r_lt.tool_trajectory)

        r_ac = self.runtime.run_turn(
            "Please add a note and comment on ticket INC123456 that I rebooted.",
            session_id="s-ac",
            confirmed=True,
        )
        self.assertIn("add_comment", r_ac.tool_trajectory)

        r_ip = self.runtime.run_turn(
            "Move ticket INC123456 to In Progress.",
            session_id="s-ip",
            confirmed=True,
        )
        self.assertIn("update_status", r_ip.tool_trajectory)
        self.assertEqual(self.backend.tickets["INC123456"]["status"], "In Progress")

    def test_gold_04_informational_equipment_query_does_not_call_create_incident(self) -> None:
        res = self.runtime.run_turn(
            "Am I eligible for a home office monitor, and what is the allowance cap?",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-gold04",
        )
        self.assertEqual(res.tool_trajectory, ["search_policy", "get_profile"])
        self.assertNotIn("create_incident", res.tool_trajectory)
        self.assertIsNone(res.confirmation_card)
        self.assertIn("500", res.response_text)

    def test_ambiguity_clarification_and_explicit_date_extraction(self) -> None:
        amb = self.runtime.run_turn("How much leave do I get?", session_id="sess-amb")
        self.assertIn("clarify", amb.response_text.lower())
        self.assertIn("20 days/year", amb.response_text)
        self.assertIn("14 days", amb.response_text)
        self.assertNotIn("<<<UNTRUSTED_POLICY_DOCUMENT_START", amb.response_text)

        past_leave = self.runtime.run_turn(
            "Please submit a time-off request from 2020-01-01 to 2020-01-05 for 5 days.",
            session_id="sess-past",
            confirmed=True,
        )
        self.assertIn("past dates", past_leave.response_text.lower())


if __name__ == "__main__":
    unittest.main()
