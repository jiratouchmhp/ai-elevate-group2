"""Runner-wide guardrail plugin (SDD §4.2 defence-in-depth, D5).

Layers enforced here, outside any prompt:
  1. Input screening (prompt injection / jailbreak / exfiltration / unsafe) — blocks the
     turn BEFORE any model call and returns a refusal with an escalation route.
  2. SPII redaction of the user message (FR-1.4) and of every model response.
  3. Capability manifest at `before_tool_callback` — out-of-manifest or named-denied
     tools are blocked and audited regardless of which agent the model routed to.
  4. System-prompt leakage canary + optional Model Armor output screening. Model Armor is a
     network call, so it screens each FINAL user-facing (orchestrator) response once — not
     every streamed chunk and not specialist drafts, which only ever reach the orchestrator.
     The canary and SPII redaction are local and still run on every chunk.
  5. Denial-of-wallet cap on LLM calls per invocation (§6.5, T-9).

`GUARDRAIL_MODE=inspect` runs every check in shadow mode (log only) for tuning.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app import config
from app.guardrails import manifest, spii
from app.guardrails.classifier import CompositeClassifier, Verdict
from app.integration import audit
from app.observability import timing

CANARY = "ALTOSTRAT-PROMPT-CANARY-7F3A"  # embedded in every system prompt; must never be emitted

REFUSAL = (
    "I can't help with that request. I'm the Altostrat HR assistant and can only help with HR policy "
    "questions, your leave and contact details, and service-desk tickets — within company policy. "
    f"If you need something else, please contact {config.HR_ESCALATION_CHANNEL}."
)
WELLBEING = (
    "I'm really sorry you're going through this. You don't have to handle it alone — please reach out to "
    "someone you trust, your manager or HR, or the Employee Assistance Programme. If you're in immediate "
    "danger, call 995 (emergency) or the Samaritans of Singapore 24-hour hotline at 1767."
)
CAP_MESSAGE = ("This request needed more steps than I'm allowed to take in one turn, so I stopped to be safe. "
               "Could you break it into smaller requests?")
LEAK_MESSAGE = "I can't share details about my internal configuration. How can I help with your HR request?"


def _text(content: types.Content | None) -> str:
    if not content or not content.parts:
        return ""
    return "\n".join(p.text for p in content.parts if getattr(p, "text", None))


def _msg(text: str, role: str = "model") -> types.Content:
    return types.Content(role=role, parts=[types.Part.from_text(text=text)])


class GuardrailPlugin(BasePlugin):
    def __init__(self, classifier: Any | None = None, mode: str | None = None) -> None:
        super().__init__(name="hr_guardrails")
        self.classifier = classifier or CompositeClassifier()
        self.mode = (mode or os.getenv("GUARDRAIL_MODE", "enforce")).lower()
        self._blocked: dict[str, Verdict] = {}
        self._llm_calls: dict[str, int] = {}

    # ----------------------------------------------------------------- helpers
    @property
    def enforcing(self) -> bool:
        return self.mode != "inspect"

    @staticmethod
    def _ids(ic: InvocationContext) -> tuple[str, str]:
        st = ic.session.state
        return (st.get("hr_session_id") or ic.session.id,
                st.get("employee_id") or config.DEMO_EMPLOYEE_ID)

    # ----------------------------------------------------------------- 1+2: input
    async def on_user_message_callback(
        self, *, invocation_context: InvocationContext, user_message: types.Content
    ) -> types.Content | None:
        text = _text(user_message)
        sid, emp = self._ids(invocation_context)
        # 1) De-identify first (FR-1.4): SPII is redacted, not a reason to refuse the request,
        #    and the raw identifier never reaches Model Armor, the model, or the logs.
        redacted, kinds = spii.redact(text, phones=False)
        if kinds:
            audit.emit(event="spii_redaction", session_id=sid, employee_id=emp, agent_id="guardrail_plugin",
                       tool_invoked="input_redaction", guardrail_verdicts=[{"redacted": kinds}], outcome="REDACTED")
        # 2) Screen the (redacted) text for injection / jailbreak / unsafe content. Model Armor's
        #    client is synchronous, so keep it off the shared event loop.
        with timing.span(invocation_context.invocation_id, "input_screen"):
            verdict = await asyncio.to_thread(self.classifier.classify, redacted, "input")
        if verdict.blocked:
            audit.emit(event="guardrail_block" if self.enforcing else "guardrail_inspect", session_id=sid,
                       employee_id=emp, agent_id="guardrail_plugin", tool_invoked="input_screen",
                       tool_args={"prompt": redacted[:500]}, guardrail_verdicts=[verdict.to_dict()],
                       outcome="BLOCKED" if self.enforcing else "SHADOW_FLAGGED")
            if self.enforcing:
                self._blocked[invocation_context.invocation_id] = verdict
                return _msg("[message withheld by input guardrail]", role="user")
        if kinds:
            return _msg(redacted + "\n[Note: a sensitive identifier was removed from this message for your "
                                   "protection. It is not needed for any HR request.]", role="user")
        return None

    async def before_run_callback(self, *, invocation_context: InvocationContext) -> types.Content | None:
        verdict = self._blocked.pop(invocation_context.invocation_id, None)
        if verdict is None:
            return None
        if "self_harm" in verdict.categories:
            return _msg(WELLBEING)
        return _msg(REFUSAL)

    # ----------------------------------------------------------------- 5: DoW cap
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        inv = callback_context.invocation_id
        n = self._llm_calls.get(inv, 0) + 1
        self._llm_calls[inv] = n
        if len(self._llm_calls) > 500:  # bound memory
            for k in list(self._llm_calls)[:250]:
                self._llm_calls.pop(k, None)
        if n > config.MAX_LLM_CALLS_PER_TURN:
            st = callback_context.state
            audit.emit(event="guardrail_block", session_id=st.get("hr_session_id"), employee_id=st.get("employee_id"),
                       agent_id=callback_context.agent_name, tool_invoked="llm_call_cap",
                       guardrail_verdicts=[{"llm_calls": n, "cap": config.MAX_LLM_CALLS_PER_TURN}],
                       outcome="BLOCKED")
            if self.enforcing:
                return LlmResponse(content=_msg(CAP_MESSAGE))
        return None

    # ----------------------------------------------------------------- 2+4: output
    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        content = llm_response.content
        if not content or not content.parts or not any(getattr(p, "text", None) for p in content.parts):
            return None
        st = callback_context.state
        # Output screening is Model Armor only; the local heuristics are input-shaped.
        remote = getattr(self.classifier, "remote", None)
        # One Model Armor round trip per FINAL user-facing response (see module docstring).
        # Streamed partials already shown are superseded in the UI by the screened final
        # (AguiTranslator emits `message_replace` when the final text differs).
        if remote is not None and (llm_response.partial or callback_context.agent_name != config.ROOT_AGENT_NAME):
            remote = None
        changed, verdicts = False, []
        new_parts = []
        for p in content.parts:
            if getattr(p, "text", None) and not getattr(p, "thought", False):
                t = p.text
                if CANARY in t:
                    t, changed = LEAK_MESSAGE, True
                    verdicts.append({"system_prompt_leak": True})
                red, kinds = spii.redact(t, phones=False)
                if kinds:
                    t, changed = red, True
                    verdicts.append({"redacted": kinds})
                if remote is not None:
                    with timing.span(callback_context.invocation_id, "output_screen"):
                        v = await asyncio.to_thread(remote.classify, t, "output")
                    if v.blocked:
                        t, changed = REFUSAL, True
                        verdicts.append(v.to_dict())
                new_parts.append(types.Part.from_text(text=t))
            else:
                new_parts.append(p)
        if not changed:
            return None
        audit.emit(event="output_guardrail", session_id=st.get("hr_session_id"), employee_id=st.get("employee_id"),
                   agent_id=callback_context.agent_name, tool_invoked="output_screen",
                   guardrail_verdicts=verdicts, outcome="MODIFIED" if self.enforcing else "SHADOW_FLAGGED")
        if not self.enforcing:
            return None
        return llm_response.model_copy(update={
            "content": types.Content(role=content.role or "model", parts=new_parts)})

    # ----------------------------------------------------------------- 3: manifest
    def _deny_tool(self, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
                   reason: str, *, enforce: bool) -> dict | None:
        """Audit a manifest denial; return the structured refusal when enforcing."""
        rule = reason.split(":")[0]
        st = tool_context.state
        audit.emit(event="manifest_denial", session_id=st.get("hr_session_id") or tool_context.session.id,
                   employee_id=st.get("employee_id"), agent_id=tool_context.agent_name, tool_invoked=tool.name,
                   tool_args=tool_args, pdp_decision="DENY", pdp_rule_ids=[rule],
                   outcome="BLOCKED" if enforce else "SHADOW_FLAGGED")
        if not enforce:
            return None
        return {"status": "blocked", "reason": "This capability is not available to this assistant.", "rule": rule}

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict | None:
        ok, reason = manifest.check(tool_context.agent_name, tool.name)
        if ok:
            return None
        return self._deny_tool(tool, tool_args, tool_context, reason, enforce=self.enforcing)

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception
    ) -> dict | None:
        """Tools the agent does not hold never reach before_tool_callback (ADK rejects them
        first). Treat them as manifest denials: audit + structured refusal, never a crash."""
        if not isinstance(error, ValueError) or "not found" not in str(error):
            return None
        _, reason = manifest.check(tool_context.agent_name, tool.name)
        # The tool does not exist for this agent, so there is nothing to shadow: always refuse.
        return self._deny_tool(tool, tool_args, tool_context, reason, enforce=True)
