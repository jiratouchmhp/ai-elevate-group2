"""Capability manifest — the authoritative tool allow-list per agent (SDD §3.1, §5.2, FR-1.1).

Enforced at `before_tool_callback` (runner-wide plugin), so an out-of-manifest call
is blocked AND audited regardless of which agent the model routed to.
"""

from __future__ import annotations

ORCHESTRATOR = "hr_agent"  # root orchestrator; name kept in sync with agents-cli-manifest.yaml
POLICY = "policy_agent"
WORKWEEK = "workweek_agent"
ITSM = "itsm_agent"

# ADK framework plumbing for delegation / task completion — not backend capabilities.
FRAMEWORK_PREFIXES = ("request_task_", "transfer_to_agent", "finish_task", "set_model_response")

MANIFEST: dict[str, set[str]] = {
    # Delegation only — holds no backend tools (§3.1). ADK exposes single_turn
    # sub-agents to the parent as tools named after the sub-agent.
    ORCHESTRATOR: {POLICY, WORKWEEK, ITSM},
    POLICY: {"search_policy"},
    WORKWEEK: {
        "get_profile", "get_personal_info", "get_leave_balance", "get_leave_requests",
        "propose_contact_update", "propose_leave", "propose_cancel_leave", "commit_action",
    },
    ITSM: {
        "get_ticket", "list_tickets", "propose_incident", "propose_comment",
        "propose_status_update", "commit_action",
    },
}

# Which mutating actions each agent may commit (a WorkWeek token cannot be committed by ITSM).
COMMITTABLE: dict[str, set[str]] = {
    WORKWEEK: {"submit_leave", "cancel_leave", "update_contact"},
    ITSM: {"create_incident", "add_comment", "update_status"},
}

# Named denials: published by the vendor host but never exposed (SDD §5.2). An
# attempt is logged as a denial rather than merely failing.
NAMED_DENIALS: dict[str, str] = {
    "get_employee_feedback": "B-5: performance data is out of scope",
    "get_current_employee_id": "Identity derives only from the verified auth context (§4.4)",
    "mint_mcp_token": "Credential minting would allow privilege escalation",
    "list_mcp_tokens": "Credential enumeration",
    "delete_mcp_token": "Credential denial of service",
    "amend_leave_request": "Amendment deferred post-MVP (use cancel + submit)",
    "approve_leave": "B-2: the agent submits, it never approves",
}


def is_framework_tool(name: str) -> bool:
    return name.startswith(FRAMEWORK_PREFIXES)


def check(agent_name: str, tool_name: str) -> tuple[bool, str]:
    if tool_name in NAMED_DENIALS:
        return False, f"NAMED_DENIAL: {NAMED_DENIALS[tool_name]}"
    if is_framework_tool(tool_name):
        return True, "framework"
    allowed = MANIFEST.get(agent_name)
    if allowed is None:
        return False, f"UNKNOWN_AGENT: {agent_name}"
    if tool_name not in allowed:
        return False, f"OUT_OF_MANIFEST: {agent_name} may not call {tool_name}"
    return True, "manifest"
