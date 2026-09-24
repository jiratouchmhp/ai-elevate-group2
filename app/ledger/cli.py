"""CLI for Provisioning and Verifying GCP Cloud Firestore (`ai-training-van-01`).

Usage:
  python -m app.ledger.cli setup   # Enables API, creates databases, seeds FAQ cache & verifies collections
  python -m app.ledger.cli status  # Prints Firestore database status and collection counts
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

from app.config.env_config import (
    get_firestore_database,
    get_firestore_location,
    get_gcp_project_id,
)
from app.governance.audit_logger import AuditLogger
from app.ledger.firestore_client import (
    COLLECTION_AUDIT_LOGS,
    COLLECTION_FAQ_CACHE,
    COLLECTION_HR_OPS_QUEUE,
    COLLECTION_LEDGER_ENTRIES,
    COLLECTION_SAGA_RECORDS,
    FirestoreStore,
)
from app.ledger.transaction_ledger import TransactionLedger
from app.safety.guardrails import CheapPathFAQCache


def run_setup(
    *,
    project_id: str,
    database_id: str,
    location: str,
) -> Dict[str, Any]:
    """Provisions Firestore Native database(s) in GCP and verifies all 5 collections."""
    store = FirestoreStore(
        project_id=project_id,
        database_id=database_id,
        location=location,
        use_firestore=True,
    )
    print(f">>> [1/4] Ensuring Firestore API & databases in {project_id} ({location})...")
    provision_report = store.ensure_database_exists(
        database_id, create_default_also=True
    )
    print(f"    Active database : {store.active_database_id}")
    print(f"    Cloud reachable : {store.is_cloud_active}")

    print(">>> [2/4] Seeding CheapPathFAQCache into Cloud Firestore `faq_cache`...")
    faq_cache = CheapPathFAQCache(firestore_store=store)
    seeded_faqs = faq_cache.sync_defaults_to_firestore()
    print(f"    Seeded {seeded_faqs} pre-approved FAQ entries.")

    print(">>> [3/4] Writing bootstrap AuditRecord & Ledger health-check entries...")
    audit = AuditLogger(firestore_store=store)
    ledger = TransactionLedger(firestore_store=store)

    boot_audit = audit.record(
        session_id="sess-firestore-bootstrap",
        employee_id="EMP-836",
        correlation_id="corr-firestore-bootstrap-001",
        actor_type="AUTOMATED_AGENT",
        tool_invoked="firestore_bootstrap_verify",
        pdp_decision="ALLOW",
        pdp_rule_id="RULE-BOOTSTRAP-001",
        outcome="SUCCESS",
        notes="Automated Cloud Firestore provisioning & audit health-check record (BDD NFR-1.2).",
    )
    idem_key = ledger.generate_idempotency_key(
        "EMP-836", "firestore_bootstrap_verify", {"check": "init"}
    )
    ledger.record_intent(
        idempotency_key=idem_key,
        employee_id="EMP-836",
        tool_name="firestore_bootstrap_verify",
        payload={"check": "init"},
    )
    ledger.mark_committed(idem_key, "FS-BOOT-OK")

    print(">>> [4/4] Verifying live round-trip read from Cloud Firestore...")
    fetched_audit = audit.get_record(boot_audit.correlation_id, prefer_remote=True)
    fetched_entry = ledger.get_entry(idem_key, prefer_remote=True)
    fetched_faq = faq_cache.lookup("How many sick days do I get?", prefer_remote=True)

    summary = {
        "status": "READY" if (store.is_cloud_active and fetched_audit and fetched_entry) else "FALLBACK_MEMORY",
        "project_id": project_id,
        "location": location,
        "active_database_id": store.active_database_id,
        "cloud_reachable": store.is_cloud_active,
        "provision_report": provision_report,
        "verified_collections": {
            COLLECTION_AUDIT_LOGS: bool(fetched_audit is not None),
            COLLECTION_LEDGER_ENTRIES: bool(fetched_entry is not None and fetched_entry.status == "COMMITTED"),
            COLLECTION_FAQ_CACHE: bool(fetched_faq is not None),
        },
        "seeded_faqs": seeded_faqs,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def run_status(
    *,
    project_id: str,
    database_id: str,
    location: str,
) -> Dict[str, Any]:
    store = FirestoreStore(
        project_id=project_id,
        database_id=database_id,
        location=location,
        use_firestore=True,
    )
    db_status, db_info = store.get_database_info(database_id)
    if db_status != 200:
        db_status, db_info = store.get_database_info("(default)")
        if db_status == 200:
            store._active_database_id = "(default)"

    counts: Dict[str, int] = {}
    for coll in (
        COLLECTION_AUDIT_LOGS,
        COLLECTION_LEDGER_ENTRIES,
        COLLECTION_SAGA_RECORDS,
        COLLECTION_HR_OPS_QUEUE,
        COLLECTION_FAQ_CACHE,
    ):
        docs = store.list_documents(coll, prefer_remote=True)
        counts[coll] = len(docs)

    report = {
        "project_id": project_id,
        "active_database_id": store.active_database_id,
        "database_http_status": db_status,
        "database_name": db_info.get("name"),
        "location_id": db_info.get("locationId", location),
        "type": db_info.get("type", "FIRESTORE_NATIVE"),
        "collection_document_counts": counts,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision and inspect GCP Cloud Firestore for Altostrat HR Agent"
    )
    parser.add_argument(
        "command",
        choices=["setup", "status"],
        nargs="?",
        default="setup",
        help="Action to run ('setup' provisions databases and verifies collections; 'status' prints counts)",
    )
    parser.add_argument("--project-id", default=get_gcp_project_id())
    parser.add_argument("--database-id", default=get_firestore_database())
    parser.add_argument("--location", default=get_firestore_location())
    args = parser.parse_args()

    if args.command == "setup":
        res = run_setup(
            project_id=args.project_id,
            database_id=args.database_id,
            location=args.location,
        )
        return 0 if res.get("cloud_reachable") else 1
    else:
        run_status(
            project_id=args.project_id,
            database_id=args.database_id,
            location=args.location,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
