"""Anti-Corruption Layer (ACL) & MCP-to-MCP Interception Proxy (SDD §5.1, §5.2, D4, D9, D10).

Responsibilities:
1. Exposes the curated Tool Contract Catalogue (§5.2) to domain sub-agents.
2. Enforces explicit denials for forbidden vendor tools (`get_employee_feedback`, `get_current_employee_id`,
   `POST /api/mcp-tokens`, `PUT /timeoff/requests/{id}`) and logs every blocked attempt (FR-1.1, B-5).
3. Brokers per-persona Personal Access Tokens (`X-MCP-Token`) via Agent Identity Auth Manager
   and attaches per-agent SPIFFE IDs (`spiffe://altostrat.sg/ns/agent-runtime/sa/...`) (D10, OQ-12).
4. Invokes the deterministic Policy Decision Point (PDP) before any mutating call.
5. Enforces NFR-4.2 retry policy: exponential backoff on reads (2x–3x), ZERO blind retries on writes
   (reconciling against backend state via `idempotency_key` on transient timeout).
6. Provides non-technical failure messages matching SDD §5.4 when backends are unavailable.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import uuid

from app.governance.audit_logger import (
    AGENT_VERSION,
    AuditLogger,
    DEFAULT_AUDIT_LOGGER,
)
from app.ledger.transaction_ledger import DEFAULT_LEDGER, TransactionLedger
from app.pdp.rules_engine import DEFAULT_PDP, PDPDecision, PolicyDecisionPoint
from app.safety.guardrails import AdvancedSDPScanner


@dataclass(frozen=True)
class ToolSpec:
    agent_tool: str
    vendor_mcp_tool: str
    system: str
    mutating: bool
    timeout_seconds: float
    max_retries: int
    allowed_agents: tuple[str, ...]


# SDD §5.2 Authoritative Tool Contract Catalogue (FR-1.1)
TOOL_CONTRACT_CATALOGUE: Dict[str, ToolSpec] = {
    "search_policy": ToolSpec(
        agent_tool="search_policy",
        vendor_mcp_tool="internal://corpus/search_policy",
        system="Corpus",
        mutating=False,
        timeout_seconds=3.0,
        max_retries=2,
        allowed_agents=("policy_agent",),
    ),
    "get_profile": ToolSpec(
        agent_tool="get_profile",
        vendor_mcp_tool="workweek://employees/profile",
        system="WorkWeek",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("workweek_agent",),
    ),
    "get_personal_info": ToolSpec(
        agent_tool="get_personal_info",
        vendor_mcp_tool="get_personal_info",
        system="WorkWeek",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("workweek_agent",),
    ),
    "get_leave_balance": ToolSpec(
        agent_tool="get_leave_balance",
        vendor_mcp_tool="get_employee_balances",
        system="WorkWeek",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("workweek_agent",),
    ),
    "get_leave_requests": ToolSpec(
        agent_tool="get_leave_requests",
        vendor_mcp_tool="get_leave_requests",
        system="WorkWeek",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("workweek_agent",),
    ),
    "update_contact": ToolSpec(
        agent_tool="update_contact",
        vendor_mcp_tool="update_personal_info",
        system="WorkWeek",
        mutating=True,
        timeout_seconds=8.0,
        max_retries=0,  # Never blind-retry writes (§5.2)
        allowed_agents=("workweek_agent",),
    ),
    "submit_leave": ToolSpec(
        agent_tool="submit_leave",
        vendor_mcp_tool="request_time_off",
        system="WorkWeek",
        mutating=True,
        timeout_seconds=8.0,
        max_retries=0,
        allowed_agents=("workweek_agent",),
    ),
    "cancel_leave": ToolSpec(
        agent_tool="cancel_leave",
        vendor_mcp_tool="cancel_leave_request",
        system="WorkWeek",
        mutating=True,
        timeout_seconds=8.0,
        max_retries=0,
        allowed_agents=("workweek_agent",),
    ),
    "get_ticket": ToolSpec(
        agent_tool="get_ticket",
        vendor_mcp_tool="serviceimmediately://tickets/{id}",
        system="ServiceImmediately",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("service_immediately_agent",),
    ),
    "list_tickets": ToolSpec(
        agent_tool="list_tickets",
        vendor_mcp_tool="list_tickets",
        system="ServiceImmediately",
        mutating=False,
        timeout_seconds=5.0,
        max_retries=3,
        allowed_agents=("service_immediately_agent",),
    ),
    "create_incident": ToolSpec(
        agent_tool="create_incident",
        vendor_mcp_tool="create_ticket",
        system="ServiceImmediately",
        mutating=True,
        timeout_seconds=8.0,
        max_retries=0,
        allowed_agents=("service_immediately_agent",),
    ),
    "add_comment": ToolSpec(
        agent_tool="add_comment",
        vendor_mcp_tool="add_ticket_comment",
        system="ServiceImmediately",
        mutating=True,
        timeout_seconds=5.0,
        max_retries=0,
        allowed_agents=("service_immediately_agent",),
    ),
    "update_status": ToolSpec(
        agent_tool="update_status",
        vendor_mcp_tool="update_ticket_status",
        system="ServiceImmediately",
        mutating=True,
        timeout_seconds=5.0,
        max_retries=0,
        allowed_agents=("service_immediately_agent",),
    ),
}

# SDD §5.2 Explicitly Denied Tools / Endpoints
EXPLICITLY_DENIED_TOOLS: Dict[str, str] = {
    "get_employee_feedback": "Performance-adjacent data; violates B-5 (no payroll, comp, or performance data).",
    "GET /work-week/api/employees/{id}/feedback": "Performance-adjacent data; violates B-5.",
    "mint_mcp_token": "Credential minting (POST /api/mcp-tokens) prohibited; privilege escalation vector.",
    "POST /api/mcp-tokens": "Credential minting prohibited.",
    "list_mcp_tokens": "Credential enumeration (GET /api/mcp-tokens) prohibited.",
    "delete_mcp_token": "Credential revocation/DoS (DELETE /api/mcp-tokens/{id}) prohibited.",
    "amend_timeoff_request": "Deferred post-MVP; amendment semantics overlap cancel + submit.",
    "get_current_employee_id": (
        "Identity must derive exclusively from verified IAP context (§4.4), never from a backend lookup "
        "that the model can invoke."
    ),
}

# D10 Agent Identity SPIFFE IDs per agent
AGENT_SPIFFE_IDS: Dict[str, str] = {
    "root_orchestrator": "spiffe://altostrat.sg/ns/agent-runtime/sa/root-orchestrator",
    "policy_agent": "spiffe://altostrat.sg/ns/agent-runtime/sa/policy-agent",
    "workweek_agent": "spiffe://altostrat.sg/ns/agent-runtime/sa/workweek-agent",
    "service_immediately_agent": "spiffe://altostrat.sg/ns/agent-runtime/sa/service-immediately-agent",
}

# OQ-12 / D10 Per-persona PATs managed via Agent Identity Auth Manager
PERSONA_MCP_TOKENS: Dict[str, str] = {
    "EMP-SG-001": "pat-sg001-a9f8e7d6c5b4",
    "EMP-SG-002": "pat-sg002-b1c2d3e4f5a6",
    "EMP-SG-003": "pat-sg003-c7d8e9f0a1b2",
}


class MockVendorBackendState:
    """Sandbox tenant data for WorkWeek (HCM) and ServiceImmediately (ITSM) (CON-1, §7.1)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.workweek_available: bool = True
        self.service_immediately_available: bool = True
        self.transient_read_failures_remaining: int = 0

        self.employees: Dict[str, Dict[str, Any]] = {
            "EMP-SG-001": {
                "employee_id": "EMP-SG-001",
                "name": "Mei Ling Tan",
                "email": "meiling.tan@altostrat.sg",
                "department": "Cloud Platform Engineering",
                "role": "Senior Staff Engineer",
                "manager": "David Lim (EMP-SG-099)",
                "hire_date": "2018-03-15",  # 8+ years service -> 21 vacation days tier
                "location_status": "Hybrid",
                "office_location": "Singapore One Raffles Quay",
                "address": "18 Marina Boulevard, #12-04, Singapore 018980",
                "phone": "+65 9123 4567",
            },
            "EMP-SG-002": {
                "employee_id": "EMP-SG-002",
                "name": "Arjun Nair",
                "email": "arjun.nair@altostrat.sg",
                "department": "Workplace Operations",
                "role": "Operations Coordinator",
                "manager": "Sarah Chen (EMP-SG-088)",
                "hire_date": "2024-06-01",
                "location_status": "On-Site",  # Ineligible for Remote/Hybrid equipment allowance (§5.4)
                "office_location": "Singapore One Raffles Quay",
                "address": "45 Tampines Ave 4, #08-11, Singapore 529681",
                "phone": "+65 9876 5432",
            },
            "EMP-SG-003": {
                "employee_id": "EMP-SG-003",
                "name": "Chloe Wong",
                "email": "chloe.wong@altostrat.sg",
                "department": "Product Management",
                "role": "Principal Product Manager",
                "manager": "David Lim (EMP-SG-099)",
                "hire_date": "2019-01-10",
                "location_status": "Remote",
                "office_location": "Singapore One Raffles Quay",
                "address": "102 River Valley Road, #05-02, Singapore 238276",
                "phone": "+65 8234 5678",
            },
        }

        self.balances: Dict[str, Dict[str, Dict[str, float]]] = {
            "EMP-SG-001": {
                "vacation": {"accrued": 21.0, "used": 16.0, "remaining": 5.0},
                "sick": {"accrued": 14.0, "used": 2.0, "remaining": 12.0},
                "hospitalization": {"accrued": 46.0, "used": 0.0, "remaining": 46.0},
            },
            "EMP-SG-002": {
                "vacation": {"accrued": 20.0, "used": 18.0, "remaining": 2.0},
                "sick": {"accrued": 14.0, "used": 0.0, "remaining": 14.0},
                "hospitalization": {"accrued": 46.0, "used": 0.0, "remaining": 46.0},
            },
            "EMP-SG-003": {
                "vacation": {"accrued": 21.0, "used": 6.0, "remaining": 15.0},
                "sick": {"accrued": 14.0, "used": 1.0, "remaining": 13.0},
                "hospitalization": {"accrued": 46.0, "used": 0.0, "remaining": 46.0},
            },
        }

        self.leave_requests: Dict[str, Dict[str, Any]] = {
            "LR-88100": {
                "request_id": "LR-88100",
                "employee_id": "EMP-SG-001",
                "leave_type": "Vacation",
                "start_date": "2026-10-15",
                "end_date": "2026-10-16",
                "days": 2.0,
                "status": "Approved",
            }
        }

        self.tickets: Dict[str, Dict[str, Any]] = {
            "INC123456": {
                "ticket_id": "INC123456",
                "requestor_id": "EMP-SG-001",
                "category": "IT",
                "short_description": "VPN token sync latency on corporate MacBook",
                "detailed_description": "Intermittent token prompt when connecting from home fiber.",
                "priority": "3 - Moderate",
                "status": "New",
                "assignee": "IT Network Desk SG",
                "actor_type": "HUMAN",
                "comments": [
                    {
                        "author": "IT Network Desk SG",
                        "timestamp": "2026-09-22T09:15:00Z",
                        "text": "Investigating gateway logs in asia-southeast1.",
                    }
                ],
            },
            "INC123457": {
                "ticket_id": "INC123457",
                "requestor_id": "EMP-SG-001",
                "category": "IT",
                "short_description": "Laptop display flickering after sleep",
                "detailed_description": "External thunderbolt display flickers.",
                "priority": "3 - Moderate",
                "status": "In Progress",
                "assignee": "IT Client Platform SG",
                "actor_type": "HUMAN",
                "comments": [],
            },
        }


