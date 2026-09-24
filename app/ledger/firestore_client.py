"""GCP Cloud Firestore Client & Provisioner for Audit Logging, Transaction Ledger & FAQ Cache.

Provides `FirestoreStore` with:
- Native Cloud Firestore v1 REST / SDK integration for project `ai-training-van-01` (`asia-southeast1`)
- Bidirectional Python <-> Firestore Value serialization (`to_firestore_fields` / `from_firestore_fields`)
- Automated database provisioning (`ensure_database_exists` for `hr-agent-transaction-ledger` & `(default)`)
- Write-through persistence with automatic in-memory fallback when offline or running isolated unit tests
"""

from __future__ import annotations

import copy
from dataclasses import asdict, is_dataclass
from enum import Enum
import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
import httpx

from app.config.env_config import (
    get_firestore_database,
    get_firestore_location,
    get_gcp_project_id,
    is_firestore_enabled,
)


FIRESTORE_API_BASE = "https://firestore.googleapis.com/v1"
SERVICE_USAGE_API_BASE = "https://serviceusage.googleapis.com/v1"

# Standard collection names in Cloud Firestore
COLLECTION_AUDIT_LOGS = "audit_logs"
COLLECTION_LEDGER_ENTRIES = "ledger_entries"
COLLECTION_SAGA_RECORDS = "saga_records"
COLLECTION_HR_OPS_QUEUE = "hr_ops_reconciliation_queue"
COLLECTION_FAQ_CACHE = "faq_cache"


