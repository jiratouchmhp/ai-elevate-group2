# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Altostrat HR Agent — MVP-1 multi-agent topology (SDD §3.1).

    hr_agent (orchestrator: route + compose, no backend tools)
      ├── policy_agent    (single_turn) search_policy — grounded, cited answers
      ├── workweek_agent  (single_turn) HR system-of-record reads + propose/commit
      └── itsm_agent      (single_turn) ServiceImmediately reads + propose/commit

All authority (identity, PDP, confirmation, idempotency, saga, audit) lives in the
ACL behind the tools; guardrails run runner-wide via `GuardrailPlugin`.
"""

import uuid
from datetime import date
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app import clock, config
from app.guardrails.plugin import CANARY, GuardrailPlugin
from app.observability.timing import TimingPlugin
from app.tools import ITSM_TOOLS, PENDING_KEY, POLICY_TOOLS, WORKWEEK_TOOLS

PROMPTS = Path(__file__).parent / "prompts"


def _model(name: str) -> Gemini:
    return Gemini(model=name, retry_options=types.HttpRetryOptions(attempts=3))


def _gen_config(thinking_level: str) -> types.GenerateContentConfig | None:
    """Per-agent thinking depth (config.*_THINKING); DEFAULT/empty keeps the model default."""
    level = (thinking_level or "").strip().upper()
    if not level or level == "DEFAULT":
        return None
    return types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level=level))


def _pending_summary(state) -> str:
    pending = state.get(PENDING_KEY) or {}
    if not pending:
        return "(none)"
    lines = []
    for pid, p in pending.items():
        args = {k: v for k, v in (p.get("proposed") or {}).items() if k not in ("ship_to",)}
        lines.append(f"- proposal_id={pid} | specialist={p.get('agent')} | action={p.get('action')} | {args}")
    return "\n".join(lines)


def _session_today(state) -> date:
    """The session's pinned date (set by init_session / eval), else today in Asia/Singapore."""
    try:
        return date.fromisoformat(state.get("today") or "")
    except (TypeError, ValueError):
        return clock.today()


def _instruction(prompt_file: str):
    template = (PROMPTS / prompt_file).read_text(encoding="utf-8")

    def provider(ctx: ReadonlyContext) -> str:
        today = _session_today(ctx.state)
        text = (template.replace("{today}", f"{today.isoformat()} ({today.strftime('%A')})")
                .replace("{escalation}", config.HR_ESCALATION_CHANNEL)
                .replace("{canary}", CANARY))
        if "{pending}" in text:
            text = text.replace("{pending}", _pending_summary(ctx.state))
        return text

    return provider


async def init_session(callback_context: CallbackContext) -> None:
    """Bind the verified identity + session metadata into state (SDD §4.4).

    In production the employee_id comes from the IAP-verified token; locally it falls
    back to DEMO_EMPLOYEE_ID; in eval the trace generator pre-seeds it per persona.
    """
    st = callback_context.state
    if not st.get("employee_id"):
        st["employee_id"] = config.DEMO_EMPLOYEE_ID
    if not st.get("hr_session_id"):
        st["hr_session_id"] = f"hrs-{uuid.uuid4().hex[:12]}"
    st["today"] = clock.today().isoformat()
    return None


policy_agent = Agent(
    name="policy_agent",
    mode="single_turn",
    description="Answers HR policy questions strictly from the Altostrat Singapore Employee Policy Handbook, "
                "with section citations. Input: the full policy question.",
    model=_model(config.POLICY_MODEL),
    generate_content_config=_gen_config(config.POLICY_THINKING),
    instruction=_instruction("policy_agent.md"),
    tools=POLICY_TOOLS,
)

workweek_agent = Agent(
    name="workweek_agent",
    mode="single_turn",
    description="Handles the signed-in employee's WorkWeek HR record: profile, contact details, leave balances "
                "and requests; proposes leave submission/cancellation and contact updates, and commits a "
                "proposal only after the user explicitly confirmed it (pass the proposal_id).",
    model=_model(config.WORKWEEK_MODEL),
    generate_content_config=_gen_config(config.WORKWEEK_THINKING),
    instruction=_instruction("workweek_agent.md"),
    tools=WORKWEEK_TOOLS,
)

itsm_agent = Agent(
    name="itsm_agent",
    mode="single_turn",
    description="Handles the signed-in employee's ServiceImmediately tickets: list/view, propose new tickets "
                "(incl. equipment allowance, relocation/badge, email delegation), comments and status moves; "
                "commits a proposal only after the user explicitly confirmed it (pass the proposal_id).",
    model=_model(config.ITSM_MODEL),
    generate_content_config=_gen_config(config.ITSM_THINKING),
    instruction=_instruction("itsm_agent.md"),
    tools=ITSM_TOOLS,
)

root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml: agents-cli derives this name
    # from the project `name:` recorded there, and telemetry reports it as
    # gen_ai.agent.name. Renaming the agent only here makes the two disagree,
    # and anything selecting traces by name stops finding this agent's.
    name=config.ROOT_AGENT_NAME,
    model=_model(config.ORCHESTRATOR_MODEL),
    generate_content_config=_gen_config(config.ORCHESTRATOR_THINKING),
    description="Altostrat HR assistant orchestrator.",
    instruction=_instruction("orchestrator.md"),
    sub_agents=[policy_agent, workweek_agent, itsm_agent],
    before_agent_callback=init_session,
)

app = App(
    root_agent=root_agent,
    name="app",
    # TimingPlugin first so its before_* hooks start the clock before any guardrail work.
    plugins=[TimingPlugin(), GuardrailPlugin()],
)
