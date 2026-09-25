"""Unit tests for LLM-First Agent & Tool Routing and Natural Language Phrasing in HRMultiAgentRuntime."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.agent import HRMultiAgentRuntime


class TestLLMRouterAndNaturalPrompts(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["USE_LIVE_MCP"] = "false"
        os.environ["USE_FIRESTORE"] = "false"
        os.environ["USE_CLOUD_RAG"] = "false"
        self.runtime = HRMultiAgentRuntime()

    def test_llm_brain_routes_freeform_balance_query_to_workweek_agent(self) -> None:
        """When Gemini Agent Brain returns intent='get_leave_balance', run_turn routes directly to workweek_agent."""
        with patch.object(
            self.runtime,
            "_query_gemini_agent_brain",
            return_value={"agent": "workweek_agent", "intent": "get_leave_balance"},
        ):
            res = self.runtime.run_turn("could you check how many days I still have in my account?")
            self.assertIn("workweek_agent", res.delegated_agents)
            self.assertIn("get_leave_balance", res.tool_trajectory)
            self.assertNotIn("policy_agent", res.delegated_agents)
            self.assertNotIn("search_policy", res.tool_trajectory)

    def test_llm_brain_routes_freeform_leave_request_to_workweek_agent(self) -> None:
        """When Gemini Agent Brain returns intent='submit_leave', run_turn routes directly to workweek_agent."""
        with patch.object(
            self.runtime,
            "_query_gemini_agent_brain",
            return_value={
                "agent": "workweek_agent",
                "intent": "submit_leave",
                "leave_type": "Vacation",
                "start_date": "2026-10-20",
                "end_date": "2026-10-21",
                "days": 2.0,
            },
        ):
            res = self.runtime.run_turn("I would like to ask for leave on Oct 20 and Oct 21")
            self.assertIn("workweek_agent", res.delegated_agents)
            self.assertIn("submit_leave", res.tool_trajectory)
            self.assertNotIn("policy_agent", res.delegated_agents)
            self.assertIsNotNone(res.confirmation_card)

    def test_natural_balance_days_phrases_route_to_workweek_without_rag(self) -> None:
        """Natural phrasings like 'retrieve my balance days' route to get_leave_balance even if LLM brain is offline."""
        prompts = [
            "retrieve my balance days",
            "what are my balance days?",
            "check my balance",
            "how many leave days do I have left?",
            "show my vacation balance",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                res = self.runtime.run_turn(prompt)
                self.assertIn("workweek_agent", res.delegated_agents)
                self.assertIn("get_leave_balance", res.tool_trajectory)
                self.assertNotIn("search_policy", res.tool_trajectory)

    def test_natural_ask_for_leave_phrases_route_to_workweek_without_rag(self) -> None:
        """Natural phrasings like 'ask for leave' route to submit_leave instead of falling through to RAG."""
        prompts = [
            "ask for leave",
            "I want to ask for leave",
            "I need 2 days of leave from 2026-10-15 to 2026-10-16",
            "can I have a day off tomorrow",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                res = self.runtime.run_turn(prompt, session_id=f"sess-{hash(prompt)}")
                self.assertIn("workweek_agent", res.delegated_agents)
                self.assertIn("submit_leave", res.tool_trajectory)
                self.assertNotIn("search_policy", res.tool_trajectory)
                self.assertEqual(res.selected_intent, "submit_leave")

    def test_selected_intent_and_source_in_turn_result_and_ui_bff(self) -> None:
        """TurnResult, handle_chat_payload, and App.tsx expose selected_intent and intent_source."""
        from pathlib import Path
        from app.ui.ag_ui_server import handle_chat_payload

        with patch.object(
            self.runtime,
            "_query_gemini_agent_brain",
            return_value={"agent": "workweek_agent", "intent": "get_leave_balance"},
        ):
            res = self.runtime.run_turn("retrieve my balance days")
            self.assertEqual(res.selected_intent, "get_leave_balance")
            self.assertEqual(res.intent_source, "gemini_llm_router")

        payload = handle_chat_payload({"prompt": "retrieve my balance days", "session_id": "sess-intent-ui"}, {})
        self.assertEqual(payload["selected_intent"], "get_leave_balance")
        self.assertIn(payload["intent_source"], ("gemini_llm_router", "pattern_fallback"))

        app_tsx = (Path(__file__).resolve().parents[2] / "app" / "ui" / "frontend" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("intent-pill", app_tsx)
        self.assertIn("🎯 Intent:", app_tsx)
        self.assertIn("3. INTENT ROUTER", app_tsx)


if __name__ == "__main__":
    unittest.main()
