"""Transaction Ledger, Audit Persistence & Saga Coordinator (SDD §1.3, §3.5, §3.6, FR-4.3, NFR-1.2, NFR-4.3).

Backed by live GCP Cloud Firestore (`ai-training-van-01`, `asia-southeast1`) with automatic
in-memory write-through and fallback when offline or in isolated unit tests:
- Idempotency key generation and lookup (`ledger_entries` collection — prevents duplicate writes on timeouts)
- 5-minute duplicate ticket scan (`TICKET_DEDUPE`, FR-4.3) across local and Firestore state
- Cross-system Saga state machine (`saga_records` collection — OPEN -> COMMITTED / PARTIALLY_COMPLETE -> COMPENSATED)
- HR Ops Reconciliation Queue (`hr_ops_reconciliation_queue` collection) for partial failures (§3.6)
- Direct Audit Record access (`audit_logs` collection) for BDD/BRD audit verification (NFR-1.2, FR-1.2, FR-4.1)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import time
import uuid
from typing import Any, Dict, List, Optional

from app.ledger.firestore_client import (
    COLLECTION_AUDIT_LOGS,
    COLLECTION_HR_OPS_QUEUE,
    COLLECTION_LEDGER_ENTRIES,
    COLLECTION_SAGA_RECORDS,
    DEFAULT_FIRESTORE_STORE,
    FirestoreStore,
)


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LedgerEntry":
        return cls(
            idempotency_key=str(data.get("idempotency_key", "")),
            employee_id=str(data.get("employee_id", "")),
            tool_name=str(data.get("tool_name", "")),
            payload=dict(data.get("payload") or {}),
            status=str(data.get("status", "PENDING")),
            backend_ref=data.get("backend_ref"),
            created_at_epoch=float(data.get("created_at_epoch") or time.time()),
            created_at_iso=str(
                data.get("created_at_iso") or datetime.now(timezone.utc).isoformat()
            ),
        )


@dataclass
class SagaStepRecord:
    step_name: str
    tool_name: str
    status: str  # COMMITTED, FAILED, COMPENSATED
    backend_ref: Optional[str] = None
    error_detail: Optional[str] = None
    reversible_with_tool: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SagaStepRecord":
        return cls(
            step_name=str(data.get("step_name", "")),
            tool_name=str(data.get("tool_name", "")),
            status=str(data.get("status", "COMMITTED")),
            backend_ref=data.get("backend_ref"),
            error_detail=data.get("error_detail"),
            reversible_with_tool=data.get("reversible_with_tool"),
        )


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
        d["state"] = self.state.value if isinstance(self.state, SagaState) else str(self.state)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SagaRecord":
        raw_state = str(data.get("state", SagaState.OPEN.value))
        try:
            state_enum = SagaState(raw_state)
        except ValueError:
            state_enum = SagaState.OPEN
        raw_steps = data.get("steps") or []
        steps = [
            s if isinstance(s, SagaStepRecord) else SagaStepRecord.from_dict(dict(s))
            for s in raw_steps
        ]
        return cls(
            saga_id=str(data.get("saga_id", "")),
            employee_id=str(data.get("employee_id", "")),
            use_case=str(data.get("use_case", "")),
            state=state_enum,
            steps=steps,
            reconciliation_task_id=data.get("reconciliation_task_id"),
            created_at_iso=str(
                data.get("created_at_iso") or datetime.now(timezone.utc).isoformat()
            ),
        )


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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HROpsReconciliationTask":
        return cls(
            task_id=str(data.get("task_id", "")),
            saga_id=str(data.get("saga_id", "")),
            employee_id=str(data.get("employee_id", "")),
            completed_steps=[dict(x) for x in (data.get("completed_steps") or [])],
            failed_step=dict(data.get("failed_step") or {}),
            summary=str(data.get("summary", "")),
            created_at_iso=str(
                data.get("created_at_iso") or datetime.now(timezone.utc).isoformat()
            ),
        )


class TransactionLedger:
    """Deterministic Transaction Ledger & Saga Coordinator backed by GCP Cloud Firestore."""

    def __init__(
        self,
        firestore_store: Optional[FirestoreStore] = None,
        *,
        auto_hydrate_remote: bool = False,
    ) -> None:
        self._entries: Dict[str, LedgerEntry] = {}
        self._sagas: Dict[str, SagaRecord] = {}
        self._hr_ops_queue: List[HROpsReconciliationTask] = []
        self.firestore: FirestoreStore = firestore_store or DEFAULT_FIRESTORE_STORE
        self.auto_hydrate_remote: bool = auto_hydrate_remote

    def generate_idempotency_key(
        self, employee_id: str, tool_name: str, payload: Dict[str, Any]
    ) -> str:
        canonical = f"{employee_id}:{tool_name}:{sorted(payload.items())}"
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"idem-{digest}"

    def get_entry(
        self, idempotency_key: str, *, prefer_remote: bool = False
    ) -> Optional[LedgerEntry]:
        if not prefer_remote and idempotency_key in self._entries:
            return self._entries[idempotency_key]

        if prefer_remote or self.auto_hydrate_remote:
            doc = self.firestore.get_document(
                COLLECTION_LEDGER_ENTRIES,
                idempotency_key,
                prefer_remote=prefer_remote,
            )
            if doc and doc.get("idempotency_key"):
                entry = LedgerEntry.from_dict(doc)
                self._entries[idempotency_key] = entry
                return entry
        return self._entries.get(idempotency_key)

    def record_intent(
        self,
        *,
        idempotency_key: str,
        employee_id: str,
        tool_name: str,
        payload: Dict[str, Any],
    ) -> LedgerEntry:
        existing = self.get_entry(idempotency_key)
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
        self.firestore.upsert_document(
            COLLECTION_LEDGER_ENTRIES, idempotency_key, entry.to_dict()
        )
        return entry

    def mark_committed(self, idempotency_key: str, backend_ref: str) -> LedgerEntry:
        entry = self.get_entry(idempotency_key)
        if not entry:
            raise KeyError(f"Unknown idempotency_key: {idempotency_key}")
        entry.status = "COMMITTED"
        entry.backend_ref = backend_ref
        self._entries[idempotency_key] = entry
        self.firestore.upsert_document(
            COLLECTION_LEDGER_ENTRIES, idempotency_key, entry.to_dict()
        )
        return entry

    def mark_failed(self, idempotency_key: str) -> Optional[LedgerEntry]:
        entry = self.get_entry(idempotency_key)
        if entry:
            entry.status = "FAILED"
            self._entries[idempotency_key] = entry
            self.firestore.upsert_document(
                COLLECTION_LEDGER_ENTRIES, idempotency_key, entry.to_dict()
            )
        return entry

    def find_recent_similar_ticket(
        self,
        *,
        employee_id: str,
        category: str,
        short_description: str,
        window_seconds: int = 300,
        prefer_remote: bool = False,
    ) -> Optional[LedgerEntry]:
        """Scans committed tickets within the 5-minute window to prevent duplicates (TICKET_DEDUPE, FR-4.3)."""
        if prefer_remote:
            remote_docs = self.firestore.query_documents(
                COLLECTION_LEDGER_ENTRIES,
                field_equals={"employee_id": employee_id, "status": "COMMITTED"},
                prefer_remote=True,
            )
            for d in remote_docs:
                if d.get("idempotency_key"):
                    ent = LedgerEntry.from_dict(d)
                    self._entries[ent.idempotency_key] = ent

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
                    or (
                        len(norm_desc) >= 8
                        and (norm_desc in entry_desc or entry_desc in norm_desc)
                    )
                ):
                    return entry
        return None

    # --- Saga & Compensation Management (SDD §3.6, NFR-4.3) ---

    def open_saga(
        self, employee_id: str, use_case: str, saga_id: Optional[str] = None
    ) -> SagaRecord:
        sid = saga_id or f"SAGA-{uuid.uuid4().int % 9000 + 1000}"
        saga = SagaRecord(saga_id=sid, employee_id=employee_id, use_case=use_case)
        self._sagas[sid] = saga
        self.firestore.upsert_document(COLLECTION_SAGA_RECORDS, sid, saga.to_dict())
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
        saga = self.get_saga(saga_id)
        if not saga:
            raise KeyError(f"Unknown saga_id: {saga_id}")
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
        self._sagas[saga_id] = saga
        self.firestore.upsert_document(COLLECTION_SAGA_RECORDS, saga_id, saga.to_dict())
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
        saga = self.get_saga(saga_id)
        if not saga:
            raise KeyError(f"Unknown saga_id: {saga_id}")
        saga.steps.append(
            SagaStepRecord(
                step_name=failed_step_name,
                tool_name=failed_tool_name,
                status="FAILED",
                error_detail=error_detail,
            )
        )
        saga.state = SagaState.PARTIALLY_COMPLETE

        completed = [asdict(s) for s in saga.steps if s.status == "COMMITTED"]
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
        self._sagas[saga_id] = saga
        self._hr_ops_queue.append(task)
        self.firestore.upsert_document(COLLECTION_SAGA_RECORDS, saga_id, saga.to_dict())
        self.firestore.upsert_document(COLLECTION_HR_OPS_QUEUE, task_id, task.to_dict())
        return task

    def mark_saga_committed(self, saga_id: str) -> SagaRecord:
        saga = self.get_saga(saga_id)
        if not saga:
            raise KeyError(f"Unknown saga_id: {saga_id}")
        saga.state = SagaState.COMMITTED
        self._sagas[saga_id] = saga
        self.firestore.upsert_document(COLLECTION_SAGA_RECORDS, saga_id, saga.to_dict())
        return saga

    def mark_saga_compensated(self, saga_id: str, compensated_ref: str) -> SagaRecord:
        saga = self.get_saga(saga_id)
        if not saga:
            raise KeyError(f"Unknown saga_id: {saga_id}")
        saga.state = SagaState.COMPENSATED
        for step in saga.steps:
            if step.status == "COMMITTED":
                step.status = f"COMPENSATED ({compensated_ref})"
        self._sagas[saga_id] = saga
        self.firestore.upsert_document(COLLECTION_SAGA_RECORDS, saga_id, saga.to_dict())
        return saga

    def get_saga(
        self, saga_id: str, *, prefer_remote: bool = False
    ) -> Optional[SagaRecord]:
        if not prefer_remote and saga_id in self._sagas:
            return self._sagas[saga_id]

        doc = self.firestore.get_document(
            COLLECTION_SAGA_RECORDS, saga_id, prefer_remote=prefer_remote
        )
        if doc and doc.get("saga_id"):
            saga = SagaRecord.from_dict(doc)
            self._sagas[saga_id] = saga
            return saga
        return self._sagas.get(saga_id)

    def get_hr_ops_tasks(
        self, *, prefer_remote: bool = False
    ) -> List[HROpsReconciliationTask]:
        if prefer_remote:
            docs = self.firestore.list_documents(
                COLLECTION_HR_OPS_QUEUE, prefer_remote=True
            )
            by_id: Dict[str, HROpsReconciliationTask] = {
                t.task_id: t for t in self._hr_ops_queue
            }
            for d in docs:
                if d.get("task_id"):
                    task = HROpsReconciliationTask.from_dict(d)
                    by_id[task.task_id] = task
            self._hr_ops_queue = list(by_id.values())
        return list(self._hr_ops_queue)

    @property
    def hr_ops_queue(self) -> List[HROpsReconciliationTask]:
        return list(self._hr_ops_queue)

    # --- Direct Audit Log Access on Firestore (BDD / BRD NFR-1.2, FR-1.2, FR-4.1) ---

    def get_audit_document(
        self, correlation_id: str, *, prefer_remote: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Fetches an audit record document from Firestore `audit_logs` by `correlation_id`."""
        return self.firestore.get_document(
            COLLECTION_AUDIT_LOGS, correlation_id, prefer_remote=prefer_remote
        )

    def list_audit_documents(
        self,
        *,
        session_id: Optional[str] = None,
        employee_id: Optional[str] = None,
        prefer_remote: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Lists or filters audit documents from Firestore `audit_logs`."""
        filters: Dict[str, Any] = {}
        if session_id:
            filters["session_id"] = session_id
        if employee_id:
            filters["employee_id"] = employee_id
        return self.firestore.query_documents(
            COLLECTION_AUDIT_LOGS,
            field_equals=filters or None,
            prefer_remote=prefer_remote,
            limit=limit,
        )

    def clear(self) -> None:
        self._entries.clear()
        self._sagas.clear()
        self._hr_ops_queue.clear()
        for coll in (
            COLLECTION_LEDGER_ENTRIES,
            COLLECTION_SAGA_RECORDS,
            COLLECTION_HR_OPS_QUEUE,
        ):
            if coll in self.firestore._memory:
                self.firestore._memory[coll].clear()


DEFAULT_LEDGER = TransactionLedger()
