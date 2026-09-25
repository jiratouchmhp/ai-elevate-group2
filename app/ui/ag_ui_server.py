"""Experience Plane — Cloud Run BFF serving AG-UI Protocol over SSE (SDD §1.3, §3.10, D8).

Resolves Identity-Aware Proxy (IAP) authenticated user assertion (`x-goog-authenticated-user-id`
or `x-goog-authenticated-user-email`) to `employee_id`, ensuring `employee_id` comes ONLY from
the verified IAP header and never from the user prompt (§4.4).

Emits AG-UI Server-Sent Events (SSE):
- `TEXT_MESSAGE_CONTENT`
- `TOOL_CALL_START` / `TOOL_CALL_END`
- `STATE_DELTA` (structured ConfirmationCard for B-3)
- `CUSTOM: citation` (CitationChip bound to content_hash + heading_slug + semantic_topic)
- `CUSTOM: guardrail_block` (First-class refusal surface with escalation route)
- `RUN_ERROR` (Non-technical failure copy per §5.4)
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from typing import Any, Dict, Generator, Optional
from urllib.parse import parse_qs, urlparse

from app.agent import HRMultiAgentRuntime
from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    get_mcp_base_url,
    is_live_mcp_enabled,
)
from app.governance.audit_logger import AGENT_VERSION, CORPUS_VERSION, RULES_VERSION


IAP_EMAIL_TO_EMPLOYEE_ID: Dict[str, str] = {
    "emp836@altostrat.sg": get_mcp_authenticated_employee_id(),
    "meiling.tan@altostrat.sg": "EMP-SG-001",
    "arjun.nair@altostrat.sg": "EMP-SG-002",
    "chloe.wong@altostrat.sg": "EMP-SG-003",
}


def resolve_iap_employee_id(headers: Optional[Dict[str, str]] = None) -> str:
    """Extracts verified employee_id strictly from IAP headers (§4.4, §3.10)."""
    default_emp = (
        get_mcp_authenticated_employee_id()
        if is_live_mcp_enabled(default=True)
        else "EMP-SG-001"
    )
    if not headers:
        return default_emp
    lower_headers = {k.lower(): v for k, v in headers.items()}
    explicit_id = lower_headers.get("x-authenticated-employee-id")
    if explicit_id:
        return explicit_id.strip()
    iap_email = lower_headers.get("x-goog-authenticated-user-email", "")
    # Strip 'accounts.google.com:' prefix if present
    clean_email = iap_email.split(":")[-1].strip().lower()
    if not clean_email:
        return default_emp
    if clean_email in IAP_EMAIL_TO_EMPLOYEE_ID:
        return IAP_EMAIL_TO_EMPLOYEE_ID[clean_email]
    if clean_email.upper().startswith("EMP-"):
        return clean_email.upper()
    return default_emp


class AGUIServerBFF:
    """Thin BFF terminating AG-UI over SSE and delegating all business logic to Agent Runtime + PDP."""

    def __init__(self, runtime: Optional[HRMultiAgentRuntime] = None) -> None:
        self.runtime = runtime or HRMultiAgentRuntime()

    def stream_ag_ui_events(
        self,
        *,
        prompt: str,
        headers: Optional[Dict[str, str]] = None,
        session_id: str = "sess-agui-001",
        confirmed: bool = False,
        user_asserted_resolution: bool = False,
    ) -> Generator[str, None, None]:
        """Yields AG-UI SSE formatted lines (`event: <type>\\ndata: <json>\\n\\n`)."""
        emp_id = resolve_iap_employee_id(headers)
        turn_result = self.runtime.run_turn(
            prompt,
            authenticated_employee_id=emp_id,
            session_id=session_id,
            confirmed=confirmed,
            user_asserted_resolution=user_asserted_resolution,
        )
        for ev in turn_result.events:
            ev_type = ev.get("type", "MESSAGE")
            payload = json.dumps(ev, sort_keys=True)
            yield f"event: {ev_type}\ndata: {payload}\n\n"


bff = AGUIServerBFF()


def build_health_payload() -> Dict[str, Any]:
    fs_store = bff.runtime.ledger.firestore
    return {
        "status": "ok",
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", "ai-training-van-01"),
        "region": os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-southeast1"),
        "rag_corpus_id": bff.runtime.retriever.rag_corpus_id,
        "rag_corpus": bff.runtime.retriever.rag_corpus_resource,
        "use_cloud_rag": bff.runtime.retriever.use_cloud_rag,
        "mcp_server_base_url": get_mcp_base_url(),
        "use_live_mcp": is_live_mcp_enabled(default=True),
        "firestore_database": fs_store.active_database_id,
        "use_firestore": fs_store.use_firestore,
        "firestore_status": fs_store.status_summary(),
        "default_employee_id": resolve_iap_employee_id(None),
        "agent_version": AGENT_VERSION,
        "rules_version": RULES_VERSION,
        "corpus_version": CORPUS_VERSION,
        "indexed_policy_chunks": len(bff.runtime.retriever.chunks),
    }


def build_audit_payload(
    *,
    session_id: Optional[str] = None,
    denials_only: bool = False,
    prefer_remote: bool = True,
    limit: int = 200,
) -> Dict[str, Any]:
    fs_store = bff.runtime.audit.firestore
    if denials_only:
        records = bff.runtime.audit.get_denials(prefer_remote=prefer_remote)[:limit]
    elif session_id:
        records = bff.runtime.audit.get_by_session(
            session_id, prefer_remote=prefer_remote
        )[:limit]
    else:
        records = (
            bff.runtime.audit.list_remote_records(limit=limit)
            if prefer_remote
            else bff.runtime.audit.records[:limit]
        )
    sorted_records = sorted(
        records,
        key=lambda r: r.timestamp or "",
        reverse=True,
    )
    return {
        "project_id": fs_store.project_id,
        "database_id": fs_store.active_database_id,
        "collection": "audit_logs",
        "use_firestore": fs_store.use_firestore,
        "cloud_reachable": fs_store.is_cloud_active,
        "firestore_status": fs_store.status_summary(),
        "sort_order": "timestamp_desc",
        "count": len(sorted_records),
        "records": [r.to_dict() for r in sorted_records],
    }


def build_ledger_payload() -> Dict[str, Any]:
    ledger = bff.runtime.ledger
    entries = sorted(
        [e.to_dict() for e in ledger._entries.values()],
        key=lambda x: str(x.get("created_at_iso") or ""),
        reverse=True,
    )
    sagas = sorted(
        [s.to_dict() for s in ledger._sagas.values()],
        key=lambda x: str(x.get("created_at_iso") or ""),
        reverse=True,
    )
    queue = sorted(
        [t.to_dict() for t in ledger.hr_ops_queue],
        key=lambda x: str(x.get("created_at_iso") or ""),
        reverse=True,
    )
    return {
        "project_id": ledger.firestore.project_id,
        "database_id": ledger.firestore.active_database_id,
        "entries": entries,
        "sagas": sagas,
        "hr_ops_reconciliation_queue": queue,
    }



def handle_chat_payload(body: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    emp_id = resolve_iap_employee_id(headers)
    turn_result = bff.runtime.run_turn(
        str(body.get("prompt", "")),
        authenticated_employee_id=emp_id,
        session_id=str(body.get("session_id", "sess-agui-001")),
        confirmed=bool(body.get("confirmed", False)),
        user_asserted_resolution=bool(body.get("user_asserted_resolution", False)),
    )
    return {
        "employee_id": emp_id,
        "response_text": turn_result.response_text,
        "citations": turn_result.citations,
        "confirmation_card": turn_result.confirmation_card,
        "delegated_agents": turn_result.delegated_agents,
        "tool_trajectory": turn_result.tool_trajectory,
        "selected_intent": turn_result.selected_intent,
        "intent_source": turn_result.intent_source,
        "blocked": turn_result.blocked,
        "refusal": turn_result.refusal,
        "saga_id": turn_result.saga_id,
        "saga_state": turn_result.saga_state,
        "events": turn_result.events,
    }


def handle_rag_search_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    res = bff.runtime.retriever.search(
        str(body.get("query", "")),
        jurisdiction=str(body.get("jurisdiction", "SG")),
        top_k=int(body.get("top_k", 3)),
    )
    return {
        "query": res.query,
        "expanded_terms": res.expanded_terms,
        "sufficient_context": res.sufficient_context,
        "refusal": res.refusal,
        "refusal_reason": res.refusal_reason,
        "escalation_route": res.escalation_route,
        "corpus_version": res.corpus_version,
        "rag_backend": res.rag_backend,
        "rag_corpus": res.rag_corpus,
        "cloud_hits_count": res.cloud_hits_count,
        "chunks": res.chunks,
    }


from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def _read_frontend_file(filename: str) -> str:
    return (FRONTEND_DIR / filename).read_text(encoding="utf-8")


INDEX_HTML = _read_frontend_file("index.html")


# Optional FastAPI ASGI integration when `fastapi` is installed in the runtime environment
try:
    from fastapi import FastAPI, Request  # type: ignore
    from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse  # type: ignore

    app = FastAPI(
        title="Altostrat Singapore — HR Agentic Assistant (MVP 1)",
        version=AGENT_VERSION,
        description="Google ADK 2+ Multi-Agent Runtime & AG-UI SSE BFF",
    )

    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        return build_health_payload()

    @app.post("/api/chat")
    async def chat_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(handle_chat_payload(body, dict(request.headers)))

    @app.get("/api/chat/stream")
    async def chat_stream_endpoint(
        prompt: str,
        request: Request,
        session_id: str = "sess-agui-001",
        confirmed: bool = False,
        user_asserted_resolution: bool = False,
    ) -> StreamingResponse:
        generator = bff.stream_ag_ui_events(
            prompt=prompt,
            headers=dict(request.headers),
            session_id=session_id,
            confirmed=confirmed,
            user_asserted_resolution=user_asserted_resolution,
        )
        return StreamingResponse(generator, media_type="text/event-stream")

    @app.post("/api/rag/search")
    async def rag_search_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse(handle_rag_search_payload(body))

    @app.get("/api/audit")
    async def audit_endpoint(
        session_id: Optional[str] = None,
        denials_only: bool = False,
        prefer_remote: bool = True,
        limit: int = 200,
    ) -> JSONResponse:
        return JSONResponse(
            build_audit_payload(
                session_id=session_id,
                denials_only=denials_only,
                prefer_remote=prefer_remote,
                limit=limit,
            )
        )

    @app.get("/api/ledger")
    async def ledger_endpoint() -> JSONResponse:
        return JSONResponse(build_ledger_payload())

    @app.get("/", response_class=HTMLResponse)
    @app.get("/index.html", response_class=HTMLResponse)
    @app.get("/audit", response_class=HTMLResponse)
    @app.get("/audit.html", response_class=HTMLResponse)
    async def index_page() -> str:
        return _read_frontend_file("index.html")

    @app.get("/frontend/styles.css")
    async def frontend_styles() -> Response:
        return Response(content=_read_frontend_file("styles.css"), media_type="text/css; charset=utf-8")

    @app.get("/frontend/App.tsx")
    async def frontend_app_tsx() -> Response:
        return Response(content=_read_frontend_file("App.tsx"), media_type="text/plain; charset=utf-8")

    @app.post("/api/reasoning_engine")
    async def reasoning_engine_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        class_method = body.get("class_method", "query")
        inp = body.get("input") or {}
        user_id = inp.get("user_id") or resolve_iap_employee_id(dict(request.headers))
        session_id = inp.get("session_id") or "sess-re-001"

        if class_method in ("create_session", "async_create_session"):
            return JSONResponse(
                {"output": {"id": session_id, "user_id": user_id, "app_name": "altostrat_hr_agent", "state": {}}}
            )
        if class_method in ("get_session", "async_get_session"):
            state = bff.runtime.get_session_state(session_id, user_id)
            return JSONResponse(
                {
                    "output": {
                        "id": session_id,
                        "user_id": user_id,
                        "app_name": "altostrat_hr_agent",
                        "state": {"pending_confirmation": state.get("pending_confirmation")},
                    }
                }
            )
        if class_method in ("list_sessions", "async_list_sessions"):
            return JSONResponse(
                {"output": {"sessions": [{"id": session_id, "user_id": user_id, "app_name": "altostrat_hr_agent"}]}}
            )
        if class_method in ("delete_session", "async_delete_session"):
            return JSONResponse({"output": {"status": "deleted", "id": session_id}})

        msg = inp.get("message") or inp.get("prompt") or inp.get("input") or ""
        if isinstance(msg, dict):
            parts = msg.get("parts") or []
            msg = " ".join(p.get("text", "") for p in parts if isinstance(p, dict)) or str(msg)
        result = handle_chat_payload(
            {"prompt": str(msg), "session_id": session_id, "confirmed": bool(inp.get("confirmed", False))},
            dict(request.headers),
        )
        return JSONResponse({"output": result})

    @app.post("/api/stream_reasoning_engine")
    async def stream_reasoning_engine_endpoint(request: Request) -> StreamingResponse:
        body = await request.json()
        inp = body.get("input") or {}
        session_id = inp.get("session_id") or "sess-re-001"
        msg = inp.get("message") or inp.get("prompt") or inp.get("input") or ""
        if isinstance(msg, dict):
            parts = msg.get("parts") or []
            msg = " ".join(p.get("text", "") for p in parts if isinstance(p, dict)) or str(msg)

        result = handle_chat_payload(
            {"prompt": str(msg), "session_id": session_id, "confirmed": bool(inp.get("confirmed", False))},
            dict(request.headers),
        )

        async def _stream_gen():
            event_payload = {
                "content": {
                    "role": "model",
                    "parts": [{"text": result.get("response_text", "")}],
                },
                "author": "altostrat_hr_agent",
                "custom_metadata": {
                    "citations": result.get("citations", []),
                    "confirmation_card": result.get("confirmation_card"),
                    "delegated_agents": result.get("delegated_agents", []),
                    "tool_trajectory": result.get("tool_trajectory", []),
                    "selected_intent": result.get("selected_intent", ""),
                    "intent_source": result.get("intent_source", ""),
                },
            }
            yield json.dumps(event_payload) + "\n"

        return StreamingResponse(_stream_gen(), media_type="application/json")

except ImportError:
    app = None  # type: ignore


class _FallbackAGUIHandler(BaseHTTPRequestHandler):
    """Zero-dependency stdlib HTTP/SSE server fallback when uvicorn/fastapi are not installed."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(build_health_payload())
            return
        if parsed.path == "/api/audit":
            qs = parse_qs(parsed.query)
            session_id = qs.get("session_id", [None])[0]
            denials_only = qs.get("denials_only", ["false"])[0].lower() == "true"
            prefer_remote = qs.get("prefer_remote", ["true"])[0].lower() != "false"
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except (ValueError, TypeError):
                limit = 200
            self._send_json(
                build_audit_payload(
                    session_id=session_id,
                    denials_only=denials_only,
                    prefer_remote=prefer_remote,
                    limit=limit,
                )
            )
            return
        if parsed.path == "/api/ledger":
            self._send_json(build_ledger_payload())
            return
        if parsed.path == "/api/chat/stream":
            qs = parse_qs(parsed.query)
            prompt = qs.get("prompt", [""])[0]
            session_id = qs.get("session_id", ["sess-agui-001"])[0]
            confirmed = qs.get("confirmed", ["false"])[0].lower() == "true"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            headers = {k: v for k, v in self.headers.items()}
            for chunk in bff.stream_ag_ui_events(
                prompt=prompt,
                headers=headers,
                session_id=session_id,
                confirmed=confirmed,
            ):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
            return
        if parsed.path in ("/", "/index.html", "/audit", "/audit.html"):
            body = _read_frontend_file("index.html").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/frontend/styles.css":
            body = _read_frontend_file("styles.css").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/frontend/App.tsx":
            body = _read_frontend_file("App.tsx").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw or "{}")
        headers = {k: v for k, v in self.headers.items()}
        if parsed.path == "/api/chat":
            self._send_json(handle_chat_payload(body, headers))
            return
        if parsed.path == "/api/rag/search":
            self._send_json(handle_rag_search_payload(body))
            return
        self.send_error(404, "Not Found")

    def _send_json(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start Altostrat HR Agentic Assistant Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Bind port (default: 8080)",
    )
    args = parser.parse_args()

    try:
        import uvicorn  # type: ignore

        if app is not None:
            print(
                f"Starting Altostrat HR Agentic Assistant via Uvicorn on http://{args.host}:{args.port} "
                f"(Project: {os.environ.get('GOOGLE_CLOUD_PROJECT', 'ai-training-van-01')})"
            )
            uvicorn.run(app, host=args.host, port=args.port)
            return
    except ImportError:
        pass

    print(
        f"Starting Altostrat HR Agentic Assistant HTTP/SSE Server on http://{args.host}:{args.port} "
        f"(Project: {os.environ.get('GOOGLE_CLOUD_PROJECT', 'ai-training-van-01')})"
    )
    server = ThreadingHTTPServer((args.host, args.port), _FallbackAGUIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
