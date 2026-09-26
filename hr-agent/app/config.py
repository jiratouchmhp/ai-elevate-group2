"""Central, versioned configuration for the Altostrat HR agent (SDD §7.2).

Every audit record stamps the *_VERSION values below so any historical decision
can be reconstructed exactly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

# --- Versioned behaviour artefacts (SDD §7.2) --------------------------------
AGENT_VERSION = "0.1.0"
PROMPT_VERSION = "1.1.0"
# RULES_VERSION and CORPUS_VERSION are read from their artefacts at load time.

# --- Model tiering (SDD D6) ---------------------------------------------------
# Pro tier for reasoning-heavy agents, Flash tier for narrow tool-shaped agents.
# Both default to the scaffold model; override via env to run tiering comparisons.
DEFAULT_MODEL = os.getenv("HR_DEFAULT_MODEL", "gemini-3.8-flash")
ORCHESTRATOR_MODEL = os.getenv("HR_ORCHESTRATOR_MODEL", DEFAULT_MODEL)
POLICY_MODEL = os.getenv("HR_POLICY_MODEL", DEFAULT_MODEL)
WORKWEEK_MODEL = os.getenv("HR_WORKWEEK_MODEL", DEFAULT_MODEL)
ITSM_MODEL = os.getenv("HR_ITSM_MODEL", DEFAULT_MODEL)

# Thinking depth per agent (latency optimisation). MINIMAL | LOW | MEDIUM | HIGH, or DEFAULT
# to leave the model's own default. Routing and tool-shaped agents rarely benefit from deep
# thinking, and every thought token is serial latency before the first visible token.
# Raise one agent (e.g. HR_POLICY_THINKING=MEDIUM) if its eval metrics regress.
DEFAULT_THINKING = os.getenv("HR_DEFAULT_THINKING", "LOW")
ORCHESTRATOR_THINKING = os.getenv("HR_ORCHESTRATOR_THINKING", DEFAULT_THINKING)
POLICY_THINKING = os.getenv("HR_POLICY_THINKING", DEFAULT_THINKING)
WORKWEEK_THINKING = os.getenv("HR_WORKWEEK_THINKING", DEFAULT_THINKING)
ITSM_THINKING = os.getenv("HR_ITSM_THINKING", DEFAULT_THINKING)

# Name of the user-facing orchestrator (keep in sync with app/agent.py root_agent).
ROOT_AGENT_NAME = "hr_agent"

# --- Identity (SDD §4.4) ------------------------------------------------------
# Stand-in for the IAP-verified identity. In eval, the trace generator injects a
# per-case persona into session state instead.
DEMO_EMPLOYEE_ID = os.getenv("DEMO_EMPLOYEE_ID", "EMP001")
ENFORCE_PILOT_ENROLLMENT = os.getenv("ENFORCE_PILOT_ENROLLMENT", "0") == "1"

# --- Integration plane --------------------------------------------------------
BACKEND_MODE = os.getenv("BACKEND_MODE", "inprocess")  # inprocess | mcp
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://127.0.0.1:8765")
BACKEND_TOKENS_JSON = os.getenv("BACKEND_TOKENS_JSON", "")  # {"EMP001": "pat..."}
# Team-level PAT (MVP). The vendor resolves identity FROM the token, so with a single
# shared PAT every call acts as that one persona (SDD "shared-PAT identity collapse").
# DEMO_EMPLOYEE_ID must then match the token's persona — see scripts/probe_backend.py.
BACKEND_SHARED_TOKEN = os.getenv("BACKEND_SHARED_TOKEN", "")
# The real WorkWeek profile does not expose `location_status` (§5.4 equipment eligibility).
# Empty => None => equipment requests are denied (fail-closed). Set to Remote/Hybrid/Onsite
# only with HR sign-off for the demo persona.
VENDOR_DEFAULT_LOCATION_STATUS = os.getenv("VENDOR_DEFAULT_LOCATION_STATUS", "")
CONFIRM_TOKEN_SECRET = os.getenv(
    "CONFIRM_TOKEN_SECRET", "local-dev-only-secret-change-me"
).encode()
CONFIRM_TOKEN_TTL_SECONDS = int(os.getenv("CONFIRM_TOKEN_TTL_SECONDS", "900"))

RUNTIME_DIR = Path(os.getenv("HR_RUNTIME_DIR", str(PROJECT_DIR / "artifacts" / "runtime")))
LEDGER_PATH = Path(os.getenv("HR_LEDGER_PATH", str(RUNTIME_DIR / "ledger.sqlite3")))
AUDIT_LOG_PATH = Path(os.getenv("HR_AUDIT_LOG_PATH", str(RUNTIME_DIR / "audit.jsonl")))
# sqlite (local/eval) | firestore (deployed — survives restarts, SDD §1.3)
LEDGER_BACKEND = os.getenv("LEDGER_BACKEND", "sqlite")
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")
# file (local JSONL) | stdout (structured → Cloud Logging → BigQuery sink) | both
AUDIT_SINK = os.getenv("AUDIT_SINK", "file")
# inprocess: tools call the ACL in-process | remote: tools call the hr-acl Cloud Run service (D7/D9)
INTEGRATION_MODE = os.getenv("INTEGRATION_MODE", "inprocess")
ACL_URL = os.getenv("ACL_URL", "")
# Shared secret for the identity envelope between the agent and hr-acl (Secret Manager in cloud).
IDENTITY_ENVELOPE_SECRET = os.getenv("IDENTITY_ENVELOPE_SECRET", "local-dev-envelope-secret").encode()

# --- Guardrails ---------------------------------------------------------------
MODEL_ARMOR_TEMPLATE_ID = os.getenv("MODEL_ARMOR_TEMPLATE_ID", "")
MODEL_ARMOR_TEMPLATE_ID_INPUT = os.getenv("MODEL_ARMOR_TEMPLATE_ID_INPUT", MODEL_ARMOR_TEMPLATE_ID)
MODEL_ARMOR_TEMPLATE_ID_OUTPUT = os.getenv("MODEL_ARMOR_TEMPLATE_ID_OUTPUT", MODEL_ARMOR_TEMPLATE_ID)
MODEL_ARMOR_LOCATION = os.getenv("MODEL_ARMOR_LOCATION", "asia-southeast1")

# --- Grounding ----------------------------------------------------------------
CORPUS_PATH = APP_DIR / "policy" / "corpus" / "handbook.md"
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "5"))
# Vertex AI RAG Engine (deployed). Empty RAG_CORPUS -> offline BM25 (tests / eval / local).
# RAG Engine, not Vertex AI Search: Search offers only global/us/eu, RAG Engine runs in
# asia-southeast1 (CON-5 residency) — documented deviation from SDD D3.
RAG_CORPUS = os.getenv("RAG_CORPUS", "")  # projects/<p>/locations/<l>/ragCorpora/<id>
RAG_LOCATION = os.getenv("RAG_LOCATION", "asia-southeast1")
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "publishers/google/models/text-embedding-005")
# Cosine distance ceiling; contexts further than this are "insufficient context" (FR-5.4).
# Calibrated on the live corpus (text-embedding-005): the farthest *relevant* top hit over
# the 100 labelled questions is 0.53, while off-topic prompts land at 0.50-0.65. 0.6 let
# most off-topic prompts through as "sufficient"; re-check with `make eval-rag` on changes.
RAG_DISTANCE_THRESHOLD = float(os.getenv("RAG_DISTANCE_THRESHOLD", "0.55"))
POLICY_CORPUS_BUCKET = os.getenv("POLICY_CORPUS_BUCKET", "")

# --- FinOps / denial-of-wallet (SDD §6.5, T-9) --------------------------------
MAX_LLM_CALLS_PER_TURN = int(os.getenv("MAX_LLM_CALLS_PER_TURN", "25"))

# Escalation channel for refusals (SDD OQ-3 — placeholder until HR Ops decides).
HR_ESCALATION_CHANNEL = os.getenv(
    "HR_ESCALATION_CHANNEL", "the HR Service Desk (open an 'HRSD' case in ServiceImmediately)"
)

# --- Chat UI / BFF (SDD §3.10) ---------------------------------------------------
# IAP email -> employee_id, e.g. {"alex.tan@altostrat.com": "EMP001"}. Behind IAP an
# unmapped user is refused (403); the BFF never falls back to a demo persona there.
PERSONA_MAP: dict[str, str] = {
    k.strip().lower(): v for k, v in json.loads(os.getenv("PERSONA_MAP_JSON", "") or "{}").items()
}
# Local demo only: lets the UI switch between the synthetic mock personas. Forced off
# unless BACKEND_MODE=inprocess (a shared PAT collapses identity to one persona).
UI_DEV_PERSONAS = os.getenv("UI_DEV_PERSONAS", "0") == "1"
UI_DIST_DIR = Path(os.getenv("UI_DIST_DIR", str(PROJECT_DIR / "frontend" / "dist")))
