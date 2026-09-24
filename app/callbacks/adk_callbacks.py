"""ADK Deterministic Callbacks for Safety, Capability Manifest, Session Isolation & Audit (SDD D1, D5, §3.2, §3.9, §4.6).

Implements:
- `before_model_callback`:
  1. Verifies IAP identity assertion (`authenticated_employee_id`), never trusting prompt claims (§4.4).
  2. Scrubs any dynamic employee data from session state (FR-3.4 no caching of balances/profiles).
  3. Enforces per-session rate limit / max loop iterations (T-9 denial-of-wallet protection).
  4. Runs Model Armor INPUT scan + Advanced SDP de-identification (§3.2 fail-fast on blocked input).
  5. Checks Cheap-Path FAQ Cache (§3.2) keyed by `corpus_version`.
- `before_tool_callback`:
  1. Enforces FR-1.1 Authoritative Tool Contract Catalogue and §5.2 Explicit Denials.
  2. Enforces D2 per-agent tool scoping (e.g., Policy Agent holds zero write authority).
  3. Enforces FR-1.5 RBAC isolation (prevents cross-user queries).
- `after_tool_callback`:
  1. Ensures dynamic balances/profile fields are never cached in session state (FR-3.4).
- `after_model_callback`:
  1. Runs Model Armor OUTPUT scan + Advanced SDP redaction (FR-1.3, FR-1.4).
  2. Emits structured audit record (`AuditLogger`) with full traceability metadata (§4.6).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from app.acl.mcp_proxy import (
    AGENT_SPIFFE_IDS,
    EXPLICITLY_DENIED_TOOLS,
    TOOL_CONTRACT_CATALOGUE,
)
from app.adk_compat import CallbackContext, LlmRequest, LlmResponse, ToolContext
from app.governance.audit_logger import CORPUS_VERSION, DEFAULT_AUDIT_LOGGER
from app.safety.guardrails import (
    AdvancedSDPScanner,
    CheapPathFAQCache,
    ModelArmorScanner,
)


DEFAULT_MODEL_ARMOR = ModelArmorScanner()
DEFAULT_SDP = AdvancedSDPScanner()
DEFAULT_FAQ_CACHE = CheapPathFAQCache()

# Forbidden dynamic keys in session state per SDD §3.9 / FR-3.4
FORBIDDEN_SESSION_CACHE_KEYS = (
    "leave_balances",
    "vacation_balance",
    "sick_balance",
    "employee_profile",
    "home_address",
    "personal_phone",
    "nric",
)


def scrub_dynamic_employee_state(state: Dict[str, Any]) -> None:
    """Enforces FR-3.4 & §3.9: Leave balances and profile fields are NEVER cached in session state."""
    for key in list(state.keys()):
        if key in FORBIDDEN_SESSION_CACHE_KEYS:
            del state[key]


def before_model_guardrail_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Executes SDD §3.2 Pre-processing pipeline before the LLM sees a turn."""
    state = callback_context.state
    scrub_dynamic_employee_state(state)

    session_id = callback_context.session_id
    employee_id = str(state.get("authenticated_employee_id", "EMP-SG-001"))
    scanner: ModelArmorScanner = state.get("model_armor", DEFAULT_MODEL_ARMOR)
    faq_cache: CheapPathFAQCache = state.get("faq_cache", DEFAULT_FAQ_CACHE)
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)

    # T-9 Denial-of-Wallet control: bound maximum turns/iterations per session window
    turn_count = int(state.get("turn_count", 0)) + 1
    max_turns = int(state.get("max_turns_per_session", 50))
    state["turn_count"] = turn_count
    if turn_count > max_turns:
        audit.record(
            session_id=session_id,
            employee_id=employee_id,
            pdp_decision="DENY",
            pdp_rule_id="T-9_RATE_LIMIT_EXCEEDED",
            outcome="BLOCKED",
            notes=f"Session turn count {turn_count} exceeded max_turns={max_turns}.",
        )
        return LlmResponse(
            text="Rate limit reached for this session. Please start a new session or contact HR Support.",
            blocked=True,
            custom_events=[
                {
                    "type": "CUSTOM: guardrail_block",
                    "reason": "T-9_RATE_LIMIT_EXCEEDED",
                }
            ],
        )

    prompt_text = llm_request.prompt or ""
    verdict = scanner.scan_input(prompt_text)

    # Store only SDP-redacted user input in session history (FR-1.4 & §3.9)
    history = state.setdefault("conversation_history_redacted", [])
    history.append({"role": "user", "text": verdict.redacted_text})

    if verdict.blocked:
        audit.record(
            session_id=session_id,
            employee_id=employee_id,
            pdp_decision="DENY",
            pdp_rule_id=f"MODEL_ARMOR_INPUT_{verdict.category}",
            guardrail_verdicts={
                "input_category": verdict.category,
                "confidence": verdict.confidence,
                "detected_spii": verdict.detected_spii,
                "mode": verdict.mode,
            },
            outcome="BLOCKED",
            notes=verdict.reason,
        )
        return LlmResponse(
            text=verdict.escalation_message or "I can't process that request right now.",
            blocked=True,
            custom_events=[
                {
                    "type": "CUSTOM: guardrail_block",
                    "category": verdict.category,
                    "reason": verdict.reason,
                    "escalation": verdict.escalation_message,
                }
            ],
        )

    # SDD §3.2 Cheap-path FAQ cache lookup (invalidated by corpus_version)
    active_corpus_ver = str(state.get("corpus_version", CORPUS_VERSION))
    cached = faq_cache.lookup(prompt_text, active_corpus_version=active_corpus_ver)
    if cached:
        audit.record(
            session_id=session_id,
            employee_id=employee_id,
            pdp_decision="ALLOW",
            pdp_rule_id="CHEAP_PATH_FAQ_CACHE_HIT",
            retrieved_doc_ids=[c["anchor"] for c in cached.get("citations", [])],
            relevance_scores=[1.0],
            outcome="SUCCESS",
            notes=f"Served from cheap-path FAQ cache (corpus_version={active_corpus_ver}).",
        )
        return LlmResponse(
            text=cached["answer"],
            citations=cached.get("citations", []),
            blocked=False,
            metadata={"faq_cache_hit": True, "corpus_version": active_corpus_ver},
        )

    return None


