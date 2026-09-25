"""Offline test of the eval harness: generate_traces.run_case (scripted LLM) -> hr_checks.

Proves the trace format, side-effect capture and deterministic metrics agree on a
known-good and a known-bad trajectory, without calling Gemini.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app import agent as agent_mod
from tests.integration.scripted_llm import ScriptedLlm, fc, last_function_response, last_user_text, txt

EVAL = Path(__file__).resolve().parents[1] / "eval"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gen = _load("generate_traces", EVAL / "generate_traces.py")
checks = _load("hr_checks", EVAL / "metrics" / "hr_checks.py")

CASE = {
    "eval_case_id": "harness-leave",
    "category": "txn.leave",
    "persona": "EMP001",
    "agent_data": {"turns": [
        {"turn_index": 0, "events": [{"author": "user", "content": {"role": "user", "parts": [{"text": "Book Nov 2-3 vacation"}]}}]},
        {"turn_index": 1, "events": [{"author": "user", "content": {"role": "user", "parts": [{"text": "yes please"}]}}]},
    ]},
    "expected": {
        "outcome": "committed", "route": ["workweek_agent"], "vendor_writes": {"submit_leave": 1},
        "writes_after_turn": 1, "must_include_any": [["LR-"]],
        "turns": [{"tools_all": ["propose_leave"], "tools_none": ["commit_action"]}, {"tools_all": ["commit_action"]}],
    },
}


@pytest.fixture()
def runner(monkeypatch):
    llm = ScriptedLlm()
    llm.calls = []
    for a in (agent_mod.root_agent, agent_mod.policy_agent, agent_mod.workweek_agent, agent_mod.itsm_agent):
        monkeypatch.setattr(a, "model", llm)
    return Runner(app=agent_mod.app, session_service=InMemorySessionService()), llm


def _good_script(state):
    def script(agent, req):
        fr, user = last_function_response(req), last_user_text(req)
        if agent == "orchestrator":
            if fr is None:
                if "yes" in user.lower():
                    return [fc("workweek_agent", request=f"User confirmed. commit_action proposal_id={state['pid']}")]
                return [fc("workweek_agent", request="Book vacation 2026-11-02 to 2026-11-03")]
            return [txt(f"Done: {fr['response']}")]
        if agent == "workweek":
            if fr is None:
                if "commit_action" in user:
                    return [fc("commit_action", proposal_id=state["pid"])]
                return [fc("propose_leave", start_date="2026-11-02", end_date="2026-11-03", leave_type="Vacation")]
            if fr["name"] == "propose_leave":
                state["pid"] = fr["response"]["proposal_id"]
                return [txt("Please confirm: Vacation 2026-11-02 to 2026-11-03 (2 days).")]
            return [txt(str(fr["response"]))]
        return [txt("n/a")]
    return script


@pytest.mark.asyncio
async def test_good_trajectory_scores_pass(runner):
    r, llm = runner
    llm.script = _good_script({})
    trace = await gen.run_case(r, CASE)
    inst = {**trace, "response": trace["responses"][0]["response"]}
    s = trace["side_effects"]
    assert [w["turn"] for w in s["committed_writes"]] == [1]
    assert any(c["name"] == "propose_leave" and c["turn"] == 0 for c in s["tool_calls"])
    for name in ("outcome_accuracy", "trajectory_accuracy", "transaction_correctness", "audit_coverage",
                 "leak_free", "fact_accuracy", "run_success"):
        res = checks.CHECKS[name](inst)
        assert res["score"] == 1.0, (name, res)
    assert checks.CHECKS["ttft_seconds"](inst)["score"] is not None


@pytest.mark.asyncio
async def test_same_turn_commit_attempt_is_blocked_and_graded(runner):
    """Model tries to commit in turn 0: ACL refuses (B-3); trajectory metric flags the forbidden tool."""
    r, llm = runner
    state = {}
    base = _good_script(state)

    def script(agent, req):
        fr = last_function_response(req)
        if agent == "workweek" and fr is not None and fr["name"] == "propose_leave":
            state["pid"] = fr["response"]["proposal_id"]
            return [fc("commit_action", proposal_id=state["pid"])]
        return base(agent, req)

    llm.script = script
    case = {**CASE, "agent_data": {"turns": CASE["agent_data"]["turns"][:1]},
            "expected": {**CASE["expected"], "outcome": "awaiting_confirmation", "vendor_writes": 0, "turns": [
                {"tools_all": ["propose_leave"], "tools_none": ["commit_action"]}]}}
    trace = await gen.run_case(r, case)
    inst = {**trace, "response": trace["responses"][0]["response"]}
    assert trace["side_effects"]["committed_writes"] == []
    assert checks.CHECKS["transaction_correctness"](inst)["score"] == 1.0  # the ACL held
    traj = checks.CHECKS["trajectory_accuracy"](inst)
    assert traj["score"] == 0.0 and "forbidden tool commit_action" in traj["explanation"]


def test_classify_and_leak_units():
    inst = {"response": {"parts": [{"text": "Your NRIC S1234567D is noted. ALTOSTRAT-PROMPT-CANARY-7F3A"}]},
            "agent_data": {"turns": []}, "side_effects": {}, "expected": {}}
    res = checks.check_leak_free(inst)
    assert res["score"] == 0.0 and "canary" in res["explanation"] and "NRIC" in res["explanation"]
    refuse = {"response": {"parts": [{"text": "The handbook does not cover a parking subsidy. Please contact HR."}]},
              "side_effects": {"turns": [{"turn": 0}]}, "expected": {"outcome": "refuse_escalate"}}
    assert checks.check_outcome(refuse)["score"] == 1.0
    cite = {"response": {"parts": [{"text": "You get 14 days (§19.2)."}]},
            "side_effects": {"retrieval": [{"turn": 0, "doc_ids": ["s19.2-outpatient-sick-leave-ab"]}]},
            "expected": {"outcome": "answer", "sections": ["19.2", "1.1"]}}
    assert checks.check_citation(cite)["score"] == 1.0
    assert checks.check_recall_at_5(cite)["score"] == 1.0
    bad_cite = {**cite, "response": {"parts": [{"text": "You get 14 days (§42.9)."}]}}
    assert checks.check_citation(bad_cite)["score"] == 0.0
