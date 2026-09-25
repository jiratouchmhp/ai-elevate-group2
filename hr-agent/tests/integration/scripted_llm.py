"""Deterministic scripted LLM for offline integration tests of the agent plumbing."""

from __future__ import annotations

from typing import AsyncGenerator, Callable

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

# script(agent_hint, llm_request) -> list[Part] ; agent hint derived from system instruction
Script = Callable[[str, LlmRequest], list[types.Part]]


def fc(name: str, **args) -> types.Part:
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def txt(t: str) -> types.Part:
    return types.Part.from_text(text=t)


def last_function_response(req: LlmRequest) -> dict | None:
    for c in reversed(req.contents or []):
        for p in c.parts or []:
            if p.function_response:
                return {"name": p.function_response.name, "response": p.function_response.response}
        if c.role == "user" and any(p.text for p in c.parts or []):
            return None
    return None


def last_user_text(req: LlmRequest) -> str:
    for c in reversed(req.contents or []):
        if c.role == "user":
            t = " ".join(p.text for p in c.parts or [] if p.text)
            if t:
                return t
    return ""


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: Script | None = None
    calls: list = []

    model_config = {"arbitrary_types_allowed": True}

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        si = str(llm_request.config.system_instruction or "")
        hint = ("policy" if "Policy Agent" in si else "workweek" if "WorkWeek Agent" in si
                else "itsm" if "ITSM Agent" in si else "orchestrator")
        tools = sorted((llm_request.tools_dict or {}).keys())
        self.calls.append({"agent": hint, "tools": tools})
        parts = self.script(hint, llm_request)
        yield LlmResponse(content=types.Content(role="model", parts=parts))
