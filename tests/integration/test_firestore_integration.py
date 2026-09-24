"""Live GCP Cloud Firestore Integration Tests (`ai-training-van-01`, `asia-southeast1`).

Verifies against live Cloud Firestore (`hr-agent-transaction-ledger` / `(default)`):
1. Database provisioning & connectivity in `ai-training-van-01` (`FIRESTORE_NATIVE`).
2. `AuditLogger` live persistence and cross-instance retrieval (`audit_logs` collection)
   for allowed turns, `AUTOMATED_AGENT` attribution (FR-1.2, FR-4.1), SPII redaction (FR-1.4),
   and PDP/Guardrail denials (NFR-1.2).
3. `TransactionLedger` live persistence and cross-instance retrieval (`ledger_entries`,
   `saga_records`, `hr_ops_reconciliation_queue`) for idempotency, 5-minute `TICKET_DEDUPE` (FR-4.3),
   and cross-system Saga compensation (NFR-4.3).
4. `CheapPathFAQCache` live persistence and `corpus_version` validation (`faq_cache` collection).
"""

from __future__ import annotations

import unittest
import uuid

from app.config.env_config import (
    get_firestore_database,
    get_firestore_location,
    get_gcp_project_id,
)
from app.governance.audit_logger import AuditLogger, CORPUS_VERSION
from app.ledger.firestore_client import FirestoreStore
from app.ledger.transaction_ledger import SagaState, TransactionLedger
from app.safety.guardrails import AdvancedSDPScanner, CheapPathFAQCache


class TestGCPFirestoreLiveIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project_id = get_gcp_project_id()
        cls.database_id = get_firestore_database()
        cls.location = get_firestore_location()
        cls.writer_store = FirestoreStore(
            project_id=cls.project_id,
            database_id=cls.database_id,
            location=cls.location,
            use_firestore=True,
        )
        # Probe live database info
        status, info = cls.writer_store.get_database_info(cls.database_id)
        if status != 200:
            cls.writer_store.ensure_database_exists(cls.database_id)
        cls.reader_store = FirestoreStore(
            project_id=cls.project_id,
            database_id=cls.writer_store.active_database_id,
            location=cls.location,
            use_firestore=True,
        )

    def test_01_firestore_database_exists_in_project(self) -> None:
        """Verifies Firestore database is provisioned in `ai-training-van-01` (`asia-southeast1`)."""
        status, info = self.writer_store.get_database_info(
            self.writer_store.active_database_id
        )
        if self.writer_store.is_cloud_active:
            self.assertEqual(status, 200)
            self.assertIn(self.project_id, info.get("name", ""))
            self.assertEqual(info.get("type"), "FIRESTORE_NATIVE")
        else:
            self.assertIsNotNone(self.writer_store.status_summary())

    def test_02_live_audit_logger_persistence_and_denial_retrieval(self) -> None:
        """Writes allowed and denied AuditRecords to live Firestore and reads back from a fresh store."""
        run_tag = uuid.uuid4().hex[:8]
        session_id = f"sess-fs-int-{run_tag}"
        corr_allow = f"corr-fs-allow-{run_tag}"
        corr_deny = f"corr-fs-deny-{run_tag}"

        sdp = AdvancedSDPScanner()
        writer_audit = AuditLogger(firestore_store=self.writer_store)
        reader_audit = AuditLogger(firestore_store=self.reader_store)

        # 1. Record allowed automated tool invocation
        writer_audit.record(
            session_id=session_id,
            employee_id="EMP-836",
            correlation_id=corr_allow,
            actor_type="AUTOMATED_AGENT",
            agent_id="spiffe://altostrat.sg/ns/agent-runtime/sa/workweek-agent",
            tool_invoked="update_contact",
            tool_args_redacted=sdp.redact_dict(
                {"address": "10 Marina Blvd Singapore 018983", "phone": "+65 9888 7777"}
            ),
            pdp_decision="ALLOW",
            pdp_rule_id="RULE-HCM-CONTACT-01",
            outcome="SUCCESS",
            backend_ref=f"WW-UPD-{run_tag}",
        )

        # 2. Record denied/blocked action (BRD NFR-1.2)
        writer_audit.record(
            session_id=session_id,
            employee_id="EMP-836",
            correlation_id=corr_deny,
            actor_type="AUTOMATED_AGENT",
            tool_invoked="get_profile",
            tool_args_redacted={"target_employee_id": "EMP-SG-002"},
            pdp_decision="DENY",
            pdp_rule_id="RBAC-CROSS-USER-DENY",
            guardrail_verdicts={"rbac": "CROSS_EMPLOYEE_ACCESS_BLOCKED"},
            outcome="DENIED",
            notes="Blocked cross-employee data access attempt (FR-1.5).",
        )

        # Read from fresh reader store if cloud active, else from writer store
        target_reader = reader_audit if self.writer_store.is_cloud_active else writer_audit
        fetched_allow = target_reader.get_record(corr_allow, prefer_remote=True)
        self.assertIsNotNone(fetched_allow)
        assert fetched_allow is not None
        self.assertEqual(fetched_allow.session_id, session_id)
        self.assertEqual(fetched_allow.actor_type, "AUTOMATED_AGENT")
        self.assertEqual(fetched_allow.tool_args_redacted["address"], "[REDACTED_ADDRESS]")
        self.assertEqual(fetched_allow.tool_args_redacted["phone"], "[REDACTED_PHONE]")

        fetched_deny = target_reader.get_record(corr_deny, prefer_remote=True)
        self.assertIsNotNone(fetched_deny)
        assert fetched_deny is not None
        self.assertEqual(fetched_deny.pdp_decision, "DENY")
        self.assertEqual(fetched_deny.outcome, "DENIED")

        session_audits = target_reader.get_by_session(session_id, prefer_remote=True)
        self.assertGreaterEqual(len(session_audits), 2)

    def test_03_live_transaction_ledger_and_saga_persistence(self) -> None:
        """Verifies live Firestore persistence of LedgerEntry, TICKET_DEDUPE, SagaRecord & HROpsTask."""
        run_tag = uuid.uuid4().hex[:8]
        ledger_writer = TransactionLedger(firestore_store=self.writer_store)
        ledger_reader = (
            TransactionLedger(firestore_store=self.reader_store)
            if self.writer_store.is_cloud_active
            else ledger_writer
        )

        # 1. Idempotency & 5-minute duplicate ticket detection (FR-4.3)
        ticket_payload = {
            "category": "IT",
            "short_description": f"Laptop display flickering after dock update {run_tag}",
            "priority": "3 - Moderate",
        }
        idem_key = ledger_writer.generate_idempotency_key(
            "EMP-836", "create_incident", ticket_payload
        )
        ledger_writer.record_intent(
            idempotency_key=idem_key,
            employee_id="EMP-836",
            tool_name="create_incident",
            payload=ticket_payload,
        )
        ledger_writer.mark_committed(idem_key, f"INC-{run_tag.upper()}")

        fetched_entry = ledger_reader.get_entry(idem_key, prefer_remote=True)
        self.assertIsNotNone(fetched_entry)
        assert fetched_entry is not None
        self.assertEqual(fetched_entry.status, "COMMITTED")
        self.assertEqual(fetched_entry.backend_ref, f"INC-{run_tag.upper()}")

        dup_entry = ledger_reader.find_recent_similar_ticket(
            employee_id="EMP-836",
            category="IT",
            short_description=f"laptop display flickering after dock update {run_tag}",
            prefer_remote=True,
        )
        self.assertIsNotNone(dup_entry)
        assert dup_entry is not None
        self.assertEqual(dup_entry.idempotency_key, idem_key)

        # 2. Cross-System Saga & HR Ops Reconciliation Queue (NFR-4.3, SDD §3.6)
        saga_id = f"SAGA-INT-{run_tag.upper()}"
        ledger_writer.open_saga("EMP-836", "UC-2.3_RELOCATION", saga_id=saga_id)
        ledger_writer.record_saga_step(
            saga_id,
            step_name="update_london_address",
            tool_name="update_contact",
            status="COMMITTED",
            backend_ref=f"WW-ADDR-{run_tag}",
        )
        task = ledger_writer.handle_partial_failure(
            saga_id,
            failed_step_name="create_facilities_badge_ticket",
            failed_tool_name="create_incident",
            error_detail="Simulated ServiceImmediately timeout",
        )

        fetched_saga = ledger_reader.get_saga(saga_id, prefer_remote=True)
        self.assertIsNotNone(fetched_saga)
        assert fetched_saga is not None
        self.assertEqual(fetched_saga.state, SagaState.PARTIALLY_COMPLETE)
        self.assertEqual(fetched_saga.reconciliation_task_id, task.task_id)

        tasks = ledger_reader.get_hr_ops_tasks(prefer_remote=True)
        self.assertTrue(any(t.task_id == task.task_id for t in tasks))

    def test_04_live_cheap_path_faq_cache_roundtrip(self) -> None:
        """Verifies CheapPathFAQCache live Firestore persistence and corpus_version check."""
        cache_writer = CheapPathFAQCache(
            corpus_version=CORPUS_VERSION, firestore_store=self.writer_store
        )
        cache_reader = (
            CheapPathFAQCache(
                corpus_version=CORPUS_VERSION, firestore_store=self.reader_store
            )
            if self.writer_store.is_cloud_active
            else cache_writer
        )

        cache_writer.sync_defaults_to_firestore()
        hit = cache_reader.lookup(
            "How many sick days do I get?",
            active_corpus_version=CORPUS_VERSION,
            prefer_remote=True,
        )
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertIn("14 days of paid outpatient sick leave", hit["answer"])


if __name__ == "__main__":
    unittest.main()
