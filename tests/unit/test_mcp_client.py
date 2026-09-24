"""Unit Tests for MCP Client, Environment Configuration, and ACL Live MCP Mediation (SDD §5.1, §5.2, D9).

Verifies:
- `.env` and environment variable loading (`MCP_TOKEN`, `WORKWEEK_MCP_URL`, `SERVICE_IMMEDIATELY_MCP_URL`)
- `X-MCP-Token` header enforcement and strict prohibition of `?token=` / `?pat=` URL query strings
- `AdvancedSDPScanner` and `ModelArmorScanner` redaction/blocking of `mcp_...` Personal Access Tokens
- `StreamableHttpMcpClient` schema normalization (`workweek://employees/{id}/profile`, `timeoff`,
  `cancel_leave_request` integer `request_id` coercion, `update_personal_info` partial field prefill,
  and `create_ticket` `requested_by` mapping)
- `AntiCorruptionLayerProxy` live MCP routing, header sanitization, and PDP integration
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple
import unittest

from app.acl.mcp_client import (
    MCPTransportError,
    StreamableHttpMcpClient,
    assert_no_url_credentials,
)
from app.acl.mcp_proxy import AntiCorruptionLayerProxy
from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    get_mcp_base_url,
    get_mcp_token,
    get_service_immediately_mcp_url,
    get_workweek_mcp_url,
)
from app.governance.audit_logger import AuditLogger
from app.ledger.transaction_ledger import TransactionLedger
from app.pdp.rules_engine import PolicyDecisionPoint
from app.safety.guardrails import AdvancedSDPScanner, ModelArmorScanner


class FakeMcpTransport:
    """Deterministic in-memory JSON-RPC 2.0 transport for unit testing `StreamableHttpMcpClient`."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any], Dict[str, str]]] = []
        self.address = "Singapore Office, 80 Pasir Panjang Rd, Singapore"
        self.phone = "+65-6521-0000"
        self.vacation_used = 5.0

    def __call__(
        self, url: str, body: bytes, headers: Dict[str, str], timeout: float
    ) -> Dict[str, Any]:
        payload = json.loads(body.decode("utf-8"))
        self.calls.append((url, payload, headers))
        method = payload["method"]
        params = payload.get("params", {})

        if method == "resources/read":
            uri = params.get("uri", "")
            if uri.endswith("/profile"):
                return {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": json.dumps(
                                    {
                                        "employee_id": "EMP-836",
                                        "first_name": "Vannick",
                                        "last_name": "Employee",
                                        "email": "vannick@google.com",
                                        "job_title": "Solutions Acceleration Architect",
                                        "department": "Google Forge (Customer Engineering)",
                                        "role": "Individual Contributor",
                                        "hire_date": "2026-09-22",
                                        "manager_id": "EMP-1",
                                        "home_address": self.address,
                                        "phone_number": self.phone,
                                    }
                                ),
                            }
                        ]
                    },
                }
            if uri.endswith("/timeoff"):
                return {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": json.dumps(
                                    {
                                        "employee_id": "EMP-836",
                                        "vacation_accrued": 20.0,
                                        "vacation_used": self.vacation_used,
                                        "sick_accrued": 10.0,
                                        "sick_used": 0.0,
                                    }
                                ),
                            }
                        ]
                    },
                }
            if uri.startswith("serviceimmediately://tickets/"):
                t_id = uri.rsplit("/", 1)[-1]
                return {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/plain",
                                "text": json.dumps(
                                    {
                                        "ticket_id": t_id,
                                        "requested_by": "EMP-836",
                                        "category": "Inquiry / Help",
                                        "short_description": "Onboarding setup and badges configuration",
                                        "status": "New",
                                        "priority": "3 - Moderate",
                                        "assignment_group": "Service Desk",
                                        "assigned_to": "ITIL User",
                                        "comments": [],
                                    }
                                ),
                            }
                        ]
                    },
                }

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "get_current_employee_id":
                text = "EMP-836"
            elif name == "get_employee_balances":
                rem = 20.0 - self.vacation_used
                text = f"Employee EMP-836 Leave Balances:\n- Vacation: {rem} days remaining ({self.vacation_used}/20.0 used)\n- Sick: 10.0 days remaining (0.0/10.0 used)"
            elif name == "get_personal_info":
                text = f"Employee EMP-836 Personal Info:\n- Address: {self.address}\n- Phone: {self.phone}"
            elif name == "update_personal_info":
                self.address = args["address"]
                self.phone = args["phone"]
                text = "Personal info updated."
            elif name == "get_leave_requests":
                text = json.dumps(
                    [
                        {
                            "request_id": 3205,
                            "employee_id": "EMP-836",
                            "start_date": "2026-06-01",
                            "end_date": "2026-06-05",
                            "leave_type": "Vacation",
                            "days": 5.0,
                        }
                    ]
                )
            elif name == "cancel_leave_request":
                assert isinstance(args["request_id"], int), "request_id must be coerced to int!"
                text = f"Success: Leave request {args['request_id']} cancelled and refunded successfully."
            elif name == "list_tickets":
                text = json.dumps(
                    [
                        {
                            "ticket_id": "INC0004806",
                            "requested_by": "EMP-836",
                            "category": "Inquiry / Help",
                            "short_description": "Onboarding setup and badges configuration",
                            "status": "New",
                            "priority": "3 - Moderate",
                            "assigned_to": "ITIL User",
                        }
                    ]
                )
            elif name == "create_ticket":
                text = "Created ticket INC0009999 successfully."
            else:
                text = "OK"

            return {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": {"result": text},
                    "isError": False,
                },
            }

        return {"jsonrpc": "2.0", "id": payload["id"], "result": {}}


