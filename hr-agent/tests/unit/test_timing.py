"""TimingPlugin (latency Phase 0) and per-agent thinking configuration."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest
from google.adk.models import LlmResponse
from google.genai import types

from app.observability import timing


def _resp(text: str | None, partial: bool | None = None) -> LlmResponse:
    parts = [types.Part.from_text(text=text)] if text else [types.Part.from_function_call(name="t", args={})]
    return LlmResponse(content=types.Content(role="model", parts=parts), partial=partial)


@pytest.mark.asyncio
async def test_turn_summary_has_llm_tool_and_screen_spans_without_content(monkeypatch):
    monkeypatch.setenv("HR_TIMING_LOG", "0")
    got: list[dict] = []
    timing.add_listener(got.append)
    try:
        plugin = timing.TimingPlugin()
        ic = NS(invocation_id="inv-t1")
        with timing.span("inv-t1", "input_screen"):  # runs before before_run in ADK
            pass
        await plugin.before_run_callback(invocation_context=ic)
        # specialist: tool call, then answer
        cb_spec = NS(invocation_id="inv-t1", agent_name="policy_agent")
        await plugin.before_model_callback(callback_context=cb_spec, llm_request=None)
        await plugin.after_model_callback(callback_context=cb_spec, llm_response=_resp(None))
        tc = NS(function_call_id="fc1", invocation_id="inv-t1", agent_name="policy_agent")
        tool = NS(name="search_policy")
        await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=tc)
        await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=tc, result={})
        # orchestrator: streamed answer
        cb_root = NS(invocation_id="inv-t1", agent_name="hr_agent")
        await plugin.before_model_callback(callback_context=cb_root, llm_request=None)
        await plugin.after_model_callback(callback_context=cb_root, llm_response=_resp("secret words", True))
        await plugin.after_model_callback(callback_context=cb_root, llm_response=_resp("secret words", False))
        with timing.span("inv-t1", "output_screen"):
            pass
        await plugin.after_run_callback(invocation_context=ic)
    finally:
        timing.remove_listener(got.append)

    assert len(got) == 1
    s = got[0]
    assert s["llm_calls"] == 2 and s["output_screen_calls"] == 1
    assert [x["kind"] for x in s["spans"]] == ["input_screen", "llm", "tool", "llm", "output_screen"]
    assert s["first_text_ms"] is not None
    root = next(x for x in s["spans"] if x.get("agent") == "hr_agent")
    assert root["chunks"] == 2
    assert "secret" not in str(s)  # durations only — never content
    assert "inv-t1" not in timing._turns  # turn state released


def test_thinking_config_per_agent():
    from app.agent import (
        _gen_config,
        itsm_agent,
        policy_agent,
        root_agent,
        workweek_agent,
    )

    assert _gen_config("DEFAULT") is None and _gen_config("") is None
    assert _gen_config("medium").thinking_config.thinking_level == types.ThinkingLevel.MEDIUM
    for a in (root_agent, policy_agent, workweek_agent, itsm_agent):
        assert a.generate_content_config.thinking_config.thinking_level == types.ThinkingLevel.LOW
