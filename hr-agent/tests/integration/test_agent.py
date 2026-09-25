"""Live-model smoke test (needs ADC + Vertex). Skipped unless HR_LIVE_TESTS=1."""

import os

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

pytestmark = pytest.mark.skipif(os.getenv("HR_LIVE_TESTS") != "1", reason="live model test; set HR_LIVE_TESTS=1")


def _run(text: str) -> str:
    from app.agent import app as adk_app

    svc = InMemorySessionService()
    session = svc.create_session_sync(user_id="EMP001", app_name=adk_app.name)
    runner = Runner(app=adk_app, session_service=svc)
    out = []
    for ev in runner.run(user_id="EMP001", session_id=session.id,
                         new_message=types.Content(role="user", parts=[types.Part.from_text(text=text)])):
        if ev.content and ev.content.parts:
            out += [p.text for p in ev.content.parts if p.text]
    return "\n".join(out)


def test_policy_question_is_cited():
    reply = _run("How many days of annual leave do I get in my first year?")
    assert "§" in reply or "Section" in reply


def test_balance_lookup():
    reply = _run("What's my vacation balance?")
    assert "5" in reply
