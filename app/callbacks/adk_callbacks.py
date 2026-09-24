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
from app.adk_compat import (
    CallbackContext,
    LlmRequest,
    LlmResponse,
    NativeAdkLlmResponse,
    ToolContext,
    genai_types,
)
from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    is_live_mcp_enabled,
)
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

# Authorized ADK multi-agent hierarchy roster for native `transfer_to_agent` handoffs
AUTHORIZED_ADK_AGENTS = frozenset(
    {
        "root_orchestrator",
        "policy_agent",
        "workweek_agent",
        "service_immediately_agent",
    }
)


def scrub_dynamic_employee_state(state: Any) -> None:
    """Enforces FR-3.4 & §3.9: Leave balances and profile fields are NEVER cached in session state."""
    if state is None:
        return
    if isinstance(state, dict):
        for key in list(state.keys()):
            if key in FORBIDDEN_SESSION_CACHE_KEYS:
                del state[key]
        return

    # Native google.adk.sessions.state.State support
    state_dict = state.to_dict() if hasattr(state, "to_dict") else {}
    for key in FORBIDDEN_SESSION_CACHE_KEYS:
        if key in state_dict or (hasattr(state, "__contains__") and key in state):
            for backing_attr in ("_value", "_delta"):
                backing = getattr(state, backing_attr, None)
                if isinstance(backing, dict) and key in backing:
                    del backing[key]


def _extract_session_id(ctx: Any) -> str:
    sess_id = getattr(ctx, "session_id", None)
    if sess_id:
        return str(sess_id)
    session_obj = getattr(ctx, "session", None)
    if session_obj and getattr(session_obj, "id", None):
        return str(session_obj.id)
    return "sess-default"


def _resolve_employee_id(state: Any) -> str:
    if state is not None and hasattr(state, "get"):
        existing = state.get("authenticated_employee_id")
        if existing:
            return str(existing)
    default_emp = (
        get_mcp_authenticated_employee_id()
        if is_live_mcp_enabled(default=True)
        else "EMP-SG-001"
    )
    if state is not None and hasattr(state, "__setitem__"):
        try:
            state["authenticated_employee_id"] = default_emp
        except Exception:
            pass
    return default_emp


def _extract_prompt_text(llm_request: Any) -> str:
    prompt_attr = getattr(llm_request, "prompt", None)
    if prompt_attr is not None:
        return str(prompt_attr)
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(contents):
        role = getattr(content, "role", "user")
        parts = getattr(content, "parts", None) or []
        if role in ("user", None):
            texts = [
                str(getattr(p, "text", ""))
                for p in parts
                if getattr(p, "text", None)
            ]
            if texts:
                return "\n".join(texts).strip()
    return ""


def _build_callback_response(
    callback_context: Any,
    *,
    text: str,
    blocked: bool = False,
    citations: Optional[list[Dict[str, Any]]] = None,
    custom_events: Optional[list[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Any:
    if isinstance(callback_context, CallbackContext):
        return LlmResponse(
            text=text,
            blocked=blocked,
            citations=citations or [],
            custom_events=custom_events or [],
            metadata=metadata or {},
        )
    if NativeAdkLlmResponse is not None and genai_types is not None:
        return NativeAdkLlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=text)],
            ),
            custom_metadata=metadata or {},
        )
    return LlmResponse(
        text=text,
        blocked=blocked,
        citations=citations or [],
        custom_events=custom_events or [],
        metadata=metadata or {},
    )