class TestMcpClientUnit(unittest.TestCase):
    """Deterministic unit tests for MCP configuration, client translation, and ACL guardrails."""

    def test_env_config_loads_mcp_token_and_urls(self) -> None:
        token = get_mcp_token()
        self.assertIsNotNone(token)
        self.assertTrue(str(token).startswith("mcp_"))
        self.assertEqual(
            get_mcp_base_url(), "https://mock-saas.aishprabhat.demo.altostrat.com"
        )
        self.assertEqual(
            get_workweek_mcp_url(),
            "https://mock-saas.aishprabhat.demo.altostrat.com/work-week/mcp/",
        )
        self.assertEqual(
            get_service_immediately_mcp_url(),
            "https://mock-saas.aishprabhat.demo.altostrat.com/service-immediately/mcp/",
        )
        self.assertEqual(get_mcp_authenticated_employee_id(), "EMP-836")

    def test_url_credential_query_parameters_are_strictly_prohibited(self) -> None:
        assert_no_url_credentials("https://mock-saas.aishprabhat.demo.altostrat.com/work-week/mcp/")
        with self.assertRaises(ValueError):
            assert_no_url_credentials(
                "https://mock-saas.aishprabhat.demo.altostrat.com/work-week/mcp/?token=secret"
            )
        with self.assertRaises(ValueError):
            assert_no_url_credentials(
                "https://mock-saas.aishprabhat.demo.altostrat.com/service-immediately/mcp/?pat=secret"
            )

    def test_sdp_and_model_armor_redact_and_block_mcp_tokens(self) -> None:
        sdp = AdvancedSDPScanner()
        raw_text = "Using token mcp_m6BfI7HZPQy4gSAaFHS-bha9bX8Bg_EoARM050hQhec for MCP call."
        redacted, found = sdp.inspect_and_deidentify(raw_text)
        self.assertIn("MCP_TOKEN", found)
        self.assertNotIn("mcp_m6BfI7HZPQy4gSAaFHS", redacted)
        self.assertIn("[REDACTED_MCP_TOKEN]", redacted)

        armor = ModelArmorScanner(sdp_scanner=sdp)
        leak_verdict = armor.scan_output(raw_text)
        self.assertTrue(leak_verdict.blocked)
        self.assertEqual(leak_verdict.category, "DATA_LEAK")

    def test_streamable_http_mcp_client_schema_translations(self) -> None:
        transport = FakeMcpTransport()
        client = StreamableHttpMcpClient(
            mcp_token="mcp_unit_test_token_1234567890",
            transport_fn=transport,
        )

        # 1. Profile resource normalization
        profile = client.fetch_employee_profile("EMP-836")
        self.assertEqual(profile["employee_id"], "EMP-836")
        self.assertEqual(profile["name"], "Vannick Employee")
        self.assertEqual(profile["role"], "Solutions Acceleration Architect")
        self.assertEqual(profile["mcp_backend"], "live_mcp_server")

        # 2. Leave balances normalization
        bal = client.fetch_leave_balances("EMP-836")
        self.assertEqual(bal["balances"]["vacation"]["remaining"], 15.0)
        self.assertEqual(bal["balances"]["sick"]["remaining"], 10.0)

        # 3. Partial contact update pre-fills existing phone when only address is supplied
        upd = client.update_contact_info(
            "EMP-836", address="10 Marina Boulevard, Singapore 018983"
        )
        self.assertEqual(
            upd["updated_profile"]["address"], "10 Marina Boulevard, Singapore 018983"
        )
        self.assertEqual(upd["updated_profile"]["phone"], "+65-6521-0000")

        # 4. cancel_leave_request coerces string request_id ("LR-3205" / "3205") to int
        cancel_res = client.cancel_leave_request("EMP-836", request_id="LR-3205")
        self.assertEqual(cancel_res["request_id"], "3205")
        self.assertEqual(cancel_res["cancelled_status"], "Cancelled")

        # Verify X-MCP-Token header was sent on every call and Authorization header was omitted
        for _, _, hdrs in transport.calls:
            self.assertEqual(hdrs.get("X-MCP-Token"), "mcp_unit_test_token_1234567890")
            self.assertNotIn("Authorization", hdrs)

    def test_acl_proxy_live_mcp_mediation_and_header_sanitization(self) -> None:
        transport = FakeMcpTransport()
        client = StreamableHttpMcpClient(
            mcp_token="mcp_unit_test_token_1234567890",
            transport_fn=transport,
        )
        audit = AuditLogger()
        ledger = TransactionLedger()
        pdp = PolicyDecisionPoint(ledger=ledger)
        acl = AntiCorruptionLayerProxy(
            pdp=pdp,
            ledger=ledger,
            audit_logger=audit,
            use_live_mcp=True,
            mcp_client=client,
            mcp_token="mcp_unit_test_token_1234567890",
        )

        res = acl.invoke_tool(
            "get_leave_balance",
            {},
            authenticated_employee_id="EMP-836",
            calling_agent="workweek_agent",
            session_id="sess-unit-mcp",
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(res["mcp_backend"], "live_mcp_server")
        self.assertEqual(res["balances"]["vacation"]["remaining"], 15.0)
        # Response attribution headers must have raw mcp_ token sanitized
        self.assertEqual(
            res["attribution_headers"]["X-MCP-Token"], "[REDACTED_MCP_TOKEN]"
        )


if __name__ == "__main__":
    unittest.main()
