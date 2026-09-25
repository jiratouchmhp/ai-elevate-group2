"""Unit Tests for Cloud Firestore Audit Logging, Transaction Ledger, Saga Coordinator & FAQ Cache.

Covers:
1. Bidirectional Firestore Value serialization (`to_firestore_fields` / `from_firestore_fields`).
2. `AuditLogger` persistence to Firestore `audit_logs` for allowed actions, `AUTOMATED_AGENT` origin
   attribution (FR-1.2, FR-4.1), SPII redaction (FR-1.4), and PDP/Guardrail denials (NFR-1.2).
3. `TransactionLedger` write-through and cross-instance hydration for idempotency (`ledger_entries`),
   5-minute duplicate ticket prevention (`TICKET_DEDUPE`, FR-4.3), `SagaRecord` (`saga_records`),
   and `HROpsReconciliationTask` (`hr_ops_reconciliation_queue`, NFR-4.3).
4. `CheapPathFAQCache` Firestore `faq_cache` persistence and `corpus_version` invalidation (SDD §3.2).
5. Automatic circuit-breaker and fallback to in-memory mode when offline or unreachable.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import unittest
from urllib.parse import unquote

from app.governance.audit_logger import AuditLogger, CORPUS_VERSION
from app.ledger.firestore_client import (
    COLLECTION_AUDIT_LOGS,
    COLLECTION_FAQ_CACHE,
    COLLECTION_HR_OPS_QUEUE,
    COLLECTION_LEDGER_ENTRIES,
    COLLECTION_SAGA_RECORDS,
    FirestoreStore,
    from_firestore_fields,
    to_firestore_fields,
)
from app.ledger.transaction_ledger import SagaState, TransactionLedger
from app.safety.guardrails import AdvancedSDPScanner, CheapPathFAQCache


class InMemoryFirestoreRESTSimulator:
    """Simulates the GCP Cloud Firestore v1 REST API (`PATCH`, `GET`, `DELETE`) for deterministic unit tests."""

    def __init__(self) -> None:
        self.documents: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.requests: list[Tuple[str, str]] = []
        self.fail_mode: bool = False

    def handle_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        json_body: Optional[Dict[str, Any]],
    ) -> Tuple[int, Dict[str, Any]]:
        self.requests.append((method, url))
        if self.fail_mode:
            raise ConnectionError("Simulated Cloud Firestore network outage")

        # URL format: .../documents/{collection}/{doc_id} or .../documents/{collection}?pageSize=...
        if "/documents/" not in url:
            return 200, {"name": "projects/ai-training-van-01/databases/hr-agent-transaction-ledger"}

        tail = url.split("/documents/", 1)[1]
        path_part = tail.split("?", 1)[0]
        parts = [unquote(p) for p in path_part.split("/") if p]

        if method == "PATCH" and len(parts) == 2:
            coll, doc_id = parts
            doc_name = f"projects/ai-training-van-01/databases/hr-agent-transaction-ledger/documents/{coll}/{doc_id}"
            stored = {
                "name": doc_name,
                "fields": (json_body or {}).get("fields", {}),
            }
            self.documents.setdefault(coll, {})[doc_id] = stored
            return 200, stored

        if method == "GET" and len(parts) == 2:
            coll, doc_id = parts
            doc = self.documents.get(coll, {}).get(doc_id)
            if not doc:
                return 404, {"error": {"code": 404, "message": "Document not found"}}
            return 200, doc

        if method == "GET" and len(parts) == 1:
            coll = parts[0]
            docs = list(self.documents.get(coll, {}).values())
            return 200, {"documents": docs}

        if method == "DELETE" and len(parts) == 2:
            coll, doc_id = parts
            self.documents.get(coll, {}).pop(doc_id, None)
            return 200, {}

        return 400, {"error": "Unsupported simulator request"}


class TestFirestoreAuditAndLedgerUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.sim = InMemoryFirestoreRESTSimulator()
        self.store_writer = FirestoreStore(
            project_id="ai-training-van-01",
            database_id="hr-agent-transaction-ledger",
            location="asia-southeast1",
            use_firestore=True,
            http_send=self.sim.handle_request,
        )
        # Separate store instance simulating a second Cloud Run container reading from Firestore
        self.store_reader = FirestoreStore(
            project_id="ai-training-van-01",
            database_id="hr-agent-transaction-ledger",
            location="asia-southeast1",
            use_firestore=True,
            http_send=self.sim.handle_request,
        )

    def test_firestore_value_serialization_roundtrip(self) -> None:
        """Verifies nested dicts, lists, booleans, ints, floats, enums, and None round-trip cleanly."""
        original = {
            "correlation_id": "corr-unit-001",
            "employee_id": "EMP-836",
            "count": 42,
            "score": 0.985,
            "blocked": False,
            "tags": ["audit", "firestore", "bdd"],
            "nested": {"state": SagaState.COMMITTED, "nullable": None},
        }
        fs_fields = to_firestore_fields(original)
        self.assertEqual(fs_fields["correlation_id"], {"stringValue": "corr-unit-001"})
        self.assertEqual(fs_fields["count"], {"integerValue": "42"})
        self.assertEqual(fs_fields["blocked"], {"booleanValue": False})

        decoded = from_firestore_fields(fs_fields)
        self.assertEqual(decoded["correlation_id"], "corr-unit-001")
        self.assertEqual(decoded["count"], 42)
        self.assertAlmostEqual(decoded["score"], 0.985)
        self.assertFalse(decoded["blocked"])
        self.assertEqual(decoded["tags"], ["audit", "firestore", "bdd"])
        self.assertEqual(decoded["nested"]["state"], "COMMITTED")
        self.assertIsNone(decoded["nested"]["nullable"])

    def test_audit_logger_persists_allowed_and_denied_actions_to_firestore(self) -> None:
        """Verifies BRD NFR-1.2, FR-1.2, FR-1.4, FR-4.1 audit records persist to `audit_logs`."""
        sdp = AdvancedSDPScanner()
        writer_audit = AuditLogger(firestore_store=self.store_writer)
        reader_audit = AuditLogger(firestore_store=self.store_reader)

        # 1. Allowed automated tool call with SPII redaction
        redacted_args = sdp.redact_dict(
            {
                "employee_id": "EMP-836",
                "phone": "+65 9123 4567",
                "nric": "S1234567A",
                "category": "IT",
            }
        )
        allow_rec = writer_audit.record(
            session_id="sess-unit-audit-01",
            employee_id="EMP-836",
            correlation_id="corr-allow-001",
            actor_type="AUTOMATED_AGENT",
            agent_id="spiffe://altostrat.sg/ns/agent-runtime/sa/service-immediately-agent",
            tool_invoked="create_incident",
            tool_args_redacted=redacted_args,
            pdp_decision="ALLOW",
            pdp_rule_id="RULE-ITSM-CREATE-01",
            outcome="SUCCESS",
            backend_ref="INC-99001",
        )

        # 2. Denied / Blocked attempt (NFR-1.2 requires logging all denials)
        deny_rec = writer_audit.record(
            session_id="sess-unit-audit-01",
            employee_id="EMP-836",
            correlation_id="corr-deny-002",
            actor_type="AUTOMATED_AGENT",
            tool_invoked="submit_leave",
            tool_args_redacted={"leave_type": "Vacation", "days": 99},
            pdp_decision="DENY",
            pdp_rule_id="PDP-LEAVE-BALANCE-EXCEEDED",
            guardrail_verdicts={"input_scan": "CLEAN", "pdp": "DENY"},
            outcome="DENIED",
            notes="Leave request exceeded remaining balance (FR-3.3).",
        )

        # Verify second instance (`reader_audit`) can fetch both from Firestore `audit_logs`
        remote_allow = reader_audit.get_record(allow_rec.correlation_id, prefer_remote=True)
        self.assertIsNotNone(remote_allow)
        assert remote_allow is not None
        self.assertEqual(remote_allow.actor_type, "AUTOMATED_AGENT")
        self.assertEqual(remote_allow.backend_ref, "INC-99001")
        self.assertEqual(remote_allow.tool_args_redacted["phone"], "[REDACTED_PHONE]")
        self.assertEqual(remote_allow.tool_args_redacted["nric"], "[REDACTED_SG_NRIC]")

        session_records = reader_audit.get_by_session("sess-unit-audit-01", prefer_remote=True)
        self.assertEqual(len(session_records), 2)

        denials = reader_audit.get_denials(prefer_remote=True)
        self.assertEqual(len(denials), 1)
        self.assertEqual(denials[0].correlation_id, deny_rec.correlation_id)
        self.assertEqual(denials[0].pdp_decision, "DENY")

    def test_transaction_ledger_idempotency_and_ticket_dedupe_across_instances(self) -> None:
        """Verifies `ledger_entries` idempotency and 5-minute duplicate scan (FR-4.3) via Firestore."""
        ledger_a = TransactionLedger(firestore_store=self.store_writer)
        ledger_b = TransactionLedger(firestore_store=self.store_reader)

        payload = {
            "category": "Hardware",
            "short_description": "VPN connection keeps dropping every 10 minutes",
            "priority": "3 - Moderate",
        }
        idem_key = ledger_a.generate_idempotency_key("EMP-836", "create_incident", payload)
        ledger_a.record_intent(
            idempotency_key=idem_key,
            employee_id="EMP-836",
            tool_name="create_incident",
            payload=payload,
        )
        ledger_a.mark_committed(idem_key, "INC-778899")

        # Instance B reads committed entry from Firestore
        remote_entry = ledger_b.get_entry(idem_key, prefer_remote=True)
        self.assertIsNotNone(remote_entry)
        assert remote_entry is not None
        self.assertEqual(remote_entry.status, "COMMITTED")
        self.assertEqual(remote_entry.backend_ref, "INC-778899")

        # Instance B detects 5-minute duplicate ticket from Firestore (FR-4.3 TICKET_DEDUPE)
        dup = ledger_b.find_recent_similar_ticket(
            employee_id="EMP-836",
            category="Hardware",
            short_description="vpn connection keeps dropping every 10 minutes",
            prefer_remote=True,
        )
        self.assertIsNotNone(dup)
        assert dup is not None
        self.assertEqual(dup.backend_ref, "INC-778899")

    def test_saga_state_machine_and_hr_ops_reconciliation_in_firestore(self) -> None:
        """Verifies cross-system Saga and HROpsReconciliationTask persist to Firestore (NFR-4.3, §3.6)."""
        ledger_a = TransactionLedger(firestore_store=self.store_writer)
        ledger_b = TransactionLedger(firestore_store=self.store_reader)

        saga = ledger_a.open_saga("EMP-836", "UC-2.2_MEDICAL_LEAVE", saga_id="SAGA-UNIT-202")
        ledger_a.record_saga_step(
            saga.saga_id,
            step_name="submit_medical_leave",
            tool_name="submit_leave",
            status="COMMITTED",
            backend_ref="LV-5001",
            reversible_with_tool="cancel_leave",
        )
        task = ledger_a.handle_partial_failure(
            saga.saga_id,
            failed_step_name="create_email_routing_ticket",
            failed_tool_name="create_incident",
            error_detail="ServiceImmediately 503 Unavailable",
        )

        # Verify Saga and HR Ops Task from second instance via Firestore
        remote_saga = ledger_b.get_saga("SAGA-UNIT-202", prefer_remote=True)
        self.assertIsNotNone(remote_saga)
        assert remote_saga is not None
        self.assertEqual(remote_saga.state, SagaState.PARTIALLY_COMPLETE)
        self.assertEqual(remote_saga.reconciliation_task_id, task.task_id)
        self.assertEqual(len(remote_saga.steps), 2)

        remote_tasks = ledger_b.get_hr_ops_tasks(prefer_remote=True)
        self.assertTrue(any(t.task_id == task.task_id for t in remote_tasks))

        # User-confirmed compensation updates Firestore state to COMPENSATED
        ledger_a.mark_saga_compensated("SAGA-UNIT-202", "CANCEL-LV-5001")
        compensated = ledger_b.get_saga("SAGA-UNIT-202", prefer_remote=True)
        assert compensated is not None
        self.assertEqual(compensated.state, SagaState.COMPENSATED)

    def test_cheap_path_faq_cache_firestore_persistence_and_corpus_invalidation(self) -> None:
        """Verifies `CheapPathFAQCache` persists to `faq_cache` and invalidates on `corpus_version` change."""
        cache_a = CheapPathFAQCache(
            corpus_version=CORPUS_VERSION, firestore_store=self.store_writer
        )
        cache_b = CheapPathFAQCache(
            corpus_version=CORPUS_VERSION, firestore_store=self.store_reader
        )

        cache_a.upsert_faq(
            "What is the bereavement leave allowance?",
            "Eligible employees receive up to 5 paid work days of bereavement leave.",
            [{"section_number": "19.4", "section_title": "Bereavement Leave"}],
        )

        hit = cache_b.lookup(
            "What is the bereavement leave allowance?!",
            active_corpus_version=CORPUS_VERSION,
            prefer_remote=True,
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertIn("5 paid work days", hit["answer"])

        # Stale corpus_version must return None (SDD §3.2)
        stale_hit = cache_b.lookup(
            "What is the bereavement leave allowance?",
            active_corpus_version="2099-01-future-corpus-v99",
            prefer_remote=True,
        )
        self.assertIsNone(stale_hit)

    def test_automatic_fallback_to_in_memory_when_firestore_offline(self) -> None:
        """Verifies zero exceptions and full local functionality when Firestore network fails."""
        self.sim.fail_mode = True
        offline_store = FirestoreStore(
            project_id="ai-training-van-01",
            database_id="hr-agent-transaction-ledger",
            use_firestore=True,
            http_send=self.sim.handle_request,
        )
        audit = AuditLogger(firestore_store=offline_store)
        ledger = TransactionLedger(firestore_store=offline_store)

        rec = audit.record(
            session_id="sess-offline",
            employee_id="EMP-836",
            tool_invoked="get_profile",
            pdp_decision="ALLOW",
            outcome="SUCCESS",
        )
        self.assertFalse(offline_store.is_cloud_active)
        self.assertIsNotNone(audit.get_record(rec.correlation_id))

        idem = ledger.generate_idempotency_key("EMP-836", "submit_leave", {"days": 1})
        ledger.record_intent(
            idempotency_key=idem,
            employee_id="EMP-836",
            tool_name="submit_leave",
            payload={"days": 1},
        )
        ledger.mark_committed(idem, "LV-LOCAL-01")
        self.assertEqual(ledger.get_entry(idem).backend_ref, "LV-LOCAL-01")

    def test_audit_logs_sorted_by_descending_timestamp_and_payload(self) -> None:
        """Verifies `list_remote_records` and `build_audit_payload` sort records by descending timestamp."""
        from app.governance.audit_logger import AuditRecord
        from app.ui.ag_ui_server import build_audit_payload

        writer_audit = AuditLogger(firestore_store=self.store_writer)
        reader_audit = AuditLogger(firestore_store=self.store_reader)

        # Insert three audit records with distinct ISO-8601 timestamps out of order
        for corr_id, ts in [
            ("corr-ts-middle", "2026-09-25T01:15:00+00:00"),
            ("corr-ts-oldest", "2026-09-25T00:05:00+00:00"),
            ("corr-ts-newest", "2026-09-25T02:30:00+00:00"),
        ]:
            rec = AuditRecord(
                correlation_id=corr_id,
                session_id="sess-sort-test",
                employee_id="EMP-836",
                tool_invoked="get_leave_balance",
                pdp_decision="ALLOW",
                outcome="SUCCESS",
                timestamp=ts,
            )
            writer_audit._records.append(rec)
            self.store_writer.upsert_document(
                COLLECTION_AUDIT_LOGS, corr_id, rec.to_dict()
            )

        remote_sorted = reader_audit.list_remote_records(limit=50)
        self.assertEqual(
            [r.correlation_id for r in remote_sorted[:3]],
            ["corr-ts-newest", "corr-ts-middle", "corr-ts-oldest"],
        )

        payload = build_audit_payload(prefer_remote=False)
        self.assertEqual(payload.get("sort_order"), "timestamp_desc")
        self.assertEqual(payload.get("collection"), "audit_logs")


if __name__ == "__main__":
    unittest.main()