def to_firestore_value(val: Any) -> Dict[str, Any]:
    """Converts a Python value into a Cloud Firestore REST API Value object."""
    if val is None:
        return {"nullValue": None}
    if isinstance(val, bool):
        return {"booleanValue": val}
    if isinstance(val, int) and not isinstance(val, bool):
        return {"integerValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if isinstance(val, Enum):
        return {"stringValue": str(val.value)}
    if isinstance(val, str):
        return {"stringValue": val}
    if is_dataclass(val) and not isinstance(val, type):
        return to_firestore_value(asdict(val))
    if isinstance(val, (list, tuple)):
        return {"arrayValue": {"values": [to_firestore_value(item) for item in val]}}
    if isinstance(val, dict):
        return {"mapValue": {"fields": to_firestore_fields(val)}}
    return {"stringValue": str(val)}


def to_firestore_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Converts a Python dictionary into a Firestore `fields` map."""
    fields: Dict[str, Any] = {}
    for k, v in data.items():
        fields[str(k)] = to_firestore_value(v)
    return fields


def from_firestore_value(fs_val: Dict[str, Any]) -> Any:
    """Converts a Cloud Firestore REST API Value object back to a native Python value."""
    if not isinstance(fs_val, dict):
        return fs_val
    if "nullValue" in fs_val:
        return None
    if "booleanValue" in fs_val:
        return bool(fs_val["booleanValue"])
    if "integerValue" in fs_val:
        try:
            return int(fs_val["integerValue"])
        except (ValueError, TypeError):
            return 0
    if "doubleValue" in fs_val:
        try:
            return float(fs_val["doubleValue"])
        except (ValueError, TypeError):
            return 0.0
    if "stringValue" in fs_val:
        return str(fs_val["stringValue"])
    if "timestampValue" in fs_val:
        return str(fs_val["timestampValue"])
    if "arrayValue" in fs_val:
        arr = fs_val.get("arrayValue") or {}
        values = arr.get("values") or []
        return [from_firestore_value(item) for item in values]
    if "mapValue" in fs_val:
        mp = fs_val.get("mapValue") or {}
        fields = mp.get("fields") or {}
        return from_firestore_fields(fields)
    return None


def from_firestore_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Converts a Firestore `fields` map back to a native Python dictionary."""
    if not fields:
        return {}
    return {k: from_firestore_value(v) for k, v in fields.items()}


def from_firestore_document(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts native Python dictionary and `_document_id` from a Firestore Document payload."""
    fields = doc.get("fields") or {}
    data = from_firestore_fields(fields)
    name = doc.get("name", "")
    if name and "_document_id" not in data:
        data["_document_id"] = name.split("/")[-1]
    return data


class GCPAuthTokenProvider:
    """Acquires and caches GCP OAuth2 Bearer tokens via ADC (`google.auth`) or `gcloud`."""

    def __init__(self) -> None:
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_access_token(self) -> Optional[str]:
        now = time.time()
        if self._cached_token and now < self._expires_at:
            return self._cached_token

        # 1. Explicit environment token override (useful in CI / tests)
        env_token = os.environ.get("GCP_ACCESS_TOKEN", "").strip()
        if env_token:
            self._cached_token = env_token
            self._expires_at = now + 3000
            return env_token

        # 2. Try google.auth Application Default Credentials (ADC)
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleAuthRequest

            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if not creds.valid:
                creds.refresh(GoogleAuthRequest())
            if creds.token:
                self._cached_token = str(creds.token).strip()
                self._expires_at = now + 3000
                return self._cached_token
        except Exception:
            pass

        # 3. Fallback to gcloud CLI
        gcloud_candidates = [
            shutil.which("gcloud"),
            "/usr/local/google/home/vannick/google-cloud-sdk/bin/gcloud",
            "/usr/bin/gcloud",
        ]
        for gcloud_bin in gcloud_candidates:
            if not gcloud_bin or not os.path.exists(gcloud_bin):
                continue
            try:
                proc = subprocess.run(
                    [gcloud_bin, "auth", "print-access-token"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    self._cached_token = proc.stdout.strip()
                    self._expires_at = now + 3000
                    return self._cached_token
            except Exception:
                continue

        return None


class FirestoreStore:
    """Dual-mode Cloud Firestore persistence engine (Live GCP Firestore + In-Memory Mirror).

    Supports:
    - Live persistence to `projects/{project_id}/databases/{database_id}/documents/{collection}/{doc_id}`
    - Automatic fallback to `(default)` database if named database is still provisioning
    - Automatic fallback to in-memory mirror when offline or when `use_firestore=False`
    """

    def __init__(
        self,
        *,
        project_id: Optional[str] = None,
        database_id: Optional[str] = None,
        location: Optional[str] = None,
        use_firestore: Optional[bool] = None,
        timeout_seconds: float = 8.0,
        http_send: Optional[Callable[[str, str, Dict[str, str], Optional[Dict[str, Any]]], Tuple[int, Dict[str, Any]]]] = None,
        token_provider: Optional[GCPAuthTokenProvider] = None,
    ) -> None:
        self.project_id = project_id or get_gcp_project_id()
        self.database_id = database_id or get_firestore_database()
        self.location = location or get_firestore_location()
        self.use_firestore = (
            is_firestore_enabled(default=True)
            if use_firestore is None
            else bool(use_firestore)
        )
        self.timeout_seconds = timeout_seconds
        self._http_send = http_send
        self._token_provider = token_provider or GCPAuthTokenProvider()

        # In-memory mirror keyed by collection -> document_id -> dict
        self._memory: Dict[str, Dict[str, Dict[str, Any]]] = {
            COLLECTION_AUDIT_LOGS: {},
            COLLECTION_LEDGER_ENTRIES: {},
            COLLECTION_SAGA_RECORDS: {},
            COLLECTION_HR_OPS_QUEUE: {},
            COLLECTION_FAQ_CACHE: {},
        }
        self._cloud_reachable: Optional[bool] = None
        self._active_database_id: str = self.database_id
        self._last_error: Optional[str] = None
        self._write_count: int = 0
        self._read_count: int = 0

    @property
    def active_database_id(self) -> str:
        return self._active_database_id

    @property
    def is_cloud_active(self) -> bool:
        return bool(self.use_firestore and self._cloud_reachable is True)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def reset_circuit_breaker(self) -> None:
        """Resets the cloud reachability flag so the next operation re-probes Firestore."""
        self._cloud_reachable = None
        self._last_error = None

    def _documents_base_url(self, db_id: Optional[str] = None) -> str:
        target_db = db_id or self._active_database_id
        encoded_db = quote(target_db, safe="()")
        return f"{FIRESTORE_API_BASE}/projects/{self.project_id}/databases/{encoded_db}/documents"

    def _databases_base_url(self) -> str:
        return f"{FIRESTORE_API_BASE}/projects/{self.project_id}/databases"

    def _request(
        self,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        *,
        bypass_circuit_breaker: bool = False,
        timeout_override: Optional[float] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        if not self.use_firestore:
            return 0, {"error": "USE_FIRESTORE is disabled"}
        if self._cloud_reachable is False and not bypass_circuit_breaker:
            return 0, {"error": self._last_error or "Firestore unreachable (circuit breaker open)"}

        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-user-project": self.project_id,
        }

        if self._http_send is not None:
            try:
                status, data = self._http_send(method, url, headers, json_body)
                if 200 <= status < 300:
                    self._cloud_reachable = True
                return status, data
            except Exception as exc:
                self._cloud_reachable = False
                self._last_error = str(exc)
                return 0, {"error": str(exc)}

        token = self._token_provider.get_access_token()
        if not token:
            self._cloud_reachable = False
            self._last_error = "No GCP access token available"
            return 0, {"error": self._last_error}

        headers["Authorization"] = f"Bearer {token}"

        req_timeout = timeout_override if timeout_override is not None else self.timeout_seconds
        try:
            with httpx.Client(timeout=req_timeout) as client:
                resp = client.request(method, url, headers=headers, json=json_body)
                try:
                    payload = resp.json() if resp.content else {}
                except Exception:
                    payload = {"raw": resp.text}
                if 200 <= resp.status_code < 300:
                    self._cloud_reachable = True
                    self._last_error = None
                return resp.status_code, payload
        except Exception as exc:
            self._cloud_reachable = False
            self._last_error = f"{type(exc).__name__}: {exc}"
            return 0, {"error": self._last_error}

    # --- Provisioning & Database Management ---

    def enable_firestore_api(self) -> Dict[str, Any]:
        """Enables `firestore.googleapis.com` in `self.project_id`."""
        url = f"{SERVICE_USAGE_API_BASE}/projects/{self.project_id}/services/firestore.googleapis.com:enable"
        status, payload = self._request(
            "POST", url, {}, bypass_circuit_breaker=True, timeout_override=30.0
        )
        return {"status_code": status, "response": payload}

    def get_database_info(self, db_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        target_db = db_id or self.database_id
        encoded_db = quote(target_db, safe="()")
        url = f"{self._databases_base_url()}/{encoded_db}"
        return self._request("GET", url, bypass_circuit_breaker=True, timeout_override=15.0)

    def ensure_database_exists(
        self,
        db_id: Optional[str] = None,
        *,
        create_default_also: bool = True,
        wait_seconds: float = 45.0,
    ) -> Dict[str, Any]:
        """Ensures the Cloud Firestore Native database exists in `self.project_id` (`self.location`).

        Creates `hr-agent-transaction-ledger` (and `(default)` if requested) when missing.
        """
        self.reset_circuit_breaker()
        self.enable_firestore_api()

        targets = [db_id or self.database_id]
        if create_default_also and "(default)" not in targets:
            targets.insert(0, "(default)")

        results: Dict[str, Any] = {}
        for target in targets:
            status, info = self.get_database_info(target)
            if status == 200:
                results[target] = {"status": "EXISTING", "database": info}
                self._active_database_id = target
                self._cloud_reachable = True
                continue

            # Create database in FIRESTORE_NATIVE mode
            create_url = f"{self._databases_base_url()}?databaseId={quote(target, safe='()')}"
            body = {
                "locationId": self.location,
                "type": "FIRESTORE_NATIVE",
                "deleteProtectionState": "DELETE_PROTECTION_DISABLED",
            }
            c_status, c_payload = self._request(
                "POST", create_url, body, bypass_circuit_breaker=True, timeout_override=30.0
            )
            if c_status in (200, 201, 409):
                # Poll until database is ready
                deadline = time.time() + wait_seconds
                db_ready = False
                last_info = c_payload
                while time.time() < deadline:
                    g_status, g_info = self.get_database_info(target)
                    if g_status == 200 and g_info.get("name"):
                        db_ready = True
                        last_info = g_info
                        break
                    time.sleep(2.0)
                results[target] = {
                    "status": "CREATED" if db_ready else "PROVISIONING",
                    "http_status": c_status,
                    "database": last_info,
                }
                if db_ready:
                    self._active_database_id = target
                    self._cloud_reachable = True
            else:
                results[target] = {
                    "status": "ERROR",
                    "http_status": c_status,
                    "detail": c_payload,
                }

        # Prefer self.database_id if ready, else fallback to (default)
        pref_status, _ = self.get_database_info(self.database_id)
        if pref_status == 200:
            self._active_database_id = self.database_id
            self._cloud_reachable = True
        else:
            def_status, _ = self.get_database_info("(default)")
            if def_status == 200:
                self._active_database_id = "(default)"
                self._cloud_reachable = True

        return {
            "project_id": self.project_id,
            "location": self.location,
            "active_database_id": self._active_database_id,
            "cloud_reachable": bool(self._cloud_reachable),
            "databases": results,
        }

    # --- CRUD Operations with Write-Through & Automatic Fallback ---

    def upsert_document(
        self,
        collection: str,
        document_id: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Persists a document to in-memory mirror and Cloud Firestore (`PATCH` upsert)."""
        clean_id = str(document_id).strip().replace("/", "_")
        doc_copy = copy.deepcopy(data)
        self._memory.setdefault(collection, {})[clean_id] = doc_copy

        if not self.use_firestore or self._cloud_reachable is False:
            return doc_copy

        url = f"{self._documents_base_url()}/{quote(collection)}/{quote(clean_id)}"
        fs_body = {"fields": to_firestore_fields(doc_copy)}
        status, resp = self._request("PATCH", url, fs_body)

        # If named database returned 404 (not yet created), try (default) database automatically
        if status == 404 and self._active_database_id != "(default)":
            alt_url = f"{self._documents_base_url('(default)')}/{quote(collection)}/{quote(clean_id)}"
            alt_status, alt_resp = self._request(
                "PATCH", alt_url, fs_body, bypass_circuit_breaker=True
            )
            if 200 <= alt_status < 300:
                self._active_database_id = "(default)"
                self._cloud_reachable = True
                status, resp = alt_status, alt_resp

        if 200 <= status < 300:
            self._write_count += 1
        return doc_copy

    def get_document(
        self,
        collection: str,
        document_id: str,
        *,
        prefer_remote: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a document by ID from local mirror or live Cloud Firestore."""
        clean_id = str(document_id).strip().replace("/", "_")
        local_doc = self._memory.get(collection, {}).get(clean_id)
        if local_doc is not None and not prefer_remote:
            return copy.deepcopy(local_doc)

        if not self.use_firestore or self._cloud_reachable is False:
            return copy.deepcopy(local_doc) if local_doc is not None else None

        url = f"{self._documents_base_url()}/{quote(collection)}/{quote(clean_id)}"
        status, resp = self._request("GET", url)
        if status == 404 and self._active_database_id != "(default)":
            alt_url = f"{self._documents_base_url('(default)')}/{quote(collection)}/{quote(clean_id)}"
            alt_status, alt_resp = self._request("GET", alt_url, bypass_circuit_breaker=True)
            if alt_status == 200:
                self._active_database_id = "(default)"
                self._cloud_reachable = True
                status, resp = alt_status, alt_resp

        if status == 200 and isinstance(resp, dict) and "fields" in resp:
            self._read_count += 1
            decoded = from_firestore_document(resp)
            self._memory.setdefault(collection, {})[clean_id] = copy.deepcopy(decoded)
            return decoded

        return copy.deepcopy(local_doc) if local_doc is not None else None

    def list_documents(
        self,
        collection: str,
        *,
        page_size: int = 300,
        prefer_remote: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lists documents in a collection, merging remote Cloud Firestore docs with local mirror."""
        merged: Dict[str, Dict[str, Any]] = {
            k: copy.deepcopy(v)
            for k, v in self._memory.get(collection, {}).items()
        }

        if self.use_firestore and (prefer_remote or self._cloud_reachable is not False):
            url = f"{self._documents_base_url()}/{quote(collection)}?pageSize={int(page_size)}"
            status, resp = self._request("GET", url)
            if status == 404 and self._active_database_id != "(default)":
                alt_url = f"{self._documents_base_url('(default)')}/{quote(collection)}?pageSize={int(page_size)}"
                alt_status, alt_resp = self._request("GET", alt_url, bypass_circuit_breaker=True)
                if alt_status == 200:
                    self._active_database_id = "(default)"
                    self._cloud_reachable = True
                    status, resp = alt_status, alt_resp

            if status == 200 and isinstance(resp, dict):
                self._read_count += 1
                for doc in resp.get("documents") or []:
                    decoded = from_firestore_document(doc)
                    doc_id = str(decoded.get("_document_id") or doc.get("name", "").split("/")[-1])
                    if doc_id:
                        self._memory.setdefault(collection, {})[doc_id] = copy.deepcopy(decoded)
                        merged[doc_id] = decoded

        return list(merged.values())

    def query_documents(
        self,
        collection: str,
        *,
        field_equals: Optional[Dict[str, Any]] = None,
        prefer_remote: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Queries documents in `collection` matching `field_equals` using Firestore `:runQuery`."""
        if not field_equals:
            return self.list_documents(collection, page_size=max(limit, 300), prefer_remote=prefer_remote)[:limit]

        # 1. Server-side single-field indexed query via :runQuery when live Firestore is active
        if self.use_firestore and (prefer_remote or self._cloud_reachable is not False) and self._http_send is None:
            primary_field, primary_val = next(iter(field_equals.items()))
            query_url = f"{self._documents_base_url()}:runQuery"
            query_body = {
                "structuredQuery": {
                    "from": [{"collectionId": collection}],
                    "where": {
                        "fieldFilter": {
                            "field": {"fieldPath": primary_field},
                            "op": "EQUAL",
                            "value": to_firestore_value(primary_val),
                        }
                    },
                    "limit": max(limit, 200),
                }
            }
            status, rows = self._request("POST", query_url, query_body)
            if status == 200 and isinstance(rows, list):
                self._read_count += 1
                for row in rows:
                    doc = row.get("document") if isinstance(row, dict) else None
                    if doc and isinstance(doc, dict) and "fields" in doc:
                        decoded = from_firestore_document(doc)
                        doc_id = str(decoded.get("_document_id") or doc.get("name", "").split("/")[-1])
                        if doc_id:
                            self._memory.setdefault(collection, {})[doc_id] = copy.deepcopy(decoded)

        # 2. Also support simulator / fallback via list_documents if needed
        elif self._http_send is not None and prefer_remote:
            self.list_documents(collection, page_size=max(limit, 300), prefer_remote=True)

        # 3. Filter across hydrated local mirror
        matched: List[Dict[str, Any]] = []
        for doc in self._memory.get(collection, {}).values():
            if all(doc.get(k) == v for k, v in field_equals.items()):
                matched.append(copy.deepcopy(doc))
        return matched[:limit]

    def delete_document(self, collection: str, document_id: str) -> bool:
        """Deletes a document from local mirror and Cloud Firestore."""
        clean_id = str(document_id).strip().replace("/", "_")
        existed_local = clean_id in self._memory.get(collection, {})
        self._memory.get(collection, {}).pop(clean_id, None)

        if self.use_firestore and self._cloud_reachable is not False:
            url = f"{self._documents_base_url()}/{quote(collection)}/{quote(clean_id)}"
            status, _ = self._request("DELETE", url)
            return existed_local or (200 <= status < 300)

        return existed_local

    def clear_local(self) -> None:
        """Clears the in-memory mirror (used between unit test runs)."""
        for coll in self._memory.values():
            coll.clear()

    def status_summary(self) -> Dict[str, Any]:
        """Returns health and telemetry summary for `/api/health` and CLI status."""
        return {
            "project_id": self.project_id,
            "configured_database_id": self.database_id,
            "active_database_id": self._active_database_id,
            "location": self.location,
            "use_firestore": self.use_firestore,
            "cloud_reachable": self._cloud_reachable,
            "last_error": self._last_error,
            "remote_writes": self._write_count,
            "remote_reads": self._read_count,
            "local_counts": {
                coll: len(items) for coll, items in self._memory.items()
            },
        }


DEFAULT_FIRESTORE_STORE = FirestoreStore()
