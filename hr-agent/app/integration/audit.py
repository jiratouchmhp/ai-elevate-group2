"""Structured audit log (SDD §4.6, NFR-1.2, FR-1.2/FR-4.1).

Every tool attempt — allowed, denied, blocked or failed — and every guardrail
block emits one JSON line. SPII is redacted before anything is written (FR-1.4).
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from app import clock, config
from app.guardrails.spii import redact_obj

_lock = threading.Lock()
_listeners: list = []  # in-process subscribers (the eval trace generator attaches here)


def add_listener(fn) -> None:
    _listeners.append(fn)


def remove_listener(fn) -> None:
    if fn in _listeners:
        _listeners.remove(fn)


def new_correlation_id() -> str:
    return f"corr-{uuid.uuid4().hex[:16]}"


def _versions() -> dict:
    from app.integration.pdp import rules_version
    from app.policy.ingest import load_corpus

    return {
        "agent_version": config.AGENT_VERSION,
        "prompt_version": config.PROMPT_VERSION,
        "rules_version": rules_version(),
        "corpus_version": load_corpus().corpus_version,
    }


def emit(
    *,
    event: str,
    session_id: str | None,
    employee_id: str | None,
    agent_id: str | None = None,
    correlation_id: str | None = None,
    tool_invoked: str | None = None,
    tool_args: dict | None = None,
    pdp_decision: str | None = None,
    pdp_rule_ids: list[str] | None = None,
    guardrail_verdicts: list[dict] | None = None,
    retrieved_doc_ids: list[str] | None = None,
    relevance_scores: list[float] | None = None,
    outcome: str | None = None,
    backend_ref: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict:
    record = {
        "ts": clock.now().isoformat(),
        "event": event,
        "correlation_id": correlation_id or new_correlation_id(),
        "session_id": session_id,
        "employee_id": employee_id,
        "actor_type": "AUTOMATED_AGENT",
        "agent_id": agent_id,
        **_versions(),
        "tool_invoked": tool_invoked,
        "tool_args_redacted": redact_obj(tool_args or {}),
        "pdp_decision": pdp_decision,
        "pdp_rule_ids": pdp_rule_ids or [],
        "guardrail_verdicts": guardrail_verdicts or [],
        "retrieved_doc_ids": retrieved_doc_ids or [],
        "relevance_scores": relevance_scores or [],
        "outcome": outcome,
        "backend_ref": backend_ref,
    }
    if extra:
        record["extra"] = redact_obj(extra)
    line = json.dumps(record, default=str)
    if config.AUDIT_SINK in ("file", "both"):
        path = Path(config.AUDIT_LOG_PATH)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    if config.AUDIT_SINK in ("stdout", "both"):
        # Cloud Run / Agent Runtime parse one-line JSON on stdout as structured logs. The
        # `logName`-independent marker `hr_audit=true` is what the BigQuery sink filters on.
        severity = "WARNING" if record["pdp_decision"] == "DENY" or event.startswith("guardrail") else "INFO"
        envelope = {"severity": severity, "message": f"hr_audit {event} {record['outcome']}",
                    "hr_audit": True, "logging.googleapis.com/labels": {
                        "hr_event": event, "pdp_decision": str(record["pdp_decision"]),
                        "agent_id": str(agent_id)},
                    "logging.googleapis.com/trace_sampled": False, **record}
        with _lock:
            sys.stdout.write(json.dumps(envelope, default=str) + "\n")
            sys.stdout.flush()
    for fn in list(_listeners):
        try:
            fn(record)
        except Exception:  # listeners must never break the audit path
            pass
    return record
