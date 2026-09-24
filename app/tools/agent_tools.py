"""Authoritative Agent Tools bound to Domain Sub-Agents (SDD §3.1, §5.2, D2, D9).

Tool scoping enforces blast-radius containment (D2):
- Policy Agent holds ONLY `search_policy` (read-only; physically incapable of backend writes).
- WorkWeek Agent holds ONLY the 7 HCM tools (`get_profile`, `get_personal_info`, `get_leave_balance`,
  `get_leave_requests`, `update_contact`, `submit_leave`, `cancel_leave`).
- ServiceImmediately Agent holds ONLY the 5 ITSM tools (`get_ticket`, `list_tickets`,
  `create_incident`, `add_comment`, `update_status`).
- Root Orchestrator holds ZERO backend tools (delegation only).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.acl.mcp_proxy import AntiCorruptionLayerProxy, DEFAULT_ACL_PROXY
from app.adk_compat import ToolContext
from app.governance.audit_logger import DEFAULT_AUDIT_LOGGER
from app.rag.retriever import DEFAULT_RETRIEVER, PolicyRetriever


def _resolve_context(
    tool_context: Optional[ToolContext], default_agent: str
) -> tuple[str, str, str, AntiCorruptionLayerProxy]:
    state = tool_context.state if tool_context and hasattr(tool_context, "state") else {}
    emp_id = str(state.get("authenticated_employee_id", "EMP-SG-001"))
    sess_id = (
        tool_context.session_id
        if tool_context and hasattr(tool_context, "session_id")
        else str(state.get("session_id", "sess-default"))
    )
    agent_name = (
        tool_context.agent_name
        if tool_context and hasattr(tool_context, "agent_name")
        else default_agent
    )
    acl: AntiCorruptionLayerProxy = state.get("acl_proxy", DEFAULT_ACL_PROXY)
    return emp_id, sess_id, agent_name, acl


# =============================================================================
# 1. Policy Agent Tool (Read-Only Grounding over Approved Handbook)
# =============================================================================
def search_policy(
    query: str,
    jurisdiction: str = "SG",
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Searches the governed Altostrat Singapore Employee Policy Handbook (UC-1.1, FR-5.1..5.4).

    Applies C-1..C-6 corpus mitigations, spotlighting delimiters (§4.3), and strict refusal
    when retrieved context is insufficient.
    """
    emp_id, sess_id, agent_name, _ = _resolve_context(tool_context, "policy_agent")
    state = tool_context.state if tool_context and hasattr(tool_context, "state") else {}
    retriever: PolicyRetriever = state.get("retriever", DEFAULT_RETRIEVER)
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)

    res = retriever.search(query=query, jurisdiction=jurisdiction)
    doc_ids = [c["chunk_id"] for c in res.chunks]
    scores = [float(c["relevance_score"]) for c in res.chunks]

    audit.record(
        session_id=sess_id,
        employee_id=emp_id,
        agent_id="spiffe://altostrat.sg/ns/agent-runtime/sa/policy-agent",
        tool_invoked="search_policy",
        tool_args_redacted={
            "query": query,
            "jurisdiction": jurisdiction,
            "rag_backend": res.rag_backend,
            "rag_corpus": res.rag_corpus,
        },
        pdp_decision="ALLOW",
        pdp_rule_id="STRICT_GROUNDING_CHECK",
        retrieved_doc_ids=doc_ids,
        relevance_scores=scores,
        outcome="REFUSED_UNANSWERABLE" if res.refusal else "SUCCESS",
        notes=(
            res.refusal_reason
            or f"Grounded policy retrieval via {res.rag_backend} ({res.rag_corpus}, cloud_hits={res.cloud_hits_count})."
        ),
    )

    citations = [
        {
            "chunk_id": c["chunk_id"],
            "section_number": c["section_number"],
            "section_title": c["section_title"],
            "semantic_topic": c["semantic_topic"],
            "citation_anchor": c["citation_anchor"],
            "deep_link_url": c["deep_link_url"],
            "authority": c["authority"],
            "jurisdiction": c["jurisdiction"],
            "relevance_score": c["relevance_score"],
            "rag_backend": res.rag_backend,
            "rag_corpus": res.rag_corpus,
        }
        for c in res.chunks
    ]

    return {
        "status": "REFUSE" if res.refusal else "SUCCESS",
        "sufficient_context": res.sufficient_context,
        "refusal": res.refusal,
        "refusal_reason": res.refusal_reason,
        "escalation_route": res.escalation_route,
        "citations": citations,
        "spotlighted_context": res.spotlighted_context,
        "clean_context": res.clean_context,
        "corpus_version": res.corpus_version,
        "rag_backend": res.rag_backend,
        "rag_corpus": res.rag_corpus,
        "cloud_hits_count": res.cloud_hits_count,
    }


