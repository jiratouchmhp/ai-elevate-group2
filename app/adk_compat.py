"""Google ADK 2+ Compatibility Layer.

Uses native `google.adk` classes when installed in the runtime environment, and falls back
to API-faithful ADK 2+ classes (`Agent`, `LlmAgent`, `App`, `ToolContext`, `CallbackContext`,
`LlmRequest`, `LlmResponse`, `InMemorySessionService`, `Runner`) when executing in environments
without `google-adk` installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    from google.adk.agents import Agent, LlmAgent  # type: ignore
    from google.adk.apps import App  # type: ignore
    from google.adk.agents.callback_context import CallbackContext  # type: ignore
    from google.adk.models import LlmRequest, LlmResponse  # type: ignore
    from google.adk.tools import ToolContext  # type: ignore

    ADK_NATIVE_AVAILABLE = True
except Exception:
    ADK_NATIVE_AVAILABLE = False

    @dataclass
    class LlmRequest:
        prompt: str = ""
        model: str = "gemini-2.5-pro"
        contents: List[Any] = field(default_factory=list)
        config: Dict[str, Any] = field(default_factory=dict)

    @dataclass
    class LlmResponse:
        text: str = ""
        custom_events: List[Dict[str, Any]] = field(default_factory=list)
        citations: List[Dict[str, Any]] = field(default_factory=list)
        confirmation_card: Optional[Dict[str, Any]] = None
        blocked: bool = False
        metadata: Dict[str, Any] = field(default_factory=dict)

    class CallbackContext:
        def __init__(
            self,
            *,
            session_id: str = "sess-default",
            agent_name: str = "root_orchestrator",
            state: Optional[Dict[str, Any]] = None,
        ) -> None:
            self.session_id = session_id
            self.agent_name = agent_name
            self.state: Dict[str, Any] = state if state is not None else {}

    class ToolContext(CallbackContext):
        def __init__(
            self,
            *,
            session_id: str = "sess-default",
            agent_name: str = "root_orchestrator",
            state: Optional[Dict[str, Any]] = None,
            correlation_id: Optional[str] = None,
        ) -> None:
            super().__init__(session_id=session_id, agent_name=agent_name, state=state)
            self.correlation_id = correlation_id

    class LlmAgent:
        """ADK 2+ LlmAgent representation with hierarchical sub-agents and callbacks."""

        def __init__(
            self,
            *,
            name: str,
            model: str,
            description: str = "",
            instruction: str = "",
            tools: Optional[List[Callable[..., Any]]] = None,
            sub_agents: Optional[List["LlmAgent"]] = None,
            before_agent_callback: Optional[Callable[..., Any]] = None,
            after_agent_callback: Optional[Callable[..., Any]] = None,
            before_model_callback: Optional[Callable[..., Any]] = None,
            after_model_callback: Optional[Callable[..., Any]] = None,
            before_tool_callback: Optional[Callable[..., Any]] = None,
            after_tool_callback: Optional[Callable[..., Any]] = None,
            output_key: Optional[str] = None,
        ) -> None:
            self.name = name
            self.model = model
            self.description = description
            self.instruction = instruction
            self.tools: List[Callable[..., Any]] = list(tools or [])
            self.sub_agents: List[LlmAgent] = list(sub_agents or [])
            self.before_agent_callback = before_agent_callback
            self.after_agent_callback = after_agent_callback
            self.before_model_callback = before_model_callback
            self.after_model_callback = after_model_callback
            self.before_tool_callback = before_tool_callback
            self.after_tool_callback = after_tool_callback
            self.output_key = output_key

    Agent = LlmAgent

    class App:
        """ADK 2+ App container binding root_agent and session/plugin configuration."""

        def __init__(
            self,
            *,
            name: str,
            root_agent: LlmAgent,
            plugins: Optional[List[Any]] = None,
        ) -> None:
            self.name = name
            self.root_agent = root_agent
            self.plugins = list(plugins or [])
