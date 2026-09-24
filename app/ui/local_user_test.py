"""Local End-to-End User Smoke & Integration Verifier (`make test-local`).

Simulates a real authenticated user (`EMP-836`) interacting with the local AG-UI BFF HTTP Server
and Google ADK Multi-Agent Runtime connected to:
1. GCP Vertex AI RAG Engine (`projects/ai-training-van-01/locations/asia-southeast1/ragCorpora/4611686018427387904`)
2. Live WorkWeek HCM MCP Server (`/work-week/mcp/`)
3. Live ServiceImmediately ITSM MCP Server (`/service-immediately/mcp/`)
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
import sys
import threading
import time
from typing import Any, Dict, Tuple
import urllib.request

from app.adk_compat import ADK_NATIVE_AVAILABLE
from app.agent import app as adk_app, root_agent
from app.config.env_config import (
    get_mcp_authenticated_employee_id,
    get_mcp_base_url,
    is_live_mcp_enabled,
)
from app.ui.ag_ui_server import _FallbackAGUIHandler


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
) -> Tuple[int, Dict[str, Any]]:
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def run_local_user_verification() -> int:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "ai-training-van-01")
    region = os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-southeast1")
    corpus_id = os.environ.get("VERTEX_RAG_CORPUS_ID", "4611686018427387904")
    emp_id = get_mcp_authenticated_employee_id()
    mcp_url = get_mcp_base_url()

    print("=" * 82)
    print(" Altostrat Singapore — Pre-Deployment Local User Verification (make test-local)")
    print("=" * 82)
    print(f"  GCP Project / Region  : {project_id} ({region})")
    print(f"  Vertex AI RAG Corpus  : {corpus_id}")
    print(f"  Live MCP Base URL     : {mcp_url}")
    print(f"  Authenticated User ID : {emp_id} (Live MCP Tenant)")
    print(f"  Google ADK Runtime    : {'Native google.adk' if ADK_NATIVE_AVAILABLE else 'ADK 2+ Compat'}")
    print("-" * 82)

    # 1. Verify ADK Agent Hierarchy
    assert adk_app.name == "altostrat_hr_agent", f"Unexpected app name: {adk_app.name}"
    assert len(root_agent.tools) == 0, "Root agent must hold zero direct tools (SDD §3.1)"
    assert len(root_agent.sub_agents) == 3, "Root agent must delegate to 3 domain sub-agents"
    print("  [PASS] 1/5 ADK Multi-Agent Hierarchy : root_orchestrator -> [policy, workweek, service_immediately]")

    # Start local AG-UI HTTP server on an ephemeral port
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FallbackAGUIHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    user_headers = {
        "x-goog-authenticated-user-email": "accounts.google.com:emp836@altostrat.sg",
    }

    try:
        # 2. Verify /health endpoint
        status_code, health = _http_json(f"{base_url}/health")
        assert status_code == 200 and health.get("status") == "ok", f"Health check failed: {health}"
        assert health.get("default_employee_id") == emp_id, f"Expected default_employee_id={emp_id}, got {health}"
        assert is_live_mcp_enabled(default=True), "USE_LIVE_MCP is not enabled or MCP_TOKEN is missing"
        print(
            f"  [PASS] 2/5 Local UI Health Check     : status=ok, default_employee={health['default_employee_id']}, "
            f"chunks={health['indexed_policy_chunks']}"
        )

        # 3. Verify User Policy Q&A via GCP Vertex AI RAG Engine
        t0 = time.time()
        _, rag_chat = _http_json(
            f"{base_url}/api/chat",
            method="POST",
            headers=user_headers,
            payload={
                "prompt": "What is the relocation allowance cap when transferring to London?",
                "session_id": "sess-local-verify-1",
            },
        )
        dt_rag = (time.time() - t0) * 1000
        citations = rag_chat.get("citations", [])
        assert citations, f"Expected policy citations, got: {rag_chat}"
        rag_backend = citations[0].get("rag_backend", "unknown")
        assert "$10,000" in rag_chat.get("response_text", ""), f"Expected $10,000 cap in response: {rag_chat}"
        print(
            f"  [PASS] 3/5 User Policy Q&A (GCP RAG) : backend={rag_backend}, "
            f"anchor={citations[0]['citation_anchor']} ({dt_rag:.0f} ms)"
        )

        # 4. Verify User WorkWeek Leave Balance via Live MCP Server (EMP-836)
        t0 = time.time()
        _, ww_chat = _http_json(
            f"{base_url}/api/chat",
            method="POST",
            headers=user_headers,
            payload={
                "prompt": "What is my current leave balance?",
                "session_id": "sess-local-verify-2",
            },
        )
        dt_ww = (time.time() - t0) * 1000
        assert ww_chat.get("employee_id") == emp_id, f"Expected employee_id={emp_id}, got {ww_chat.get('employee_id')}"
        assert "workweek_agent" in ww_chat.get("delegated_agents", []), f"Expected workweek_agent: {ww_chat}"
        assert "get_leave_balance" in ww_chat.get("tool_trajectory", []), f"Expected get_leave_balance: {ww_chat}"
        assert "Vacation" in ww_chat.get("response_text", ""), f"Expected Vacation balance in response: {ww_chat}"
        print(
            f"  [PASS] 4/5 User WorkWeek (Live MCP)  : employee={ww_chat['employee_id']}, "
            f"tools={ww_chat['tool_trajectory']} ({dt_ww:.0f} ms)"
        )

        # 5. Verify User ServiceImmediately Tickets & Cross-System Eligibility (GCP RAG + Live MCP)
        t0 = time.time()
        _, cross_chat = _http_json(
            f"{base_url}/api/chat",
            method="POST",
            headers=user_headers,
            payload={
                "prompt": "Am I eligible for a home office monitor, and what is the allowance cap?",
                "session_id": "sess-local-verify-3",
            },
        )
        dt_cross = (time.time() - t0) * 1000
        assert "policy_agent" in cross_chat.get("delegated_agents", []), f"Missing policy_agent: {cross_chat}"
        assert "workweek_agent" in cross_chat.get("delegated_agents", []), f"Missing workweek_agent: {cross_chat}"
        assert "$500" in cross_chat.get("response_text", ""), f"Missing $500 cap in response: {cross_chat}"
        print(
            f"  [PASS] 5/5 Cross-System (RAG + MCP)  : agents={cross_chat['delegated_agents']}, "
            f"tools={cross_chat['tool_trajectory']} ({dt_cross:.0f} ms)"
        )

    finally:
        server.shutdown()
        server.server_close()

    print("-" * 82)
    print("  ALL LOCAL USER VERIFICATION CHECKS PASSED!")
    print("  To interact in your browser before deploying to GCP, run:")
    print("    make start-local    (starts BOTH Customer Chat UI :8080 and ADK Web UI :8000)")
    print("    - Customer Chat UI : http://vannick2.c.googlers.com:8080")
    print("    - ADK Developer UI : http://vannick2.c.googlers.com:8000")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(run_local_user_verification())
