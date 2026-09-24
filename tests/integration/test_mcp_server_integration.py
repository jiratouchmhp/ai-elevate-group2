"""Live MCP Server Integration Tests (`https://mock-saas.aishprabhat.demo.altostrat.com`).

Validates direct `StreamableHttpMcpClient` and `AntiCorruptionLayerProxy(use_live_mcp=True)`
integration against the live WorkWeek (`/work-week/mcp/`) and ServiceImmediately
(`/service-immediately/mcp/`) MCP endpoints using the environment `MCP_TOKEN` (`EMP-836`).

Covers:
1. Live MCP tool & resource discovery (`tools/list`, `resources/templates/list`)
2. Token-bound employee identity resolution (`EMP-836` — Vannick Employee)
3. Live WorkWeek HCM reads (`get_profile`, `get_personal_info`, `get_leave_balance`, `get_leave_requests`)
4. Live ServiceImmediately ITSM reads (`list_tickets`, `get_ticket` for `INC0004806`)
5. Reversible live write + compensation round-trips through ACL + PDP:
   - `submit_leave` (`request_time_off`) -> verify balance decremented -> `cancel_leave` (`cancel_leave_request`) -> verify balance restored to 15.0
   - `update_contact` (`update_personal_info`) -> verify updated -> restore original address & phone
6. Security & Governance enforcement (`FR-1.5` cross-user access denial, `FR-1.1` forbidden tool blocks)
"""

from __future__ import annotations

import unittest

from app.acl.mcp_client import StreamableHttpMcpClient
from app.acl.mcp_proxy import AntiCorruptionLayerProxy
from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    get_mcp_token,
)
from app.governance.audit_logger import AuditLogger
from app.ledger.transaction_ledger import TransactionLedger
from app.pdp.rules_engine import PolicyDecisionPoint


