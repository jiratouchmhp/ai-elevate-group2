"""Transaction Ledger — idempotency, intents, saga state, reconciliation (SDD §3.5, §3.6).

SQLite for the local MVP (Firestore in the target architecture). The ledger —
not the session — owns saga state so it survives session expiry (§3.9).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import timedelta
from pathlib import Path

from app import clock, config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
  intent_id TEXT PRIMARY KEY, session_id TEXT, employee_id TEXT, action TEXT,
  args TEXT, idempotency_key TEXT UNIQUE, status TEXT, created_at TEXT, backend_ref TEXT);
CREATE TABLE IF NOT EXISTS sagas (
  saga_id TEXT PRIMARY KEY, session_id TEXT UNIQUE, employee_id TEXT, state TEXT,
  steps TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS reconciliation_tasks (
  task_id TEXT PRIMARY KEY, saga_id TEXT, employee_id TEXT, summary TEXT, created_at TEXT, status TEXT);
"""


class Ledger:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = str(path or config.LEDGER_PATH)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript("DELETE FROM intents; DELETE FROM sagas; DELETE FROM reconciliation_tasks;")
            self._conn.commit()

    # --------------------------------------------------------------- intents
    def record_intent(self, session_id: str, employee_id: str, action: str, args: dict,
                      idempotency_key: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT intent_id FROM intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if row:
                return row["intent_id"]
            intent_id = f"INT-{uuid.uuid4().hex[:10]}"
            self._conn.execute(
                "INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)",
                (intent_id, session_id, employee_id, action, json.dumps(args), idempotency_key,
                 "PROPOSED", clock.now().isoformat(), None))
            self._conn.commit()
            return intent_id

    def intent_status(self, idempotency_key: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM intents WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def mark_intent(self, idempotency_key: str, status: str, backend_ref: str | None = None) -> None:
        with self._lock:
            self._conn.execute("UPDATE intents SET status=?, backend_ref=? WHERE idempotency_key=?",
                               (status, backend_ref, idempotency_key))
            self._conn.commit()

    # --------------------------------------------------------------- sagas
    def saga_for_session(self, session_id: str, employee_id: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sagas WHERE session_id=?", (session_id,)).fetchone()
            if row:
                d = dict(row)
                d["steps"] = json.loads(d["steps"])
                return d
            saga_id = f"SAGA-{uuid.uuid4().hex[:6].upper()}"
            self._conn.execute("INSERT INTO sagas VALUES (?,?,?,?,?,?)",
                               (saga_id, session_id, employee_id, "OPEN", "[]", clock.now().isoformat()))
            self._conn.commit()
            return {"saga_id": saga_id, "session_id": session_id, "employee_id": employee_id,
                    "state": "OPEN", "steps": []}

    def append_step(self, session_id: str, employee_id: str, step: dict) -> dict:
        with self._lock:
            saga = self.saga_for_session(session_id, employee_id)
            saga["steps"].append(step)
            state = _saga_state(saga["steps"], step)
            saga["state"] = state
            self._conn.execute("UPDATE sagas SET state=?, steps=?, updated_at=? WHERE saga_id=?",
                               (state, json.dumps(saga["steps"]), clock.now().isoformat(), saga["saga_id"]))
            self._conn.commit()
            return saga

    def open_reconciliation(self, saga_id: str, employee_id: str, summary: str) -> str:
        with self._lock:
            task_id = f"HROPS-{uuid.uuid4().hex[:6].upper()}"
            self._conn.execute("INSERT INTO reconciliation_tasks VALUES (?,?,?,?,?,?)",
                               (task_id, saga_id, employee_id, summary, clock.now().isoformat(), "OPEN"))
            self._conn.commit()
            return task_id

    def reconciliation_tasks(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM reconciliation_tasks").fetchall()]


def _saga_state(steps: list[dict], step: dict) -> str:
    committed = [s for s in steps if s["status"] == "COMMITTED"]
    failed = [s for s in steps if s["status"] == "FAILED"]
    compensated = [s for s in steps if s.get("compensation")]
    if failed and committed:
        return "COMPENSATED" if compensated and step.get("compensation") else "PARTIALLY_COMPLETE"
    if failed:
        return "FAILED"
    return "COMMITTED"


class FirestoreLedger:  # pragma: no cover - exercised against a real Firestore database
    """Same contract as `Ledger`, backed by Firestore (SDD §1.3 target: 'Transaction Ledger · Firestore').

    Collections: `hr_intents` (doc id = idempotency key), `hr_sagas` (doc id = session id),
    `hr_reconciliation_tasks`. Idempotency and saga updates use transactions so concurrent
    commits of the same intent cannot both succeed.
    """

    def __init__(self, project: str | None = None, database: str | None = None) -> None:
        from google.cloud import firestore

        self._fs = firestore
        self._db = firestore.Client(project=project, database=database or config.FIRESTORE_DATABASE)
        self._intents = self._db.collection("hr_intents")
        self._sagas = self._db.collection("hr_sagas")
        self._tasks = self._db.collection("hr_reconciliation_tasks")

    def reset(self) -> None:
        raise RuntimeError("Refusing to wipe a Firestore ledger; delete collections deliberately if needed.")

    def record_intent(self, session_id: str, employee_id: str, action: str, args: dict,
                      idempotency_key: str) -> str:
        ref = self._intents.document(idempotency_key)
        intent_id = f"INT-{uuid.uuid4().hex[:10]}"

        @self._fs.transactional
        def txn(tx):
            snap = ref.get(transaction=tx)
            if snap.exists:
                return snap.get("intent_id")
            tx.set(ref, {"intent_id": intent_id, "session_id": session_id, "employee_id": employee_id,
                         "action": action, "args": json.dumps(args), "idempotency_key": idempotency_key,
                         "status": "PROPOSED", "created_at": clock.now().isoformat(), "backend_ref": None,
                         "expires_at": clock.now() + timedelta(days=30)})
            return intent_id

        return txn(self._db.transaction())

    def intent_status(self, idempotency_key: str) -> dict | None:
        snap = self._intents.document(idempotency_key).get()
        if not snap.exists:
            return None
        d = snap.to_dict()
        d.pop("expires_at", None)
        return d

    def mark_intent(self, idempotency_key: str, status: str, backend_ref: str | None = None) -> None:
        self._intents.document(idempotency_key).set({"status": status, "backend_ref": backend_ref}, merge=True)

    def saga_for_session(self, session_id: str, employee_id: str) -> dict:
        ref = self._sagas.document(session_id)
        snap = ref.get()
        if snap.exists:
            d = snap.to_dict()
            d["steps"] = json.loads(d["steps"])
            return d
        saga = {"saga_id": f"SAGA-{uuid.uuid4().hex[:6].upper()}", "session_id": session_id,
                "employee_id": employee_id, "state": "OPEN", "steps": "[]", "updated_at": clock.now().isoformat()}
        ref.create(saga)
        return saga | {"steps": []}

    def append_step(self, session_id: str, employee_id: str, step: dict) -> dict:
        self.saga_for_session(session_id, employee_id)
        ref = self._sagas.document(session_id)

        @self._fs.transactional
        def txn(tx):
            d = ref.get(transaction=tx).to_dict()
            steps = [*json.loads(d["steps"]), step]
            state = _saga_state(steps, step)
            tx.update(ref, {"steps": json.dumps(steps), "state": state, "updated_at": clock.now().isoformat()})
            return d | {"steps": steps, "state": state}

        return txn(self._db.transaction())

    def open_reconciliation(self, saga_id: str, employee_id: str, summary: str) -> str:
        task_id = f"HROPS-{uuid.uuid4().hex[:6].upper()}"
        self._tasks.document(task_id).set({"task_id": task_id, "saga_id": saga_id, "employee_id": employee_id,
                                           "summary": summary, "created_at": clock.now().isoformat(),
                                           "status": "OPEN"})
        return task_id

    def reconciliation_tasks(self) -> list[dict]:
        return [d.to_dict() for d in self._tasks.stream()]


_LEDGER: Ledger | FirestoreLedger | None = None


def get_ledger() -> Ledger | FirestoreLedger:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = FirestoreLedger() if config.LEDGER_BACKEND == "firestore" else Ledger()
    return _LEDGER
