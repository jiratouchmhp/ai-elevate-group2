"""Stateful eval trace generator for the Altostrat HR agent.

Why not plain `agents-cli eval generate`? Each case needs its own deterministic world:
a persona bound to the session, a freshly reset vendor mock + ledger, optional fault
injection, a pinned clock, and capture of side effects that never appear in the chat
transcript (vendor writes, PDP decisions, audit records). This runner does exactly that
and writes the standard `agent_data` trace format, so grading is still
`agents-cli eval grade`:

    uv run python tests/eval/generate_traces.py                       # all datasets
    uv run python tests/eval/generate_traces.py -d single_turn -d redteam --limit 5
    agents-cli eval grade --traces artifacts/traces/ --config tests/eval/eval_config.yaml

Traces carry, per case: `agent_data`, `responses` (final text), the authored
`expected`, and `side_effects` {tool_calls, vendor_calls, committed_writes, audit,
retrieval, turns[{latency_s, ttft_s}]} for the deterministic metrics in metrics/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HR_FIXED_TODAY", "2026-10-05")
os.environ.setdefault("BACKEND_MODE", "inprocess")
os.environ.setdefault("INTEGRATION_MODE", "inprocess")
os.environ.setdefault("LEDGER_BACKEND", "sqlite")
os.environ.setdefault("HR_RUNTIME_DIR", str(ROOT / "artifacts" / "eval_runtime"))

DATASETS = Path(__file__).parent / "datasets"
DEFAULT_SETS = ("single_turn", "tool_calling", "multi_turn", "redteam")
SUB_AGENTS = ("policy_agent", "workweek_agent", "itsm_agent")


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _user_turns(case: dict) -> list[str]:
    if case.get("prompt"):
        return ["".join(p.get("text", "") for p in case["prompt"]["parts"])]
    turns = []
    for t in case["agent_data"]["turns"]:
        for ev in t["events"]:
            if ev.get("author") == "user":
                turns.append("".join(p.get("text", "") for p in ev["content"]["parts"]))
    return turns


def _part_json(p) -> dict | None:
    if p.text and not getattr(p, "thought", False):
        return {"text": p.text}
    if p.function_call:
        return {"function_call": {"name": p.function_call.name, "args": dict(p.function_call.args or {})}}
    if p.function_response:
        resp = p.function_response.response
        return {"function_response": {"name": p.function_response.name,
                                      "response": json.loads(json.dumps(resp, default=str))}}
    return None


async def run_case(runner, case: dict) -> dict:
    from app.integration import audit
    from app.integration.acl import WRITE_OPS
    from app.integration.ledger import get_ledger
    from mock_backends.domain import get_mock

    mock = get_mock()
    mock.reset()
    get_ledger().reset()
    if case.get("faults"):
        mock.set_faults(case["faults"])
    records: list[dict] = []
    audit.add_listener(records.append)
    write_ops = set(WRITE_OPS.values())
    persona = case.get("persona", "EMP001")
    session = await runner.session_service.create_session(
        app_name="app", user_id=persona, state={"employee_id": persona})

    turns_out, side = [], {"tool_calls": [], "vendor_calls": [], "committed_writes": [], "audit": [],
                           "retrieval": [], "turns": [], "errors": []}
    final_text = ""
    try:
        for ti, text in enumerate(_user_turns(case)):
            from google.genai import types

            n_log, n_audit = len(mock.call_log), len(records)
            events = [{"author": "user", "content": {"role": "user", "parts": [{"text": text}]}}]
            t0, ttft, turn_text = time.perf_counter(), None, []
            try:
                async for ev in runner.run_async(
                        user_id=persona, session_id=session.id,
                        new_message=types.Content(role="user", parts=[types.Part.from_text(text=text)])):
                    if not ev.content or not ev.content.parts:
                        continue
                    parts = [pj for p in ev.content.parts if (pj := _part_json(p))]
                    if not parts:
                        continue
                    events.append({"author": ev.author, "content": {"role": "model", "parts": parts}})
                    for pj in parts:
                        if "function_call" in pj:
                            side["tool_calls"].append({"turn": ti, "agent": ev.author, **pj["function_call"]})
                        if "text" in pj and ev.author == "hr_agent":
                            ttft = ttft if ttft is not None else time.perf_counter() - t0
                            turn_text.append(pj["text"])
            except Exception as e:  # recorded, graded as a failure — never crash the whole run
                side["errors"].append({"turn": ti, "error": f"{type(e).__name__}: {e}"[:500]})
            latency = time.perf_counter() - t0
            for c in mock.call_log[n_log:]:
                side["vendor_calls"].append({"turn": ti, "op": c["op"], "write": c["op"] in write_ops})
            for r in records[n_audit:]:
                slim = {k: r.get(k) for k in ("event", "agent_id", "tool_invoked", "outcome", "pdp_decision",
                                              "pdp_rule_ids", "backend_ref", "retrieved_doc_ids")}
                slim["turn"] = ti
                side["audit"].append(slim)
                if r.get("outcome") == "COMMITTED":
                    side["committed_writes"].append({"turn": ti, "tool": r.get("tool_invoked"),
                                                     "backend_ref": r.get("backend_ref")})
                if r.get("event") == "retrieval":
                    side["retrieval"].append({"turn": ti, "query": (r.get("tool_args") or {}).get("query"),
                                              "doc_ids": r.get("retrieved_doc_ids") or []})
            side["turns"].append({"turn": ti, "latency_s": round(latency, 3),
                                  "ttft_s": round(ttft, 3) if ttft is not None else None,
                                  "final_text": "\n".join(turn_text)})
            turns_out.append({"turn_index": ti, "events": events})
    finally:
        audit.remove_listener(records.append)

    final_text = side["turns"][-1]["final_text"] if side["turns"] else ""
    trace = {k: v for k, v in case.items() if k not in ("agent_data",)}
    trace["agent_data"] = {
        "agents": {n: {"agent_id": n} for n in ("hr_agent", *SUB_AGENTS)},
        "turns": turns_out,
    }
    if "prompt" not in trace:
        trace["prompt"] = {"role": "user", "parts": [{"text": _user_turns(case)[0]}]}
    trace["responses"] = [{"response": {"role": "model", "parts": [{"text": final_text or "(no response)"}]}}]
    trace["side_effects"] = side
    return trace


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--dataset", action="append", help=f"dataset name(s); default {DEFAULT_SETS}")
    ap.add_argument("--ids", help="comma-separated eval_case_ids to run")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("-o", "--out", default=str(ROOT / "artifacts" / "traces"))
    args = ap.parse_args()

    _load_env()
    os.environ["BACKEND_MODE"] = "inprocess"  # eval always runs against the seeded mock
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from app.agent import app as adk_app

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ids = set(args.ids.split(",")) if args.ids else None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name in args.dataset or DEFAULT_SETS:
        cases = json.loads((DATASETS / f"{name}.json").read_text())["eval_cases"]
        if ids:
            cases = [c for c in cases if c["eval_case_id"] in ids]
        if args.limit:
            cases = cases[: args.limit]
        if not cases:
            continue
        traces = []
        for i, case in enumerate(cases, 1):
            runner = Runner(app=adk_app, session_service=InMemorySessionService())
            t = await run_case(runner, case)
            traces.append(t)
            lat = max((x["latency_s"] for x in t["side_effects"]["turns"]), default=0)
            print(f"[{name} {i}/{len(cases)}] {case['eval_case_id']:<28} {lat:6.1f}s "
                  f"tools={[c['name'] for c in t['side_effects']['tool_calls']]}"
                  f"{' ERR' if t['side_effects']['errors'] else ''}", flush=True)
        path = out / f"traces_{name}_{stamp}.json"
        path.write_text(json.dumps({"eval_cases": traces}, indent=1, ensure_ascii=False))
        print(f"wrote {path} ({len(traces)} cases)")


if __name__ == "__main__":
    asyncio.run(main())
