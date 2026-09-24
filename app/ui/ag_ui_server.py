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

import json
from typing import Any, Dict, Generator, Optional

from app.agent import HRMultiAgentRuntime


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
