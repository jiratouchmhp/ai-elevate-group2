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


from app.ledger.firestore_client import (
    COLLECTION_AUDIT_LOGS,
    DEFAULT_FIRESTORE_STORE,
    FirestoreStore,
)


AGENT_VERSION = "1.0.0-mvp1"
PROMPT_VERSION = "2026.09.23-v1.2"
RULES_VERSION = "1.2.0"
CORPUS_VERSION = "2026-07-altostrat-sg-v1"


@dataclass
class AuditRecord:
    """Structured audit log record conforming to SDD §4.6 table and BRD NFR-1.2 / FR-1.2 / FR-4.1."""

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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditRecord":
        return cls(
            correlation_id=str(data.get("correlation_id", "")),
            session_id=str(data.get("session_id", "")),
            employee_id=str(data.get("employee_id", "")),
            actor_type=str(data.get("actor_type", "AUTOMATED_AGENT")),
            agent_id=str(
                data.get(
                    "agent_id",
                    "spiffe://altostrat.sg/ns/agent-runtime/sa/root-orchestrator",
                )
            ),
            agent_version=str(data.get("agent_version", AGENT_VERSION)),
            prompt_version=str(data.get("prompt_version", PROMPT_VERSION)),
            rules_version=str(data.get("rules_version", RULES_VERSION)),
            corpus_version=str(data.get("corpus_version", CORPUS_VERSION)),
            tool_invoked=data.get("tool_invoked"),
            tool_args_redacted=dict(data.get("tool_args_redacted") or {}),
            pdp_decision=data.get("pdp_decision"),
            pdp_rule_id=data.get("pdp_rule_id"),
            guardrail_verdicts=dict(data.get("guardrail_verdicts") or {}),
            retrieved_doc_ids=[str(x) for x in (data.get("retrieved_doc_ids") or [])],
            relevance_scores=[float(x) for x in (data.get("relevance_scores") or [])],
            outcome=str(data.get("outcome", "SUCCESS")),
            backend_ref=data.get("backend_ref"),
            timestamp=str(
                data.get("timestamp") or datetime.now(timezone.utc).isoformat()
            ),
            notes=data.get("notes"),
        )


class AuditLogger:
    """Cloud Firestore + In-memory structured audit sink (BRD NFR-1.2, FR-1.2, FR-4.1, SDD §4.6)."""

    def __init__(self, firestore_store: Optional[FirestoreStore] = None) -> None:
        self._records: List[AuditRecord] = []
        self.firestore: FirestoreStore = firestore_store or DEFAULT_FIRESTORE_STORE

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
        self.firestore.upsert_document(
            COLLECTION_AUDIT_LOGS,
            entry.correlation_id,
            entry.to_dict(),
        )
        return entry

    @property
    def records(self) -> List[AuditRecord]:
        return list(self._records)

    def get_record(
        self, correlation_id: str, *, prefer_remote: bool = False
    ) -> Optional[AuditRecord]:
        if not prefer_remote:
            for r in self._records:
                if r.correlation_id == correlation_id:
                    return r
        doc = self.firestore.get_document(
            COLLECTION_AUDIT_LOGS, correlation_id, prefer_remote=prefer_remote
        )
        if doc and doc.get("correlation_id"):
            rec = AuditRecord.from_dict(doc)
            if not any(r.correlation_id == rec.correlation_id for r in self._records):
                self._records.append(rec)
            return rec
        return None

    def get_by_session(
        self, session_id: str, *, prefer_remote: bool = False
    ) -> List[AuditRecord]:
        if prefer_remote:
            docs = self.firestore.query_documents(
                COLLECTION_AUDIT_LOGS,
                field_equals={"session_id": session_id},
                prefer_remote=True,
            )
            by_id: Dict[str, AuditRecord] = {
                r.correlation_id: r
                for r in self._records
                if r.session_id == session_id
            }
            for d in docs:
                if d.get("correlation_id"):
                    rec = AuditRecord.from_dict(d)
                    by_id[rec.correlation_id] = rec
                    if not any(
                        r.correlation_id == rec.correlation_id for r in self._records
                    ):
                        self._records.append(rec)
            return list(by_id.values())
        return [r for r in self._records if r.session_id == session_id]

    def get_denials(self, *, prefer_remote: bool = False) -> List[AuditRecord]:
        if prefer_remote:
            docs = self.firestore.list_documents(
                COLLECTION_AUDIT_LOGS, prefer_remote=True
            )
            for d in docs:
                if d.get("correlation_id"):
                    rec = AuditRecord.from_dict(d)
                    if not any(
                        r.correlation_id == rec.correlation_id for r in self._records
                    ):
                        self._records.append(rec)
        return [
            r
            for r in self._records
            if r.pdp_decision == "DENY" or r.outcome in ("DENIED", "BLOCKED")
        ]

    def list_remote_records(self, limit: int = 100) -> List[AuditRecord]:
        docs = self.firestore.list_documents(
            COLLECTION_AUDIT_LOGS, page_size=limit, prefer_remote=True
        )
        out: List[AuditRecord] = []
        for d in docs:
            if d.get("correlation_id"):
                out.append(AuditRecord.from_dict(d))
        return out[:limit]

    def clear(self) -> None:
        self._records.clear()
        if COLLECTION_AUDIT_LOGS in self.firestore._memory:
            self.firestore._memory[COLLECTION_AUDIT_LOGS].clear()


DEFAULT_AUDIT_LOGGER = AuditLogger()