# =============================================================================
# 2. WorkWeek (HCM) Agent Tools (7 tools — SDD §3.1 & §5.2)
# =============================================================================
def get_profile(
    employee_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Fetches real-time employee profile from WorkWeek (never cached in session state per FR-3.4)."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    args: Dict[str, Any] = {}
    if employee_id:
        args["employee_id"] = employee_id
    return acl.invoke_tool(
        "get_profile",
        args,
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def get_personal_info(
    employee_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Fetches personal contact information (home address, personal phone) from WorkWeek."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    args: Dict[str, Any] = {}
    if employee_id:
        args["employee_id"] = employee_id
    return acl.invoke_tool(
        "get_personal_info",
        args,
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def get_leave_balance(
    employee_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Fetches real-time Vacation and Sick leave balances from WorkWeek (FR-3.2, FR-3.4)."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    args: Dict[str, Any] = {}
    if employee_id:
        args["employee_id"] = employee_id
    return acl.invoke_tool(
        "get_leave_balance",
        args,
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def get_leave_requests(
    employee_id: Optional[str] = None,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Lists submitted/approved leave requests for the authenticated employee in WorkWeek."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    args: Dict[str, Any] = {}
    if employee_id:
        args["employee_id"] = employee_id
    return acl.invoke_tool(
        "get_leave_requests",
        args,
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def update_contact(
    address: str = "",
    phone: str = "",
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Updates personal address and/or phone in WorkWeek after PDP CONTACT_FORMAT & B-3 confirmation."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    return acl.invoke_tool(
        "update_contact",
        {"address": address, "phone": phone, "confirmed": confirmed},
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def submit_leave(
    start_date: str,
    end_date: str,
    leave_type: str = "Vacation",
    days: float = 1.0,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Submits a leave request in WorkWeek after deterministic PDP validation (LEAVE_BALANCE_CAP, LEAVE_CHRONOLOGY, LEAVE_NOTICE_15D) and B-3 confirmation."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    return acl.invoke_tool(
        "submit_leave",
        {
            "start_date": start_date,
            "end_date": end_date,
            "leave_type": leave_type,
            "days": days,
            "confirmed": confirmed,
        },
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def cancel_leave(
    request_id: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Cancels a leave request in WorkWeek and refunds days (SDD §3.6 user-confirmed compensating action)."""
    emp_id, sess_id, agent_name, acl = _resolve_context(tool_context, "workweek_agent")
    return acl.invoke_tool(
        "cancel_leave",
        {"request_id": request_id, "confirmed": confirmed},
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


# =============================================================================
# 3. ServiceImmediately (ITSM) Agent Tools (5 tools — SDD §3.1 & §5.2)
# =============================================================================
def get_ticket(
    ticket_id: str,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Retrieves ticket details, status, priority, assignee, and comments from ServiceImmediately."""
    emp_id, sess_id, agent_name, acl = _resolve_context(
        tool_context, "service_immediately_agent"
    )
    return acl.invoke_tool(
        "get_ticket",
        {"ticket_id": ticket_id},
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def list_tickets(
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Lists all ServiceImmediately tickets belonging to the authenticated employee."""
    emp_id, sess_id, agent_name, acl = _resolve_context(
        tool_context, "service_immediately_agent"
    )
    return acl.invoke_tool(
        "list_tickets",
        {},
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def create_incident(
    category: str,
    short_description: str,
    detailed_description: str = "",
    priority: str = "3 - Moderate",
    shipping_address: str = "",
    workflow_type: str = "",
    estimated_cost_usd: float = 0.0,
    relocation_amount_usd: float = 0.0,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Creates an incident ticket in ServiceImmediately after PDP dedupe, priority floor, and eligibility checks."""
    emp_id, sess_id, agent_name, acl = _resolve_context(
        tool_context, "service_immediately_agent"
    )
    return acl.invoke_tool(
        "create_incident",
        {
            "category": category,
            "short_description": short_description,
            "detailed_description": detailed_description or short_description,
            "priority": priority,
            "shipping_address": shipping_address,
            "workflow_type": workflow_type,
            "estimated_cost_usd": estimated_cost_usd,
            "relocation_amount_usd": relocation_amount_usd,
            "confirmed": confirmed,
        },
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def add_comment(
    ticket_id: str,
    comment: str,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Appends a comment to an existing ServiceImmediately ticket."""
    emp_id, sess_id, agent_name, acl = _resolve_context(
        tool_context, "service_immediately_agent"
    )
    return acl.invoke_tool(
        "add_comment",
        {"ticket_id": ticket_id, "comment": comment, "confirmed": confirmed},
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


def update_status(
    ticket_id: str,
    new_status: str,
    resolution_notes: str = "",
    user_asserted_resolution: bool = False,
    confirmed: bool = False,
    tool_context: Optional[ToolContext] = None,
) -> Dict[str, Any]:
    """Transitions ticket status following strict Handbook §5.5 sequential lifecycle (New -> In Progress -> Resolved -> Closed) and B-8."""
    emp_id, sess_id, agent_name, acl = _resolve_context(
        tool_context, "service_immediately_agent"
    )
    return acl.invoke_tool(
        "update_status",
        {
            "ticket_id": ticket_id,
            "new_status": new_status,
            "resolution_notes": resolution_notes,
            "user_asserted_resolution": user_asserted_resolution,
            "confirmed": confirmed,
        },
        authenticated_employee_id=emp_id,
        calling_agent=agent_name,
        session_id=sess_id,
    )


POLICY_AGENT_TOOLS = [search_policy]
WORKWEEK_AGENT_TOOLS = [
    get_profile,
    get_personal_info,
    get_leave_balance,
    get_leave_requests,
    update_contact,
    submit_leave,
    cancel_leave,
]
SERVICE_IMMEDIATELY_AGENT_TOOLS = [
    get_ticket,
    list_tickets,
    create_incident,
    add_comment,
    update_status,
]
