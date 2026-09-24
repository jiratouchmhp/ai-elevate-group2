"""Structured Audit Logging and Governance Telemetry (SDD §4.6, §7.2, NFR-1.2).

Every turn and tool invocation emits a structured AuditRecord — including denials
(pdp_decision='DENY' and guardrail blocks).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import uuid
from typing import Any, Dict, List, Optional


AGENT_VERSION = "1.0.0-mvp1"
PROMPT_VERSION = "2026.09.23-v1.2"
RULES_VERSION = "1.2.0"
CORPUS_VERSION = "2026-07-altostrat-sg-v1"


@dataclass
class AuditRecord:
    """Structured audit log record conforming to SDD §4.6 table."""

    correlation_id: str
    session_id: str
    employee_id: str
    actor_type: str = "AUTOMATED_AGENT"  # AUTOMATED_AGENT vs HUMAN (FR-1.2 / FR-4.1)
    agent_id: str = "spiffe://altostrat.sg/ns/agent-runtime/sa/root-orchestrator"
    agent_version: str = AGENT_VERSION
    prompt_version: str = PROMPT_VERSION
    rules_version: str = RULES_VERSION
    corpus_version: str = CORPUS_VERSION
    tool_invoked: Optional[str] = None
    tool_args_redacted: Optional[Dict[str, Any]] = None
    pdp_decision: Optional[str] = None  # ALLOW, ALLOW_WITH_WARNING, DENY, CONFIRMATION_REQUIRED
    pdp_rule_id: Optional[str] = None
    guardrail_verdicts: Dict[str, Any] = field(default_factory=dict)
    retrieved_doc_ids: List[str] = field(default_factory=list)
    relevance_scores: List[float] = field(default_factory=list)
    outcome: str = "SUCCESS"  # SUCCESS, DENIED, BLOCKED, PARTIAL_FAILURE, ERROR
    backend_ref: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


class AuditLogger:
    """In-memory + structured JSON audit sink (Cloud Logging / BigQuery sink ready)."""

    def __init__(self) -> None:
        self._records: List[AuditRecord] = []

    def record(
        self,
        *,
        session_id: str,
        employee_id: str,
        correlation_id: Optional[str] = None,
        actor_type: str = "AUTOMATED_AGENT",
        agent_id: str = "spiffe://altostrat.sg/ns/agent-runtime/sa/root-orchestrator",
        tool_invoked: Optional[str] = None,
        tool_args_redacted: Optional[Dict[str, Any]] = None,
        pdp_decision: Optional[str] = None,
        pdp_rule_id: Optional[str] = None,
        guardrail_verdicts: Optional[Dict[str, Any]] = None,
        retrieved_doc_ids: Optional[List[str]] = None,
        relevance_scores: Optional[List[float]] = None,
        outcome: str = "SUCCESS",
        backend_ref: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> AuditRecord:
        entry = AuditRecord(
            correlation_id=correlation_id or f"corr-{uuid.uuid4().hex[:12]}",
            session_id=session_id,
            employee_id=employee_id,
            actor_type=actor_type,
            agent_id=agent_id,
            tool_invoked=tool_invoked,
            tool_args_redacted=tool_args_redacted or {},
            pdp_decision=pdp_decision,
            pdp_rule_id=pdp_rule_id,
            guardrail_verdicts=guardrail_verdicts or {},
            retrieved_doc_ids=retrieved_doc_ids or [],
            relevance_scores=relevance_scores or [],
            outcome=outcome,
            backend_ref=backend_ref,
            notes=notes,
        )
        self._records.append(entry)
        return entry

    @property
    def records(self) -> List[AuditRecord]:
        return list(self._records)

    def get_by_session(self, session_id: str) -> List[AuditRecord]:
        return [r for r in self._records if r.session_id == session_id]

    def get_denials(self) -> List[AuditRecord]:
        return [
            r
            for r in self._records
            if r.pdp_decision == "DENY" or r.outcome in ("DENIED", "BLOCKED")
        ]

    def clear(self) -> None:
        self._records.clear()


DEFAULT_AUDIT_LOGGER = AuditLogger()
