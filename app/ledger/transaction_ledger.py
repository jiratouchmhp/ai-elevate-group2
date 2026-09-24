"""Transaction Ledger & Saga Coordinator (SDD §1.3, §3.5, §3.6, FR-4.3, NFR-4.3).

Backed by in-memory state (Firestore-compatible schema) for MVP 1:
- Idempotency key generation and lookup (prevents duplicate writes on network timeouts)
- 5-minute duplicate ticket scan (TICKET_DEDUPE, FR-4.3)
- Cross-system Saga state machine (OPEN -> COMMITTED / PARTIALLY_COMPLETE -> COMPENSATED)
- HR Ops Reconciliation Queue for partial failures (§3.6)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import time
import uuid
from typing import Any, Dict, List, Optional


class SagaState(str, Enum):
    OPEN = "OPEN"
    COMMITTED = "COMMITTED"
    PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
    COMPENSATED = "COMPENSATED"
    FAILED = "FAILED"


@dataclass
class LedgerEntry:
    idempotency_key: str
    employee_id: str
    tool_name: str
    payload: Dict[str, Any]
    status: str  # PENDING, COMMITTED, FAILED, COMPENSATED
    backend_ref: Optional[str] = None
    created_at_epoch: float = field(default_factory=time.time)
    created_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SagaStepRecord:
    step_name: str
    tool_name: str
    status: str  # COMMITTED, FAILED, COMPENSATED
    backend_ref: Optional[str] = None
    error_detail: Optional[str] = None
    reversible_with_tool: Optional[str] = None


@dataclass
class SagaRecord:
    saga_id: str
    employee_id: str
    use_case: str
    state: SagaState = SagaState.OPEN
    steps: List[SagaStepRecord] = field(default_factory=list)
    reconciliation_task_id: Optional[str] = None
    created_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class HROpsReconciliationTask:
    task_id: str
    saga_id: str
    employee_id: str
    completed_steps: List[Dict[str, Any]]
    failed_step: Dict[str, Any]
    summary: str
    created_at_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class TransactionLedger:
    """Deterministic Transaction Ledger for idempotency, deduplication, and Sagas."""

    def __init__(self) -> None:
        self._entries: Dict[str, LedgerEntry] = {}
        self._sagas: Dict[str, SagaRecord] = {}
        self._hr_ops_queue: List[HROpsReconciliationTask] = []

    def generate_idempotency_key(
        self, employee_id: str, tool_name: str, payload: Dict[str, Any]
    ) -> str:
        canonical = f"{employee_id}:{tool_name}:{sorted(payload.items())}"
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"idem-{digest}"

    def get_entry(self, idempotency_key: str) -> Optional[LedgerEntry]:
        return self._entries.get(idempotency_key)

    def record_intent(
        self,
        *,
        idempotency_key: str,
        employee_id: str,
        tool_name: str,
        payload: Dict[str, Any],
    ) -> LedgerEntry:
        existing = self._entries.get(idempotency_key)
        if existing and existing.status == "COMMITTED":
            return existing
        entry = LedgerEntry(
            idempotency_key=idempotency_key,
            employee_id=employee_id,
            tool_name=tool_name,
            payload=dict(payload),
            status="PENDING",
        )
        self._entries[idempotency_key] = entry
        return entry

    def mark_committed(self, idempotency_key: str, backend_ref: str) -> LedgerEntry:
        entry = self._entries[idempotency_key]
        entry.status = "COMMITTED"
        entry.backend_ref = backend_ref
        return entry

    def mark_failed(self, idempotency_key: str) -> Optional[LedgerEntry]:
        entry = self._entries.get(idempotency_key)
        if entry:
            entry.status = "FAILED"
        return entry

    def find_recent_similar_ticket(
        self,
        *,
        employee_id: str,
        category: str,
        short_description: str,
        window_seconds: int = 300,
    ) -> Optional[LedgerEntry]:
        """Scans committed tickets within the 5-minute window to prevent duplicates (TICKET_DEDUPE)."""
        now = time.time()
        norm_desc = " ".join(short_description.lower().split())
        for entry in self._entries.values():
            if (
                entry.employee_id == employee_id
                and entry.tool_name in ("create_incident", "create_ticket")
                and entry.status == "COMMITTED"
                and (now - entry.created_at_epoch) <= window_seconds
            ):
                entry_cat = str(entry.payload.get("category", "")).lower()
                entry_desc = " ".join(
                    str(entry.payload.get("short_description", "")).lower().split()
                )
                if entry_cat == category.lower() and (
                    entry_desc == norm_desc
                    or (len(norm_desc) >= 8 and (norm_desc in entry_desc or entry_desc in norm_desc))
                ):
                    return entry
        return None

    # --- Saga & Compensation Management (SDD §3.6) ---

    def open_saga(self, employee_id: str, use_case: str, saga_id: Optional[str] = None) -> SagaRecord:
        sid = saga_id or f"SAGA-{uuid.uuid4().int % 9000 + 1000}"
        saga = SagaRecord(saga_id=sid, employee_id=employee_id, use_case=use_case)
        self._sagas[sid] = saga
        return saga

    def record_saga_step(
        self,
        saga_id: str,
        *,
        step_name: str,
        tool_name: str,
        status: str,
        backend_ref: Optional[str] = None,
        error_detail: Optional[str] = None,
        reversible_with_tool: Optional[str] = None,
    ) -> SagaRecord:
        saga = self._sagas[saga_id]
        saga.steps.append(
            SagaStepRecord(
                step_name=step_name,
                tool_name=tool_name,
                status=status,
                backend_ref=backend_ref,
                error_detail=error_detail,
                reversible_with_tool=reversible_with_tool,
            )
        )
        return saga

    def handle_partial_failure(
        self,
        saga_id: str,
        failed_step_name: str,
        failed_tool_name: str,
        error_detail: str,
    ) -> HROpsReconciliationTask:
        """Marks saga as PARTIALLY_COMPLETE and queues an HR Ops reconciliation task (§3.6).

        Crucially: does NOT auto-reverse committed steps because reversing without asking
        would violate B-3 (no autonomous write without confirmation).
        """
        saga = self._sagas[saga_id]
        saga.steps.append(
            SagaStepRecord(
                step_name=failed_step_name,
                tool_name=failed_tool_name,
                status="FAILED",
                error_detail=error_detail,
            )
        )
        saga.state = SagaState.PARTIALLY_COMPLETE

        completed = [
            asdict(s) for s in saga.steps if s.status == "COMMITTED"
        ]
        task_id = f"HROPS-{uuid.uuid4().hex[:8].upper()}"
        saga.reconciliation_task_id = task_id

        task = HROpsReconciliationTask(
            task_id=task_id,
            saga_id=saga_id,
            employee_id=saga.employee_id,
            completed_steps=completed,
            failed_step={
                "step_name": failed_step_name,
                "tool_name": failed_tool_name,
                "error": error_detail,
            },
            summary=(
                f"Partial completion in {saga.use_case} ({saga_id}): "
                f"{len(completed)} step(s) committed, '{failed_step_name}' failed ({error_detail}). "
                "Manual follow-up or user-confirmed compensation required."
            ),
        )
        self._hr_ops_queue.append(task)
        return task

    def mark_saga_committed(self, saga_id: str) -> SagaRecord:
        saga = self._sagas[saga_id]
        saga.state = SagaState.COMMITTED
        return saga

    def mark_saga_compensated(self, saga_id: str, compensated_ref: str) -> SagaRecord:
        saga = self._sagas[saga_id]
        saga.state = SagaState.COMPENSATED
        for step in saga.steps:
            if step.status == "COMMITTED":
                step.status = f"COMPENSATED ({compensated_ref})"
        return saga

    def get_saga(self, saga_id: str) -> Optional[SagaRecord]:
        return self._sagas.get(saga_id)

    @property
    def hr_ops_queue(self) -> List[HROpsReconciliationTask]:
        return list(self._hr_ops_queue)

    def clear(self) -> None:
        self._entries.clear()
        self._sagas.clear()
        self._hr_ops_queue.clear()


DEFAULT_LEDGER = TransactionLedger()
