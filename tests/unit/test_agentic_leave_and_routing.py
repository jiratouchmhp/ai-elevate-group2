"""Unit tests for Agent-Driven Intent Routing & Dynamic Date/Duration Resolution."""

from __future__ import annotations

import unittest

from app.acl.mcp_proxy import AntiCorruptionLayerProxy, MockVendorBackendState
from app.agent import HRMultiAgentRuntime


class TestAgenticLeaveAndRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MockVendorBackendState()
        self.acl = AntiCorruptionLayerProxy(
            backend_state=self.backend,
            use_live_mcp=False,
        )
        self.runtime = HRMultiAgentRuntime(acl_proxy=self.acl)

    def test_book_2_days_from_10_01_resolves_dates_and_asks_confirmation(self) -> None:
        """'book 2 days from 10/01' must resolve 2026-10-01 to 2026-10-02 (2.0 days) and ask B-3 confirmation."""
        res = self.runtime.run_turn(
            "book 2 days from 10/01",
            authenticated_employee_id="EMP-SG-003",
            session_id="sess-agentic-1",
        )
        self.assertIn("workweek_agent", res.delegated_agents)
        self.assertNotIn("policy_agent", res.delegated_agents)
        self.assertIn("submit_leave", res.tool_trajectory)
        self.assertIsNotNone(res.confirmation_card)
        self.assertEqual(res.confirmation_card["start_date"], "2026-10-01")
        self.assertEqual(res.confirmation_card["end_date"], "2026-10-02")
        self.assertEqual(res.confirmation_card["days"], 2.0)
        self.assertEqual(res.confirmation_card["resulting_balance_after"], 13.0)  # 15.0 - 2.0

        # Confirm on next turn with "yes" -> must commit exact 2026-10-01 to 2026-10-02 dates
        res_confirm = self.runtime.run_turn(
            "yes",
            authenticated_employee_id="EMP-SG-003",
            session_id="sess-agentic-1",
        )
        self.assertIsNone(res_confirm.confirmation_card)
        self.assertIn("2026-10-01", res_confirm.response_text)
        self.assertIn("2026-10-02", res_confirm.response_text)
        self.assertIn("2.0 days", res_confirm.response_text)

    def test_book_leave_date_range_computes_days_automatically(self) -> None:
        """'book leave between 2026-11-02 to 2026-11-05' must compute 4.0 days instead of defaulting to 2.0."""
        res = self.runtime.run_turn(
            "book leave between 2026-11-02 to 2026-11-05",
            authenticated_employee_id="EMP-SG-003",
            session_id="sess-agentic-2",
        )
        self.assertIn("workweek_agent", res.delegated_agents)
        self.assertNotIn("policy_agent", res.delegated_agents)
        self.assertIsNotNone(res.confirmation_card)
        self.assertEqual(res.confirmation_card["start_date"], "2026-11-02")
        self.assertEqual(res.confirmation_card["end_date"], "2026-11-05")
        self.assertEqual(res.confirmation_card["days"], 4.0)
        self.assertEqual(res.confirmation_card["resulting_balance_after"], 11.0)  # 15.0 - 4.0

    def test_book_leave_without_dates_proposes_window_and_allows_date_override(self) -> None:
        """'I want to book 2 days of leave' proposes an eligible window with B-3 confirmation and allows overriding with 'from 10/01'."""
        res1 = self.runtime.run_turn(
            "I want to book 2 days of leave",
            authenticated_employee_id="EMP-SG-003",
            session_id="sess-agentic-3",
        )
        self.assertIn("workweek_agent", res1.delegated_agents)
        self.assertIn("get_leave_balance", res1.tool_trajectory)
        self.assertIn("submit_leave", res1.tool_trajectory)
        self.assertIsNotNone(res1.confirmation_card)

        # Follow up with "from 10/01" -> merges with the 2-day draft -> 2026-10-01 to 2026-10-02
        res2 = self.runtime.run_turn(
            "from 10/01",
            authenticated_employee_id="EMP-SG-003",
            session_id="sess-agentic-3",
        )
        self.assertIsNotNone(res2.confirmation_card)
        self.assertEqual(res2.confirmation_card["start_date"], "2026-10-01")
        self.assertEqual(res2.confirmation_card["end_date"], "2026-10-02")
        self.assertEqual(res2.confirmation_card["days"], 2.0)

    def test_natural_ticket_creation_without_hardcoded_keywords(self) -> None:
        """Natural ticket prompt routes to service_immediately_agent.create_incident without 'squeak' or 'vpn'."""
        res = self.runtime.run_turn(
            "My Wi-Fi keeps disconnecting in the office, please open a ticket",
            authenticated_employee_id="EMP-SG-001",
            session_id="sess-agentic-4",
        )
        self.assertIn("service_immediately_agent", res.delegated_agents)
        self.assertIn("create_incident", res.tool_trajectory)
        self.assertIsNotNone(res.confirmation_card)


if __name__ == "__main__":
    unittest.main()