class TestLiveMcpServerIntegration(unittest.TestCase):
    """Integration tests executing against the live WorkWeek & ServiceImmediately MCP servers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp_token = get_mcp_token()
        if not cls.mcp_token:
            raise unittest.SkipTest("MCP_TOKEN environment variable is required for live MCP integration tests.")
        cls.employee_id = get_mcp_authenticated_employee_id()
        cls.client = StreamableHttpMcpClient(mcp_token=cls.mcp_token)

    def setUp(self) -> None:
        self.audit = AuditLogger()
        self.ledger = TransactionLedger()
        self.pdp = PolicyDecisionPoint(ledger=self.ledger)
        self.acl = AntiCorruptionLayerProxy(
            pdp=self.pdp,
            ledger=self.ledger,
            audit_logger=self.audit,
            use_live_mcp=True,
            mcp_client=self.client,
            mcp_token=self.mcp_token,
        )

    def test_01_live_mcp_discovery_tools_and_resource_templates(self) -> None:
        """Verify `tools/list` and `resources/templates/list` on both `/work-week/mcp/` and `/service-immediately/mcp/`."""
        ww_tools = {t["name"] for t in self.client.list_tools("WorkWeek")}
        self.assertTrue(
            {
                "get_employee_balances",
                "request_time_off",
                "update_personal_info",
                "get_personal_info",
                "get_leave_requests",
                "cancel_leave_request",
                "get_current_employee_id",
            }.issubset(ww_tools)
        )

        ww_templates = {
            t["uriTemplate"] for t in self.client.list_resource_templates("WorkWeek")
        }
        self.assertIn("workweek://employees/{employee_id}/profile", ww_templates)
        self.assertIn("workweek://employees/{employee_id}/timeoff", ww_templates)

        si_tools = {t["name"] for t in self.client.list_tools("ServiceImmediately")}
        self.assertTrue(
            {
                "list_tickets",
                "create_ticket",
                "add_ticket_comment",
                "update_ticket_status",
            }.issubset(si_tools)
        )

        si_templates = {
            t["uriTemplate"]
            for t in self.client.list_resource_templates("ServiceImmediately")
        }
        self.assertIn("serviceimmediately://tickets/{ticket_id}", si_templates)

    def test_02_live_workweek_reads_via_acl_proxy(self) -> None:
        """Verify live `get_profile`, `get_personal_info`, `get_leave_balance`, and `get_leave_requests` for EMP-836."""
        resolved_emp = self.client.resolve_token_employee_id()
        self.assertEqual(resolved_emp, self.employee_id)

        prof_res = self.acl.invoke_tool(
            "get_profile",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(prof_res["status"], "SUCCESS")
        self.assertEqual(prof_res["mcp_backend"], "live_mcp_server")
        self.assertEqual(prof_res["profile"]["employee_id"], "EMP-836")
        self.assertIn("Vannick", prof_res["profile"]["name"])

        info_res = self.acl.invoke_tool(
            "get_personal_info",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(info_res["status"], "SUCCESS")
        self.assertEqual(info_res["mcp_backend"], "live_mcp_server")
        self.assertTrue(len(info_res["profile"]["address"]) >= 5)

        bal_res = self.acl.invoke_tool(
            "get_leave_balance",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(bal_res["status"], "SUCCESS")
        self.assertEqual(bal_res["mcp_backend"], "live_mcp_server")
        self.assertIn("vacation", bal_res["balances"])
        self.assertIn("sick", bal_res["balances"])
        self.assertGreaterEqual(bal_res["balances"]["vacation"]["accrued"], 20.0)

        reqs_res = self.acl.invoke_tool(
            "get_leave_requests",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(reqs_res["status"], "SUCCESS")
        self.assertEqual(reqs_res["mcp_backend"], "live_mcp_server")
        self.assertGreaterEqual(len(reqs_res["leave_requests"]), 1)

    def test_03_live_service_immediately_reads_via_acl_proxy(self) -> None:
        """Verify live `list_tickets` and `get_ticket` (INC0004806) on `/service-immediately/mcp/`."""
        list_res = self.acl.invoke_tool(
            "list_tickets",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="service_immediately_agent",
        )
        self.assertEqual(list_res["status"], "SUCCESS")
        self.assertEqual(list_res["mcp_backend"], "live_mcp_server")
        ticket_ids = [t["ticket_id"] for t in list_res["tickets"]]
        self.assertIn("INC0004806", ticket_ids)

        ticket_res = self.acl.invoke_tool(
            "get_ticket",
            {"ticket_id": "INC0004806"},
            authenticated_employee_id=self.employee_id,
            calling_agent="service_immediately_agent",
        )
        self.assertEqual(ticket_res["status"], "SUCCESS")
        self.assertEqual(ticket_res["mcp_backend"], "live_mcp_server")
        self.assertEqual(ticket_res["ticket"]["ticket_id"], "INC0004806")
        self.assertEqual(ticket_res["ticket"]["requestor_id"], "EMP-836")

    def test_04_live_submit_and_cancel_leave_roundtrip_via_acl_and_pdp(self) -> None:
        """Execute a live `submit_leave` followed by `cancel_leave` and verify exact balance restoration."""
        initial_bal = self.acl.invoke_tool(
            "get_leave_balance",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )["balances"]["vacation"]["remaining"]

        # 1. Unconfirmed call must be intercepted by PDP (B-3 confirmation gate) BEFORE hitting live MCP
        unconfirmed = self.acl.invoke_tool(
            "submit_leave",
            {
                "start_date": "2026-11-16",
                "end_date": "2026-11-17",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": False,
            },
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(unconfirmed["status"], "CONFIRMATION_REQUIRED")

        # 2. Confirmed call executes `request_time_off` on live `/work-week/mcp/`
        submitted = self.acl.invoke_tool(
            "submit_leave",
            {
                "start_date": "2026-11-16",
                "end_date": "2026-11-17",
                "leave_type": "Vacation",
                "days": 2.0,
                "confirmed": True,
            },
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(submitted["status"], "SUCCESS")
        self.assertEqual(submitted["mcp_backend"], "live_mcp_server")
        created_req_id = submitted["request_id"]
        self.assertEqual(submitted["remaining_balance"], round(initial_bal - 2.0, 1))

        # 3. Cancel the newly created leave request via ACL (`cancel_leave` -> `cancel_leave_request`)
        cancelled = self.acl.invoke_tool(
            "cancel_leave",
            {"request_id": created_req_id, "confirmed": True},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(cancelled["status"], "SUCCESS")
        self.assertEqual(cancelled["mcp_backend"], "live_mcp_server")
        self.assertEqual(cancelled["remaining_balance"], initial_bal)

    def test_05_live_update_contact_and_restore_roundtrip(self) -> None:
        """Execute a live `update_contact` and immediately restore original profile values."""
        orig_prof = self.acl.invoke_tool(
            "get_profile",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )["profile"]
        orig_address = orig_prof["address"]
        orig_phone = orig_prof["phone"]

        try:
            upd = self.acl.invoke_tool(
                "update_contact",
                {
                    "address": "Singapore Office, 80 Pasir Panjang Rd, #10-01, Singapore",
                    "phone": orig_phone,
                    "confirmed": True,
                },
                authenticated_employee_id=self.employee_id,
                calling_agent="workweek_agent",
            )
            self.assertEqual(upd["status"], "SUCCESS")
            self.assertEqual(upd["mcp_backend"], "live_mcp_server")
            self.assertIn("#10-01", upd["updated_profile"]["address"])
        finally:
            # Always restore original address & phone
            restore_ledger = TransactionLedger()
            restore_acl = AntiCorruptionLayerProxy(
                pdp=PolicyDecisionPoint(ledger=restore_ledger),
                ledger=restore_ledger,
                audit_logger=self.audit,
                use_live_mcp=True,
                mcp_client=self.client,
                mcp_token=self.mcp_token,
            )
            restored = restore_acl.invoke_tool(
                "update_contact",
                {
                    "address": orig_address,
                    "phone": orig_phone,
                    "confirmed": True,
                },
                authenticated_employee_id=self.employee_id,
                calling_agent="workweek_agent",
            )
            self.assertEqual(restored["status"], "SUCCESS")
            self.assertEqual(restored["updated_profile"]["address"], orig_address)

    def test_06_security_guardrails_cross_user_isolation_and_denied_tools(self) -> None:
        """Verify FR-1.5 cross-user isolation and FR-1.1 explicitly denied tools when using live MCP."""
        cross_res = self.acl.invoke_tool(
            "get_leave_balance",
            {"employee_id": "EMP-SG-002"},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(cross_res["status"], "DENIED")
        self.assertEqual(cross_res["error_code"], "CROSS_USER_ACCESS_DENIED")

        denied_res = self.acl.invoke_tool(
            "get_current_employee_id",
            {},
            authenticated_employee_id=self.employee_id,
            calling_agent="workweek_agent",
        )
        self.assertEqual(denied_res["status"], "DENIED")
        self.assertEqual(denied_res["error_code"], "TOOL_EXPLICITLY_DENIED")


if __name__ == "__main__":
    unittest.main()
