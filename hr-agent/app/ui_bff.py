"""Thin BFF for the React chat UI (SDD §3.10 "Hosting and identity").

Responsibilities — and nothing else (no business logic; all validation stays in the ACL/PDP):
  * resolve the caller's identity (IAP assertion -> employee_id, or local demo persona);
  * create ADK sessions bound to that identity;
  * stream AG-UI events (SSE) translated from the ADK Runner (`app/ui_events.py`);
  * serve governed handbook passages for citation deep links (FR-5.3).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types
from pydantic import BaseModel, Field

from app import config
from app.integration import audit
from app.policy.ingest import load_corpus
from app.ui_events import AguiTranslator

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ui"])

IAP_EMAIL_HEADER = "x-goog-authenticated-user-email"
DEMO_PERSONA_HEADER = "x-demo-persona"


# ---------------------------------------------------------------------------- identity
def _seed_employees() -> dict:
    from mock_backends.domain import get_mock

    return get_mock().snapshot().get("employees", {})


def dev_personas_enabled() -> bool:
    # Persona switching is a local-demo affordance only: never with a shared PAT (identity
    # collapse) and never behind IAP.
    return config.UI_DEV_PERSONAS and config.BACKEND_MODE == "inprocess"


def resolve_employee_id(request: Request) -> str:
    iap = request.headers.get(IAP_EMAIL_HEADER)
    if iap:
        email = iap.split(":", 1)[-1].strip().lower()
        emp = config.PERSONA_MAP.get(email)
        if not emp:
            raise HTTPException(status_code=403, detail="Your account is not enrolled in the HR assistant pilot.")
        return emp
    if dev_personas_enabled():
        wanted = (request.headers.get(DEMO_PERSONA_HEADER) or "").strip().upper()
        if wanted and wanted in _seed_employees():
            return wanted
    return config.DEMO_EMPLOYEE_ID


def _runner(request: Request):
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent is starting up. Please retry.")
    return runner


# ---------------------------------------------------------------------------- endpoints
@router.get("/me")
async def me(request: Request) -> dict:
    emp = resolve_employee_id(request)
    body: dict = {"employee_id": emp, "mode": config.BACKEND_MODE, "today": _today()}
    if config.BACKEND_MODE == "inprocess":
        p = _seed_employees().get(emp, {})
        body |= {k: p.get(k) for k in ("name", "department", "role", "location_status")}
    if dev_personas_enabled():
        body["personas"] = [
            {"employee_id": k, "name": v.get("name"), "location_status": v.get("location_status"),
             "role": v.get("role")}
            for k, v in sorted(_seed_employees().items())
        ]
    return body


def _today() -> str:
    from app import clock

    return clock.today().isoformat()


class SessionOut(BaseModel):
    session_id: str
    employee_id: str


@router.post("/sessions", response_model=SessionOut)
async def create_session(request: Request) -> SessionOut:
    runner = _runner(request)
    emp = resolve_employee_id(request)
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=emp,
        state={"employee_id": emp, "hr_session_id": f"hrs-{uuid.uuid4().hex[:12]}"},
    )
    return SessionOut(session_id=session.id, employee_id=emp)


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4000)


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, default=str)}\n\n"


@router.post("/chat")
async def chat(body: ChatIn, request: Request) -> StreamingResponse:
    runner = _runner(request)
    emp = resolve_employee_id(request)
    # Sessions are keyed by (app, user_id=employee_id): another persona's session id is simply not found.
    session = await runner.session_service.get_session(app_name=runner.app_name, user_id=emp,
                                                       session_id=body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Conversation not found. Start a new chat.")
    hr_sid = session.state.get("hr_session_id") or session.id

    async def stream() -> AsyncIterator[str]:
        tr = AguiTranslator(thread_id=session.id)
        blocks: list[dict] = []

        def on_audit(rec: dict) -> None:
            if rec.get("event") == "guardrail_block" and rec.get("session_id") in (hr_sid, session.id):
                blocks.append(rec)

        def drain() -> list[dict]:
            out = []
            while blocks:
                out += tr.guardrail_block(blocks.pop(0))
            return out

        audit.add_listener(on_audit)
        try:
            for ev in tr.start():
                yield _sse(ev)
            msg = types.Content(role="user", parts=[types.Part.from_text(text=body.message)])
            async for event in runner.run_async(user_id=emp, session_id=session.id, new_message=msg,
                                                run_config=RunConfig(streaming_mode=StreamingMode.SSE)):
                for ev in drain() + tr.translate(event):
                    yield _sse(ev)
            for ev in drain() + tr.finish():
                yield _sse(ev)
        except Exception:
            log.exception("ui chat run failed")
            for ev in tr.error():
                yield _sse(ev)
        finally:
            audit.remove_listener(on_audit)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@lru_cache(maxsize=1)
def _corpus_index() -> dict:
    rep = load_corpus()
    return {"version": rep.corpus_version, "chunks": rep.chunks,
            "by_anchor": {c.anchor: c for c in rep.chunks}}


@router.get("/corpus/{anchor}")
async def corpus_passage(anchor: str) -> dict:
    idx = _corpus_index()
    chunk = idx["by_anchor"].get(anchor)
    if chunk is None:
        raise HTTPException(status_code=404, detail="Citation not found in the governed corpus.")
    siblings = [c for c in idx["chunks"] if c.section_number == chunk.section_number]
    return {
        "corpus_version": idx["version"],
        "anchor": chunk.anchor,
        "citation_label": chunk.citation_label,
        "semantic_topic": chunk.semantic_topic,
        "section_number": chunk.section_number,
        "section_title": chunk.section_title,
        "effective_date": chunk.effective_date,
        "misfiled_from": chunk.misfiled_from,
        "passages": [
            {"anchor": c.anchor, "subsection": c.subsection, "title": c.subsection_title,
             "text": c.text, "highlight": c.anchor == chunk.anchor}
            for c in siblings
        ],
    }
