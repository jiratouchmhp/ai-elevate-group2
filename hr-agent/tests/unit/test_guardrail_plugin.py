"""GuardrailPlugin callbacks in isolation (SDD D5): output screening, manifest denials, prompt date.

The plugin is otherwise exercised end-to-end through the scripted-LLM integration tests;
these pin the refactored helpers directly with lightweight fakes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.models import LlmResponse
from google.genai import types

from app.guardrails.classifier import LocalHeuristicClassifier, Verdict
from app.guardrails.plugin import CANARY, LEAK_MESSAGE, REFUSAL, GuardrailPlugin


class FakeRemote:
    def __init__(self, block: bool) -> None:
        self.block = block
        self.calls: list[tuple[str, str]] = []

    def classify(self, text: str, direction: str = "input") -> Verdict:
        self.calls.append((text, direction))
        return Verdict(self.block, ["rai"] if self.block else [], "model_armor")


class FakeComposite:
    def __init__(self, remote: FakeRemote | None) -> None:
        self.local = LocalHeuristicClassifier()
        self.remote = remote

    def classify(self, text: str, direction: str = "input") -> Verdict:
        return self.local.classify(text, direction)


def _cb_ctx(agent: str = "hr_agent") -> SimpleNamespace:
    return SimpleNamespace(state={"hr_session_id": "hrs-test", "employee_id": "EMP001"}, agent_name=agent,
                           invocation_id="inv-test")


def _tool_ctx(agent: str) -> SimpleNamespace:
    return SimpleNamespace(state={"hr_session_id": "hrs-test", "employee_id": "EMP001"}, agent_name=agent,
                           session=SimpleNamespace(id="s1"))


def _resp(text: str, partial: bool | None = None) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
                       partial=partial)


def _out(resp: LlmResponse | None) -> str | None:
    return None if resp is None else resp.content.parts[0].text


@pytest.mark.asyncio
async def test_output_screen_uses_remote_classifier_when_present():
    remote = FakeRemote(block=True)
    plugin = GuardrailPlugin(classifier=FakeComposite(remote), mode="enforce")
    out = await plugin.after_model_callback(callback_context=_cb_ctx(), llm_response=_resp("some answer"))
    assert _out(out) == REFUSAL
    assert remote.calls == [("some answer", "output")]


@pytest.mark.asyncio
async def test_model_armor_skips_streamed_partials_and_specialist_drafts():
    """Latency: one Model Armor round trip per final user-facing response, not per chunk."""
    remote = FakeRemote(block=True)
    plugin = GuardrailPlugin(classifier=FakeComposite(remote), mode="enforce")
    # Streamed partial chunks from the orchestrator: no network screen.
    assert await plugin.after_model_callback(callback_context=_cb_ctx(), llm_response=_resp("par", True)) is None
    # Specialist drafts only reach the orchestrator, whose final output is screened.
    for agent in ("policy_agent", "workweek_agent", "itsm_agent"):
        assert await plugin.after_model_callback(callback_context=_cb_ctx(agent),
                                                 llm_response=_resp("draft")) is None
    assert remote.calls == []
    # The final orchestrator response is screened exactly once and a block still refuses.
    out = await plugin.after_model_callback(callback_context=_cb_ctx(), llm_response=_resp("final", False))
    assert _out(out) == REFUSAL
    assert remote.calls == [("final", "output")]


@pytest.mark.asyncio
async def test_local_checks_still_run_on_partials_and_keep_partial_flag():
    plugin = GuardrailPlugin(classifier=FakeComposite(FakeRemote(block=False)), mode="enforce")
    out = await plugin.after_model_callback(callback_context=_cb_ctx("policy_agent"),
                                            llm_response=_resp(f"x {CANARY}", True))
    assert _out(out) == LEAK_MESSAGE
    assert out.partial is True  # a rewritten chunk must not be mistaken for the final response


@pytest.mark.asyncio
async def test_output_passes_through_without_remote_or_findings():
    plugin = GuardrailPlugin(classifier=FakeComposite(None), mode="enforce")
    assert await plugin.after_model_callback(callback_context=_cb_ctx(), llm_response=_resp("fine")) is None


@pytest.mark.asyncio
async def test_canary_leak_replaced_and_inspect_mode_only_logs():
    enforce = GuardrailPlugin(classifier=FakeComposite(None), mode="enforce")
    out = await enforce.after_model_callback(callback_context=_cb_ctx(), llm_response=_resp(f"x {CANARY} y"))
    assert _out(out) == LEAK_MESSAGE
    inspect = GuardrailPlugin(classifier=FakeComposite(None), mode="inspect")
    assert await inspect.after_model_callback(callback_context=_cb_ctx(),
                                              llm_response=_resp(f"x {CANARY} y")) is None


@pytest.mark.asyncio
async def test_manifest_denial_enforced_and_shadowed():
    tool = SimpleNamespace(name="propose_leave")
    enforce = GuardrailPlugin(classifier=FakeComposite(None), mode="enforce")
    res = await enforce.before_tool_callback(tool=tool, tool_args={}, tool_context=_tool_ctx("policy_agent"))
    assert res["status"] == "blocked" and res["rule"]
    inspect = GuardrailPlugin(classifier=FakeComposite(None), mode="inspect")
    assert await inspect.before_tool_callback(tool=tool, tool_args={}, tool_context=_tool_ctx("policy_agent")) is None
    # In-manifest tools pass untouched.
    ok = SimpleNamespace(name="search_policy")
    assert await enforce.before_tool_callback(tool=ok, tool_args={}, tool_context=_tool_ctx("policy_agent")) is None


@pytest.mark.asyncio
async def test_unknown_tool_error_is_always_refused_even_in_inspect_mode():
    tool = SimpleNamespace(name="grant_admin")
    plugin = GuardrailPlugin(classifier=FakeComposite(None), mode="inspect")
    res = await plugin.on_tool_error_callback(tool=tool, tool_args={}, tool_context=_tool_ctx("itsm_agent"),
                                              error=ValueError("Tool grant_admin not found"))
    assert res["status"] == "blocked"
    other = await plugin.on_tool_error_callback(tool=tool, tool_args={}, tool_context=_tool_ctx("itsm_agent"),
                                                error=RuntimeError("boom"))
    assert other is None


def test_prompt_weekday_matches_session_date():
    from app.agent import _session_today

    assert _session_today({"today": "2026-12-22"}).strftime("%A") == "Tuesday"
    assert _session_today({"today": "not-a-date"}) is not None
    assert _session_today({}) is not None
