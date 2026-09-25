"""Per-turn latency breakdown (latency optimisation, Phase 0).

`TimingPlugin` records how long each LLM call, tool call and guardrail screen takes and
emits one structured `turn_timing` line per invocation (stdout -> Cloud Logging). Only
durations, agent names and tool names are logged — never prompt/response content — so
this adds no PII to the logs.

Enable/disable with `HR_TIMING_LOG` (default on). Tests and the latency bench can
subscribe with `add_listener` to receive the summary dicts directly.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app import config

log = logging.getLogger("hr.latency")
if not log.handlers:  # uvicorn leaves the root logger at WARNING; give this one its own sink
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

_SPECIALISTS = frozenset({"policy_agent", "workweek_agent", "itsm_agent"})
_MAX_OPEN = 500  # bound memory if a run dies without after_run_callback
_listeners: list[Callable[[dict], None]] = []
# invocation_id -> span list; shared with `span()` so guardrail screens land in the same turn
_turns: dict[str, dict[str, Any]] = {}


def add_listener(fn: Callable[[dict], None]) -> None:
    _listeners.append(fn)


def remove_listener(fn: Callable[[dict], None]) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def _enabled() -> bool:
    return os.getenv("HR_TIMING_LOG", "1") != "0"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _turn(invocation_id: str, started: float | None = None) -> dict[str, Any]:
    """The turn record, created on first use (the input screen runs before `before_run`)."""
    turn = _turns.get(invocation_id)
    if turn is None:
        if len(_turns) > _MAX_OPEN:
            for k in list(_turns)[: _MAX_OPEN // 2]:
                _turns.pop(k, None)
        turn = _turns[invocation_id] = {"t0": started or time.perf_counter(), "first_text_ms": None, "spans": []}
    return turn


def _record(invocation_id: str | None, span: dict, started: float | None = None) -> None:
    if invocation_id:
        _turn(invocation_id, started)["spans"].append(span)


@contextmanager
def span(invocation_id: str | None, kind: str, **fields: Any):
    """Time an arbitrary block (e.g. a Model Armor call) into the current turn."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _record(invocation_id, {"kind": kind, "ms": _ms(t0), **fields}, started=t0)


class TimingPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(name="hr_timing")
        self._llm_open: dict[tuple[str, str], dict] = {}
        self._tool_open: dict[str, float] = {}

    # ------------------------------------------------------------------ turn
    async def before_run_callback(self, *, invocation_context: InvocationContext) -> None:
        _turn(invocation_context.invocation_id)  # keeps t0 if the input screen already opened it
        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        turn = _turns.pop(invocation_context.invocation_id, None)
        if turn is None:
            return None
        spans = turn["spans"]
        llm = [s for s in spans if s["kind"] == "llm"]
        summary = {
            "event": "turn_timing",
            "invocation_id": invocation_context.invocation_id,
            "total_ms": _ms(turn["t0"]),
            "first_text_ms": turn["first_text_ms"],
            "llm_calls": len(llm),
            "llm_ms": sum(s["ms"] for s in llm),
            "tool_ms": sum(s["ms"] for s in spans if s["kind"] == "tool"),
            "screen_ms": sum(s["ms"] for s in spans if s["kind"].endswith("_screen")),
            "output_screen_calls": sum(1 for s in spans if s["kind"] == "output_screen"),
            "spans": spans,
        }
        if _enabled():
            log.info(json.dumps(summary, default=str))
        for fn in list(_listeners):
            try:
                fn(summary)
            except Exception:  # a listener must never break the run
                pass
        return None

    # ------------------------------------------------------------------ LLM
    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        key = (callback_context.invocation_id, callback_context.agent_name)
        if len(self._llm_open) > _MAX_OPEN:
            self._llm_open.clear()
        self._llm_open[key] = {"t0": time.perf_counter(), "first_ms": None, "chunks": 0}
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        inv, agent = callback_context.invocation_id, callback_context.agent_name
        rec = self._llm_open.get((inv, agent))
        if rec is None:
            return None
        rec["chunks"] += 1
        if rec["first_ms"] is None:
            rec["first_ms"] = _ms(rec["t0"])
        has_text = bool(llm_response.content and any(getattr(p, "text", None) and not getattr(p, "thought", False)
                                                     for p in (llm_response.content.parts or [])))
        turn = _turns.get(inv)
        if has_text and turn is not None and turn["first_text_ms"] is None and agent == config.ROOT_AGENT_NAME:
            turn["first_text_ms"] = _ms(turn["t0"])
        if llm_response.partial:
            return None
        self._llm_open.pop((inv, agent), None)
        usage = llm_response.usage_metadata
        _record(inv, {
            "kind": "llm", "agent": agent, "ms": _ms(rec["t0"]), "first_chunk_ms": rec["first_ms"],
            "chunks": rec["chunks"],
            "thought_tokens": getattr(usage, "thoughts_token_count", None) if usage else None,
            "output_tokens": getattr(usage, "candidates_token_count", None) if usage else None,
        })
        return None

    # ------------------------------------------------------------------ tools
    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict | None:
        if len(self._tool_open) > _MAX_OPEN:
            self._tool_open.clear()
        self._tool_open[tool_context.function_call_id or tool.name] = time.perf_counter()
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict
    ) -> dict | None:
        t0 = self._tool_open.pop(tool_context.function_call_id or tool.name, None)
        if t0 is not None:
            # Specialists run as tools (mode="single_turn"); their span wraps nested LLM + tool
            # spans, so keep it separate from backend/retrieval tool time to avoid double counting.
            kind = "subagent" if tool.name in _SPECIALISTS else "tool"
            _record(tool_context.invocation_id, {"kind": kind, "agent": tool_context.agent_name,
                                                 "tool": tool.name, "ms": _ms(t0)})
        return None

