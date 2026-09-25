"""LLM-as-judge metrics (SDD §9.4 layer 2): groundedness and answer correctness.

One judge call per case returns both verdicts; results are cached per case so the two
metrics share a call. The judge sees ONLY what the agent saw — the policy excerpts
returned by `search_policy` (and system-of-record tool results) inside the trace — so
"grounded" means "supported by retrieved evidence", not "plausible".

Env: HR_JUDGE_MODEL (default gemini-3.8-flash), GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_*
or GEMINI_API_KEY. Temperature 0, JSON schema output.
"""

from __future__ import annotations

import json
import os
import threading

_local = threading.local()
_cache: dict[str, dict] = {}
_lock = threading.Lock()

JUDGE_PROMPT = """You are a strict evaluator for an enterprise HR assistant (Altostrat, Singapore).
The assistant must answer ONLY from evidence returned by its tools (policy handbook excerpts,
HR/ITSM system records). You will grade the FINAL RESPONSE.

Return JSON with:
- "grounded": true if EVERY factual policy/system claim in the final response (numbers, eligibility,
  durations, amounts, procedures, record values) is supported by the TOOL EVIDENCE below; false if
  any claim is unsupported or contradicts the evidence. Generic courtesy, clarifying questions,
  refusals and "contact HR" routing are not claims. If the response makes no factual claims, true.
- "unsupported_claims": list of the unsupported/contradicted claims (empty if grounded).
- "correct": true if the response is consistent with the EXPECTED ANSWER (same key facts; extra
  correct detail is fine; missing the key fact or stating a different figure is incorrect). If
  EXPECTED ANSWER is "(none)", judge whether the response appropriately fulfils EXPECTED BEHAVIOUR.
- "explanation": one or two sentences.

EXPECTED BEHAVIOUR: {outcome}
EXPECTED ANSWER: {reference}

USER CONVERSATION (user turns):
{user_turns}

TOOL EVIDENCE (verbatim tool results seen by the assistant, truncated):
{evidence}

FINAL RESPONSE:
{response}
"""


def _client():
    c = getattr(_local, "client", None)
    if c is None:
        from google import genai

        c = _local.client = genai.Client()
    return c


def _g(o, k, d=None):
    if o is None:
        return d
    return o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)


def _texts(content) -> str:
    return "\n".join(t for p in (_g(content, "parts") or []) if (t := _g(p, "text")))


def _evidence(instance, limit: int = 24000) -> str:
    chunks = []
    for turn in _g(_g(instance, "agent_data"), "turns") or []:
        for ev in _g(turn, "events") or []:
            for p in _g(_g(ev, "content"), "parts") or []:
                fr = _g(p, "function_response")
                if fr:
                    resp = _g(fr, "response")
                    if isinstance(resp, dict) and "excerpts" in resp:
                        body = resp["excerpts"]
                    else:
                        body = json.dumps(resp, default=str, ensure_ascii=False)
                    chunks.append(f"[{_g(fr, 'name')}] {body}")
    text = "\n\n".join(chunks)
    return text[:limit] if text else "(no tool evidence)"


def _user_turns(instance) -> str:
    out = []
    for turn in _g(_g(instance, "agent_data"), "turns") or []:
        for ev in _g(turn, "events") or []:
            if _g(ev, "author") == "user":
                out.append("- " + _texts(_g(ev, "content")))
    return "\n".join(out) or _texts(_g(instance, "prompt"))


def judge(instance) -> dict:
    key = str(_g(instance, "eval_case_id") or id(instance))
    with _lock:
        if key in _cache:
            return _cache[key]
    from google.genai import types
    from pydantic import BaseModel

    class Verdict(BaseModel):
        grounded: bool
        unsupported_claims: list[str]
        correct: bool
        explanation: str

    ref = _g(instance, "reference")
    ref_text = _texts(_g(ref, "response")) if ref else ""
    exp = _g(instance, "expected") or {}
    prompt = JUDGE_PROMPT.format(
        outcome=exp.get("outcome", "answer"), reference=ref_text or "(none)",
        user_turns=_user_turns(instance), evidence=_evidence(instance),
        response=_texts(_g(instance, "response")) or "(empty)")
    resp = _client().models.generate_content(
        model=os.getenv("HR_JUDGE_MODEL", "gemini-3.8-flash"), contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                           response_schema=Verdict))
    v = Verdict.model_validate_json(resp.text).model_dump()
    with _lock:
        _cache[key] = v
    return v


def groundedness(instance) -> dict:
    v = judge(instance)
    expl = v["explanation"] + (f" Unsupported: {v['unsupported_claims']}" if v["unsupported_claims"] else "")
    return {"score": 1.0 if v["grounded"] else 0.0, "explanation": expl}


def answer_correctness(instance) -> dict:
    exp = _g(instance, "expected") or {}
    if not _g(instance, "reference") and exp.get("outcome") not in ("answer", "refuse_escalate", "clarify"):
        return {"score": None, "explanation": "N/A: no reference answer / not a Q&A case"}
    v = judge(instance)
    return {"score": 1.0 if v["correct"] else 0.0, "explanation": v["explanation"]}


CHECKS = {"groundedness": groundedness, "answer_correctness": answer_correctness}