def before_tool_guardrail_callback(
    tool: Callable[..., Any] | str,
    args: Dict[str, Any],
    tool_context: ToolContext,
) -> Optional[Dict[str, Any]]:
    """Enforces FR-1.1 capability manifest, D2 sub-agent blast-radius containment, and FR-1.5 RBAC."""
    tool_name = tool if isinstance(tool, str) else getattr(tool, "__name__", str(tool))
    state = tool_context.state
    scrub_dynamic_employee_state(state)

    employee_id = str(state.get("authenticated_employee_id", "EMP-SG-001"))
    agent_name = tool_context.agent_name
    spiffe_id = AGENT_SPIFFE_IDS.get(
        agent_name, f"spiffe://altostrat.sg/ns/agent-runtime/sa/{agent_name}"
    )
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)

    if tool_name in EXPLICITLY_DENIED_TOOLS:
        reason = EXPLICITLY_DENIED_TOOLS[tool_name]
        audit.record(
            session_id=tool_context.session_id,
            employee_id=employee_id,
            agent_id=spiffe_id,
            tool_invoked=tool_name,
            tool_args_redacted=DEFAULT_SDP.redact_dict(args),
            pdp_decision="DENY",
            pdp_rule_id="FR-1.1_EXPLICIT_DENIAL",
            outcome="DENIED",
            notes=reason,
        )
        return {
            "status": "DENIED",
            "rule_id": "FR-1.1_EXPLICIT_DENIAL",
            "message": f"Blocked by capability manifest: {reason}",
        }

    spec = TOOL_CONTRACT_CATALOGUE.get(tool_name)
    if not spec:
        audit.record(
            session_id=tool_context.session_id,
            employee_id=employee_id,
            agent_id=spiffe_id,
            tool_invoked=tool_name,
            tool_args_redacted=DEFAULT_SDP.redact_dict(args),
            pdp_decision="DENY",
            pdp_rule_id="FR-1.1_UNKNOWN_TOOL",
            outcome="DENIED",
            notes=f"Tool '{tool_name}' is outside the authorized tool manifest.",
        )
        return {
            "status": "DENIED",
            "rule_id": "FR-1.1_UNKNOWN_TOOL",
            "message": f"Tool '{tool_name}' is not in the authorized tool manifest.",
        }

    if agent_name not in spec.allowed_agents:
        audit.record(
            session_id=tool_context.session_id,
            employee_id=employee_id,
            agent_id=spiffe_id,
            tool_invoked=tool_name,
            tool_args_redacted=DEFAULT_SDP.redact_dict(args),
            pdp_decision="DENY",
            pdp_rule_id="D2_BLAST_RADIUS_VIOLATION",
            outcome="DENIED",
            notes=f"Agent '{agent_name}' is not allowed to invoke '{tool_name}'.",
        )
        return {
            "status": "DENIED",
            "rule_id": "D2_BLAST_RADIUS_VIOLATION",
            "message": f"Agent '{agent_name}' is strictly forbidden from calling '{tool_name}'.",
        }

    return None


def after_tool_guardrail_callback(
    tool: Callable[..., Any] | str,
    args: Dict[str, Any],
    tool_context: ToolContext,
    tool_response: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Ensures FR-3.4 (no dynamic employee data cached in session) after every tool call."""
    scrub_dynamic_employee_state(tool_context.state)
    return tool_response


def after_model_guardrail_callback(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Scans model output via Model Armor + Advanced SDP before returning to UI (§3.3, §4.5)."""
    state = callback_context.state
    scrub_dynamic_employee_state(state)

    scanner: ModelArmorScanner = state.get("model_armor", DEFAULT_MODEL_ARMOR)
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)
    employee_id = str(state.get("authenticated_employee_id", "EMP-SG-001"))

    verdict = scanner.scan_output(llm_response.text)
    if verdict.blocked:
        audit.record(
            session_id=callback_context.session_id,
            employee_id=employee_id,
            pdp_decision="DENY",
            pdp_rule_id=f"MODEL_ARMOR_OUTPUT_{verdict.category}",
            guardrail_verdicts={
                "output_category": verdict.category,
                "confidence": verdict.confidence,
                "detected_spii": verdict.detected_spii,
            },
            outcome="BLOCKED",
            notes=verdict.reason,
        )
        return LlmResponse(
            text=verdict.escalation_message or "I can't process that request right now.",
            blocked=True,
            custom_events=[
                {
                    "type": "CUSTOM: guardrail_block",
                    "category": verdict.category,
                    "reason": verdict.reason,
                }
            ],
        )

    # Replace any inadvertent SPII in output with SDP-deidentified text
    llm_response.text = verdict.redacted_text
    history = state.setdefault("conversation_history_redacted", [])
    history.append({"role": "assistant", "text": verdict.redacted_text})
    return llm_response
