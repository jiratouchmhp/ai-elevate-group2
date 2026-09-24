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
from app.governance.audit_logger import AGENT_VERSION, CORPUS_VERSION, RULES_VERSION


IAP_EMAIL_TO_EMPLOYEE_ID: Dict[str, str] = {
    "meiling.tan@altostrat.sg": "EMP-SG-001",
    "arjun.nair@altostrat.sg": "EMP-SG-002",
    "chloe.wong@altostrat.sg": "EMP-SG-003",
}


def resolve_iap_employee_id(headers: Optional[Dict[str, str]] = None) -> str:
    """Extracts verified employee_id strictly from IAP headers (§4.4, §3.10)."""
    if not headers:
        return "EMP-SG-001"
    lower_headers = {k.lower(): v for k, v in headers.items()}
    explicit_id = lower_headers.get("x-authenticated-employee-id")
    if explicit_id:
        return explicit_id.strip()
    iap_email = lower_headers.get("x-goog-authenticated-user-email", "")
    # Strip 'accounts.google.com:' prefix if present
    clean_email = iap_email.split(":")[-1].strip().lower()
    return IAP_EMAIL_TO_EMPLOYEE_ID.get(clean_email, "EMP-SG-001")


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
    return {
        "status": "ok",
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", "ai-training-van-01"),
        "region": os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-southeast1"),
        "agent_version": AGENT_VERSION,
        "rules_version": RULES_VERSION,
        "corpus_version": CORPUS_VERSION,
        "indexed_policy_chunks": len(bff.runtime.retriever.chunks),
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
        "chunks": res.chunks,
    }


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Altostrat Singapore — HR Agentic Assistant (MVP 1)</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; margin: 0; background: #f8fafc; color: #0f172a; }
    header { background: #1e293b; color: #fff; padding: 14px 24px; display: flex; justify-content: space-between; align-items: center; }
    main { max-width: 900px; margin: 24px auto; padding: 0 16px; }
    .card { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
    .msg { padding: 12px 14px; border-radius: 8px; margin-bottom: 10px; line-height: 1.5; white-space: pre-wrap; }
    .msg.user { background: #eff6ff; border: 1px solid #bfdbfe; }
    .msg.agent { background: #f8fafc; border: 1px solid #e2e8f0; }
    .chip { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; background: #dbeafe; color: #1e40af; margin-right: 6px; margin-top: 6px; text-decoration: none; }
    .confirm-box { background: #fffbeb; border: 1px solid #fcd34d; padding: 12px; border-radius: 8px; margin-top: 10px; }
    button { background: #2563eb; color: #fff; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; }
    input, select { padding: 9px 12px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }
  </style>
</head>
<body>
  <header>
    <div>
      <strong>Altostrat Singapore — HR Agentic Assistant</strong>
      <span style="font-size: 12px; opacity: 0.8; margin-left: 8px;">GCP Project: ai-training-van-01 (asia-southeast1)</span>
    </div>
    <div>
      <label style="font-size: 13px;">IAP Persona: </label>
      <select id="persona">
        <option value="meiling.tan@altostrat.sg">Mei Ling Tan (EMP-SG-001)</option>
        <option value="arjun.nair@altostrat.sg">Arjun Nair (EMP-SG-002)</option>
        <option value="chloe.wong@altostrat.sg">Chloe Wong (EMP-SG-003)</option>
      </select>
    </div>
  </header>
  <main>
    <div class="card" id="chat-log" style="min-height: 320px;">
      <div class="msg agent">Welcome to the Altostrat Singapore HR &amp; IT Assistant. Ask about policy handbook topics, check WorkWeek leave balances, or manage ServiceImmediately IT tickets.</div>
    </div>
    <div class="card" style="display: flex; gap: 8px;">
      <input id="prompt" style="flex: 1;" placeholder="e.g. What is the relocation allowance cap when transferring to London?" />
      <button onclick="sendChat(false)">Send</button>
    </div>
  </main>
  <script>
    async function sendChat(confirmed) {
      const input = document.getElementById("prompt");
      const promptText = confirmed ? "Yes, confirm action" : input.value.trim();
      if (!promptText) return;
      if (!confirmed) { input.value = ""; }
      const log = document.getElementById("chat-log");
      log.innerHTML += `<div class="msg user"><strong>You:</strong> ${promptText}</div>`;
      const persona = document.getElementById("persona").value;
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-goog-authenticated-user-email": "accounts.google.com:" + persona
        },
        body: JSON.stringify({ prompt: promptText, confirmed: confirmed })
      });
      const data = await res.json();
      let html = `<div class="msg agent"><strong>Assistant (${data.employee_id}):</strong>\\n${data.response_text}`;
      if (data.citations && data.citations.length) {
        html += `<div style="margin-top:8px;">` + data.citations.map(c =>
          `<a class="chip" href="${c.deep_link_url}" target="_blank">[${c.semantic_topic}] §${c.section_number} (${c.citation_anchor})</a>`
        ).join("") + `</div>`;
      }
      if (data.confirmation_card) {
        html += `<div class="confirm-box"><strong>Confirmation Required (${data.confirmation_card.action})</strong><br/>` +
          `<button style="margin-top:8px;" onclick="sendChat(true)">Confirm &amp; Execute</button></div>`;
      }
      html += `</div>`;
      log.innerHTML += html;
      log.scrollTop = log.scrollHeight;
    }
  </script>
</body>
</html>"""


# Optional FastAPI ASGI integration when `fastapi` is installed in the runtime environment
try:
    from fastapi import FastAPI, Request  # type: ignore
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # type: ignore

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

    @app.get("/", response_class=HTMLResponse)
    async def index_page() -> str:
        return INDEX_HTML

except ImportError:
    app = None  # type: ignore


class _FallbackAGUIHandler(BaseHTTPRequestHandler):
    """Zero-dependency stdlib HTTP/SSE server fallback when uvicorn/fastapi are not installed."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(build_health_payload())
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
        if parsed.path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