DEFAULT_BACKEND_STATE = MockVendorBackendState()


class AntiCorruptionLayerProxy:
    """MCP-to-MCP Interception Proxy on Cloud Run (SDD D9, §5.1, §5.2)."""

    def __init__(
        self,
        *,
        pdp: Optional[PolicyDecisionPoint] = None,
        ledger: Optional[TransactionLedger] = None,
        audit_logger: Optional[AuditLogger] = None,
        backend_state: Optional[MockVendorBackendState] = None,
    ) -> None:
        self.pdp = pdp or DEFAULT_PDP
        self.ledger = ledger or DEFAULT_LEDGER
        self.audit = audit_logger or DEFAULT_AUDIT_LOGGER
        self.backend = backend_state or DEFAULT_BACKEND_STATE
        self.sdp = AdvancedSDPScanner()

    def build_attribution_headers(
        self, *, employee_id: str, agent_name: str, correlation_id: str
    ) -> Dict[str, str]:
        """Constructs FR-1.2 & §5.1 attribution + X-MCP-Token headers."""
        spiffe_id = AGENT_SPIFFE_IDS.get(
            agent_name, f"spiffe://altostrat.sg/ns/agent-runtime/sa/{agent_name}"
        )
        pat_token = PERSONA_MCP_TOKENS.get(employee_id, "pat-default-sandbox")
        return {
            "X-MCP-Token": pat_token,
            "X-Actor-Type": "AUTOMATED_AGENT",
            "X-On-Behalf-Of": employee_id,
            "X-Agent-Id": spiffe_id,
            "X-Agent-Version": AGENT_VERSION,
            "X-Correlation-Id": correlation_id,
        }

    def invoke_tool(
        self,
        tool_name: str,
        args: Dict[str, Any],
        *,
        authenticated_employee_id: str,
        calling_agent: str,
        session_id: str = "sess-default",
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Mediates every tool call through capability manifest, RBAC, PDP, and Ledger."""
        corr_id = correlation_id or f"corr-{uuid.uuid4().hex[:10]}"
        spiffe_id = AGENT_SPIFFE_IDS.get(
            calling_agent, f"spiffe://altostrat.sg/ns/agent-runtime/sa/{calling_agent}"
        )
        redacted_args = self.sdp.redact_dict(args)

        # 1. Check Explicitly Denied Tools (§5.2)
        if tool_name in EXPLICITLY_DENIED_TOOLS:
            denial_reason = EXPLICITLY_DENIED_TOOLS[tool_name]
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="DENY",
                pdp_rule_id="CAPABILITY_MANIFEST_EXPLICIT_DENIAL",
                outcome="DENIED",
                notes=denial_reason,
            )
            return {
                "status": "DENIED",
                "error_code": "TOOL_EXPLICITLY_DENIED",
                "rule_id": "FR-1.1_CAPABILITY_BOUNDARY",
                "message": f"Tool '{tool_name}' is blocked by the capability manifest: {denial_reason}",
            }

        # 2. Check Authoritative Tool Contract Catalogue (§5.2)
        spec = TOOL_CONTRACT_CATALOGUE.get(tool_name)
        if not spec:
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="DENY",
                pdp_rule_id="CAPABILITY_MANIFEST_UNKNOWN_TOOL",
                outcome="DENIED",
                notes=f"Uncatalogued tool '{tool_name}' blocked at ACL.",
            )
            return {
                "status": "DENIED",
                "error_code": "UNKNOWN_TOOL",
                "rule_id": "FR-1.1_CAPABILITY_BOUNDARY",
                "message": f"Tool '{tool_name}' is not in the authorized capability manifest.",
            }

        # 3. Check Sub-Agent Blast-Radius Scoping (D2 & §3.1)
        if calling_agent not in spec.allowed_agents:
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="DENY",
                pdp_rule_id="D2_AGENT_BLAST_RADIUS_CONTAINMENT",
                outcome="DENIED",
                notes=f"Agent '{calling_agent}' is not permitted to invoke '{tool_name}'.",
            )
            return {
                "status": "DENIED",
                "error_code": "AGENT_SCOPE_VIOLATION",
                "rule_id": "D2_BLAST_RADIUS_CONTAINMENT",
                "message": f"Agent '{calling_agent}' does not hold authority to invoke '{tool_name}'.",
            }

        # 4. Enforce RBAC / Cross-User Isolation (FR-1.5 & T-3):
        # If the model passed an explicit employee_id parameter that differs from the IAP-verified
        # authenticated_employee_id, block immediately as a cross-user access attempt!
        requested_emp = args.get("employee_id")
        if requested_emp and str(requested_emp).strip() != authenticated_employee_id:
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="DENY",
                pdp_rule_id="FR-1.5_CROSS_USER_ISOLATION",
                outcome="DENIED",
                notes=f"Blocked cross-user access attempt from {authenticated_employee_id} targeting {requested_emp}.",
            )
            return {
                "status": "DENIED",
                "error_code": "CROSS_USER_ACCESS_DENIED",
                "rule_id": "FR-1.5_RBAC_ISOLATION",
                "message": (
                    "Access denied (FR-1.5): You may only view or modify your own employee records. "
                    "Cross-user queries (such as viewing another employee's or manager's records) are strictly prohibited."
                ),
            }

        # Always inject the verified employee_id from IAP context (§4.4)
        scoped_args = dict(args)
        scoped_args["employee_id"] = authenticated_employee_id

        # 5. Check Backend Availability & NFR-4.1 / §5.4 Failure Modes
        if spec.system == "WorkWeek" and not self.backend.workweek_available:
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="ALLOW" if not spec.mutating else "PENDING",
                outcome="ERROR",
                notes="WorkWeek backend unavailable (503).",
            )
            return {
                "status": "BACKEND_UNAVAILABLE",
                "http_status": 503,
                "system": "WorkWeek",
                "user_message": (
                    "WorkWeek is temporarily unavailable. Your leave balance can't be checked right now "
                    "— please try again shortly."
                ),
            }

        if spec.system == "ServiceImmediately" and not self.backend.service_immediately_available:
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="ALLOW" if not spec.mutating else "PENDING",
                outcome="ERROR",
                notes="ServiceImmediately backend unavailable (503).",
            )
            return {
                "status": "BACKEND_UNAVAILABLE",
                "http_status": 503,
                "system": "ServiceImmediately",
                "user_message": (
                    "I can't reach the service desk. You can raise this directly at "
                    "https://service-immediately.altostrat.sg/portal."
                ),
            }

        # 6. Gather current backend context for deterministic PDP validation
        context_data = self._build_pdp_context(tool_name, scoped_args, authenticated_employee_id)

        # 7. Run Deterministic Policy Decision Point (PDP) evaluation
        decision: PDPDecision = self.pdp.evaluate(
            tool_name,
            scoped_args,
            employee_id=authenticated_employee_id,
            context_data=context_data,
        )

        if not decision.allowed:
            outcome_label = (
                "CONFIRMATION_REQUIRED"
                if decision.status == "CONFIRMATION_REQUIRED"
                else "DENIED"
            )
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision=decision.status,
                pdp_rule_id=decision.rule_id,
                outcome=outcome_label,
                backend_ref=decision.existing_backend_ref,
                notes=decision.reason,
            )
            return {
                "status": decision.status,
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "user_message": decision.user_message,
                "warnings": decision.warnings,
                "confirmation_card": decision.confirmation_card,
                "existing_backend_ref": decision.existing_backend_ref,
                "idempotency_key": decision.idempotency_key,
            }

        # Apply any PDP modifications (e.g., programmatic priority downgrade §5.5)
        effective_args = decision.modified_args or scoped_args
        headers = self.build_attribution_headers(
            employee_id=authenticated_employee_id,
            agent_name=calling_agent,
            correlation_id=corr_id,
        )

        # 8. Execute call with NFR-4.2 retry / idempotency semantics
        if spec.mutating:
            idem_key = decision.idempotency_key or self.ledger.generate_idempotency_key(
                authenticated_employee_id, tool_name, effective_args
            )
            # Reconcile if already committed in Ledger (idempotent replay)
            existing = self.ledger.get_entry(idem_key)
            if existing and existing.status == "COMMITTED":
                return {
                    "status": "SUCCESS",
                    "idempotent_replay": True,
                    "backend_ref": existing.backend_ref,
                    "idempotency_key": idem_key,
                    "warnings": decision.warnings,
                    "attribution_headers": headers,
                }

            self.ledger.record_intent(
                idempotency_key=idem_key,
                employee_id=authenticated_employee_id,
                tool_name=tool_name,
                payload=effective_args,
            )
            result = self._execute_backend_operation(
                tool_name, effective_args, authenticated_employee_id, headers
            )
            backend_ref = str(
                result.get("request_id") or result.get("ticket_id") or f"REF-{uuid.uuid4().hex[:6].upper()}"
            )
            self.ledger.mark_committed(idem_key, backend_ref)
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=self.sdp.redact_dict(effective_args),
                pdp_decision=decision.status,
                pdp_rule_id=decision.rule_id,
                outcome="SUCCESS",
                backend_ref=backend_ref,
                notes="Mutating tool executed after PDP ALLOW and explicit user confirmation.",
            )
            result["status"] = "SUCCESS"
            result["idempotency_key"] = idem_key
            result["backend_ref"] = backend_ref
            result["warnings"] = decision.warnings
            result["attribution_headers"] = headers
            return result

        # Read-only operation with exponential backoff retry (NFR-4.2)
        attempts = 0
        while True:
            attempts += 1
            if self.backend.transient_read_failures_remaining > 0:
                self.backend.transient_read_failures_remaining -= 1
                if attempts <= spec.max_retries:
                    continue
                return {
                    "status": "BACKEND_UNAVAILABLE",
                    "http_status": 503,
                    "system": spec.system,
                    "user_message": f"{spec.system} is temporarily unavailable after {attempts} retries.",
                }

            result = self._execute_backend_operation(
                tool_name, effective_args, authenticated_employee_id, headers
            )
            self.audit.record(
                session_id=session_id,
                employee_id=authenticated_employee_id,
                correlation_id=corr_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=redacted_args,
                pdp_decision="ALLOW",
                pdp_rule_id="READ_ONLY_PASS",
                outcome="SUCCESS",
                notes=f"Read operation succeeded on attempt {attempts}.",
            )
            result["status"] = "SUCCESS"
            result["attempts"] = attempts
            result["attribution_headers"] = headers
            return result

    def _build_pdp_context(
        self, tool_name: str, args: Dict[str, Any], employee_id: str
    ) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {}
        emp = self.backend.employees.get(employee_id, {})
        ctx["location_status"] = emp.get("location_status", "Hybrid")
        ctx["address"] = emp.get("address", "")
        ctx["balances"] = copy.deepcopy(self.backend.balances.get(employee_id, {}))

        if tool_name in ("cancel_leave", "cancel_leave_request"):
            req_id = str(args.get("request_id", ""))
            req = self.backend.leave_requests.get(req_id)
            if req:
                ctx["request_owner_id"] = req.get("employee_id")

        if tool_name in ("update_status", "update_ticket_status", "add_comment", "add_ticket_comment"):
            t_id = str(args.get("ticket_id", ""))
            ticket = self.backend.tickets.get(t_id)
            if ticket:
                ctx["current_status"] = ticket.get("status", "New")
                ctx["requestor_id"] = ticket.get("requestor_id", employee_id)

        return ctx

    def _execute_backend_operation(
        self,
        tool_name: str,
        args: Dict[str, Any],
        employee_id: str,
        headers: Dict[str, str],
    ) -> Dict[str, Any]:
        if tool_name in ("get_profile", "get_personal_info"):
            profile = copy.deepcopy(self.backend.employees.get(employee_id, {}))
            return {"profile": profile}

        if tool_name == "get_leave_balance":
            balances = copy.deepcopy(self.backend.balances.get(employee_id, {}))
            return {"employee_id": employee_id, "balances": balances}

        if tool_name == "get_leave_requests":
            reqs = [
                copy.deepcopy(r)
                for r in self.backend.leave_requests.values()
                if r.get("employee_id") == employee_id
            ]
            return {"employee_id": employee_id, "leave_requests": reqs}

        if tool_name == "update_contact":
            emp = self.backend.employees.setdefault(employee_id, {"employee_id": employee_id})
            if args.get("address"):
                emp["address"] = str(args["address"]).strip()
            if args.get("phone"):
                emp["phone"] = str(args["phone"]).strip()
            return {
                "request_id": f"WW-CNT-{uuid.uuid4().hex[:6].upper()}",
                "updated_profile": copy.deepcopy(emp),
            }

        if tool_name == "submit_leave":
            leave_type = str(args.get("leave_type", "Vacation"))
            days = float(args.get("days", args.get("work_days", 1.0)))
            type_key = "vacation" if leave_type.lower().startswith("vac") else "sick"
            bal = self.backend.balances.setdefault(employee_id, {}).setdefault(
                type_key, {"accrued": 20.0, "used": 0.0, "remaining": 20.0}
            )
            bal["used"] = round(bal["used"] + days, 1)
            bal["remaining"] = round(bal["remaining"] - days, 1)
            req_id = f"LR-{88200 + len(self.backend.leave_requests) + 1}"
            record = {
                "request_id": req_id,
                "employee_id": employee_id,
                "leave_type": leave_type,
                "start_date": args["start_date"],
                "end_date": args["end_date"],
                "days": days,
                "status": "Submitted",
                "actor_type": headers.get("X-Actor-Type", "AUTOMATED_AGENT"),
            }
            self.backend.leave_requests[req_id] = record
            return {
                "request_id": req_id,
                "leave_request": copy.deepcopy(record),
                "remaining_balance": bal["remaining"],
            }

        if tool_name == "cancel_leave":
            req_id = str(args.get("request_id", ""))
            req = self.backend.leave_requests.get(req_id)
            if not req:
                return {"error": f"Leave request {req_id} not found."}
            days = float(req.get("days", 0.0))
            leave_type = str(req.get("leave_type", "Vacation"))
            type_key = "vacation" if leave_type.lower().startswith("vac") else "sick"
            bal = self.backend.balances.get(employee_id, {}).get(type_key)
            if bal and req.get("status") != "Cancelled":
                bal["used"] = max(0.0, round(bal["used"] - days, 1))
                bal["remaining"] = round(bal["remaining"] + days, 1)
            req["status"] = "Cancelled"
            return {
                "request_id": req_id,
                "cancelled_status": "Cancelled",
                "refunded_days": days,
                "remaining_balance": bal["remaining"] if bal else None,
            }

        if tool_name == "get_ticket":
            t_id = str(args.get("ticket_id", ""))
            ticket = self.backend.tickets.get(t_id)
            if not ticket or ticket.get("requestor_id") != employee_id:
                return {"error": f"Ticket {t_id} not found for employee {employee_id}."}
            return {"ticket": copy.deepcopy(ticket)}

        if tool_name == "list_tickets":
            items = [
                copy.deepcopy(t)
                for t in self.backend.tickets.values()
                if t.get("requestor_id") == employee_id
            ]
            return {"tickets": items}

        if tool_name == "create_incident":
            t_id = f"INC00{42318 + len(self.backend.tickets)}"
            ticket = {
                "ticket_id": t_id,
                "requestor_id": employee_id,
                "category": args.get("category", "IT"),
                "short_description": args.get("short_description", ""),
                "detailed_description": args.get("detailed_description", args.get("short_description", "")),
                "priority": args.get("priority", "3 - Moderate"),
                "status": "New",
                "assignee": f"{args.get('category', 'IT')} Service Desk SG",
                "actor_type": headers.get("X-Actor-Type", "AUTOMATED_AGENT"),
                "shipping_address": args.get("shipping_address"),
                "comments": [],
            }
            self.backend.tickets[t_id] = ticket
            return {"ticket_id": t_id, "ticket": copy.deepcopy(ticket)}

        if tool_name == "add_comment":
            t_id = str(args.get("ticket_id", ""))
            ticket = self.backend.tickets.get(t_id)
            if not ticket:
                return {"error": f"Ticket {t_id} not found."}
            comment_obj = {
                "author": employee_id,
                "actor_type": headers.get("X-Actor-Type", "AUTOMATED_AGENT"),
                "text": args.get("comment", ""),
            }
            ticket.setdefault("comments", []).append(comment_obj)
            return {"ticket_id": t_id, "ticket": copy.deepcopy(ticket)}

        if tool_name == "update_status":
            t_id = str(args.get("ticket_id", ""))
            ticket = self.backend.tickets.get(t_id)
            if not ticket:
                return {"error": f"Ticket {t_id} not found."}
            ticket["status"] = args.get("new_status", args.get("status"))
            if args.get("resolution_notes"):
                ticket.setdefault("comments", []).append(
                    {
                        "author": employee_id,
                        "actor_type": headers.get("X-Actor-Type", "AUTOMATED_AGENT"),
                        "text": f"Resolution notes: {args['resolution_notes']}",
                    }
                )
            return {"ticket_id": t_id, "ticket": copy.deepcopy(ticket)}

        return {"status": "NO_OP"}


DEFAULT_ACL_PROXY = AntiCorruptionLayerProxy()