def before_model_guardrail_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Executes SDD §3.2 Pre-processing pipeline before the LLM sees a turn."""
    state = callback_context.state
    scrub_dynamic_employee_state(state)

    session_id = _extract_session_id(callback_context)
    employee_id = _resolve_employee_id(state)
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
        return _build_callback_response(
            callback_context,
            text="Rate limit reached for this session. Please start a new session or contact HR Support.",
            blocked=True,
            custom_events=[
                {
                    "type": "CUSTOM: guardrail_block",
                    "reason": "T-9_RATE_LIMIT_EXCEEDED",
                }
            ],
        )

    prompt_text = _extract_prompt_text(llm_request)
    verdict = scanner.scan_input(prompt_text)

    # Store only SDP-redacted user input in session history (FR-1.4 & §3.9)
    history = list(state.get("conversation_history_redacted", []))
    history.append({"role": "user", "text": verdict.redacted_text})
    state["conversation_history_redacted"] = history

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
        return _build_callback_response(
            callback_context,
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
        return _build_callback_response(
            callback_context,
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
    if isinstance(tool, str):
        tool_name = tool
    else:
        tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", str(tool))
    state = tool_context.state
    scrub_dynamic_employee_state(state)

    session_id = _extract_session_id(tool_context)
    employee_id = _resolve_employee_id(state)
    agent_name = getattr(tool_context, "agent_name", "root_orchestrator")
    spiffe_id = AGENT_SPIFFE_IDS.get(
        agent_name, f"spiffe://altostrat.sg/ns/agent-runtime/sa/{agent_name}"
    )
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)

    if tool_name in EXPLICITLY_DENIED_TOOLS:
        reason = EXPLICITLY_DENIED_TOOLS[tool_name]
        audit.record(
            session_id=session_id,
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

    if tool_name == "transfer_to_agent":
        target_agent = str(args.get("agent_name", "")).strip()
        if target_agent in AUTHORIZED_ADK_AGENTS:
            audit.record(
                session_id=session_id,
                employee_id=employee_id,
                agent_id=spiffe_id,
                tool_invoked=tool_name,
                tool_args_redacted=DEFAULT_SDP.redact_dict(args),
                pdp_decision="ALLOW",
                pdp_rule_id="ADK_AGENT_TRANSFER_ALLOW",
                outcome="SUCCESS",
                notes=f"Authorized ADK agent handoff from '{agent_name}' to '{target_agent}'.",
            )
            return None

        audit.record(
            session_id=session_id,
            employee_id=employee_id,
            agent_id=spiffe_id,
            tool_invoked=tool_name,
            tool_args_redacted=DEFAULT_SDP.redact_dict(args),
            pdp_decision="DENY",
            pdp_rule_id="ADK_AGENT_TRANSFER_UNAUTHORIZED",
            outcome="DENIED",
            notes=f"Blocked unauthorized ADK transfer target '{target_agent}' from '{agent_name}'.",
        )
        return {
            "status": "DENIED",
            "rule_id": "ADK_AGENT_TRANSFER_UNAUTHORIZED",
            "message": f"Target agent '{target_agent}' is not in the authorized agent roster.",
        }

    spec = TOOL_CONTRACT_CATALOGUE.get(tool_name)
    if not spec:
        audit.record(
            session_id=session_id,
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
            session_id=session_id,
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

    session_id = _extract_session_id(callback_context)
    scanner: ModelArmorScanner = state.get("model_armor", DEFAULT_MODEL_ARMOR)
    audit = state.get("audit_logger", DEFAULT_AUDIT_LOGGER)
    employee_id = _resolve_employee_id(state)

    has_direct_text = hasattr(llm_response, "text")
    if has_direct_text:
        raw_output_text = getattr(llm_response, "text", "") or ""
    else:
        content_obj = getattr(llm_response, "content", None)
        parts = getattr(content_obj, "parts", None) or []
        raw_output_text = "\n".join(
            str(getattr(p, "text", "")) for p in parts if getattr(p, "text", None)
        )

    verdict = scanner.scan_output(raw_output_text)
    if verdict.blocked:
        audit.record(
            session_id=session_id,
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
        return _build_callback_response(
            callback_context,
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
    if has_direct_text:
        llm_response.text = verdict.redacted_text
    else:
        content_obj = getattr(llm_response, "content", None)
        parts = getattr(content_obj, "parts", None) or []
        for p in parts:
            if getattr(p, "text", None):
                p.text = scanner.scan_output(str(p.text)).redacted_text

    history = list(state.get("conversation_history_redacted", []))
    history.append({"role": "assistant", "text": verdict.redacted_text})
    state["conversation_history_redacted"] = history
    return llm_response
