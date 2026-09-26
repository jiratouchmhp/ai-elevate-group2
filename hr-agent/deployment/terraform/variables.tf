# ------------------------------------------------------------------ project / placement
variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Primary region for every regional resource (SDD CON-5: Singapore residency)."
  type        = string
  default     = "asia-southeast1"
}

variable "environment" {
  description = "dev | staging | prod (SDD §7.1 — prod is NOT production during MVP 1)."
  type        = string
  default     = "dev"
}

variable "cost_centre" {
  description = "FinOps attribution label (SDD §6.5)."
  type        = string
  default     = "hr-it-shared-services"
}

variable "extra_labels" {
  type    = map(string)
  default = {}
}

# ------------------------------------------------------------------ images (set by deploy.sh)
variable "agent_image" {
  description = "hr-agent image (UI + BFF + ADK agents). Empty -> placeholder image."
  type        = string
  default     = ""
}

variable "acl_image" {
  description = "hr-acl image (Integration Plane). Empty -> placeholder image."
  type        = string
  default     = ""
}

variable "agent_version" {
  description = "Stamped into every audit record (SDD §7.2); deploy.sh passes the git SHA."
  type        = string
  default     = "0.1.0"
}

# ------------------------------------------------------------------ model / agent behaviour
variable "model_location" {
  description = "Vertex AI location for Gemini. 'global' until SDD OQ-5 (regional Pro tier) is resolved."
  type        = string
  default     = "global"
}

variable "default_model" {
  description = "HR_DEFAULT_MODEL (per-agent overrides via agent_env_overrides)."
  type        = string
  default     = "gemini-3.8-flash"
}

variable "agent_env_overrides" {
  description = "Extra/override env vars for hr-agent (e.g. HR_ORCHESTRATOR_MODEL)."
  type        = map(string)
  default     = {}
}

variable "guardrail_mode" {
  description = "enforce | inspect (shadow mode for Phase 4 threshold tuning, SDD §7.4)."
  type        = string
  default     = "enforce"
  validation {
    condition     = contains(["enforce", "inspect"], var.guardrail_mode)
    error_message = "guardrail_mode must be enforce or inspect."
  }
}

variable "max_llm_calls_per_turn" {
  description = "Denial-of-wallet cap (SDD T-9, §6.5)."
  type        = number
  default     = 25
}

variable "hr_escalation_channel" {
  description = "Where refusals route (SDD OQ-3)."
  type        = string
  default     = "the HR Service Desk (open an 'HRSD' case in ServiceImmediately)"
}

# ------------------------------------------------------------------ grounding (RAG Engine)
variable "rag_corpus" {
  description = "Vertex AI RAG Engine corpus resource name, written by `app.policy.rag_sync --write-tfvars`. Empty -> BM25 fallback."
  type        = string
  default     = ""
}

variable "rag_distance_threshold" {
  description = "Max cosine distance for a retrieved chunk to count as relevant (calibrated with make eval-rag)."
  type        = number
  default     = 0.55
}

# ------------------------------------------------------------------ identity (IAP)
variable "iap_members" {
  description = "Principals allowed through IAP, e.g. [\"allAuthenticatedUsers\"] or [\"user:alex@altostrat.com\"]."
  type        = list(string)
  default     = []
}

variable "persona_map" {
  description = "IAP email -> employee_id (PERSONA_MAP_JSON). With a single shared PAT, map everyone to the PAT's persona."
  type        = map(string)
  default     = {}
}

variable "demo_employee_id" {
  description = "Persona the shared vendor PAT resolves to (see scripts/probe_backend.py)."
  type        = string
  default     = "EMP-829"
}

variable "vendor_default_location_status" {
  description = "WorkWeek profile lacks location_status (§5.4). Empty = equipment requests fail closed; Remote/Hybrid/Onsite needs HR sign-off."
  type        = string
  default     = ""
}

# ------------------------------------------------------------------ vendor backends
variable "backend_base_url" {
  description = "Vendor host serving /work-week/mcp/ and /service-immediately/mcp/."
  type        = string
  default     = "https://mock-saas.example.com"
}

variable "enable_per_persona_tokens" {
  description = "Also mount BACKEND_TOKENS_JSON from Secret Manager (one PAT per persona, SDD OQ-12 option a)."
  type        = bool
  default     = false
}

# ------------------------------------------------------------------ scaling
variable "agent_max_instances" {
  description = "Sessions are in-memory: keep at 1 until a shared session service is adopted."
  type        = number
  default     = 1
}

variable "acl_max_instances" {
  type    = number
  default = 3
}

# Warm instances remove cold-start latency (image pull, Python import, index build) from the
# first request after idle. They are billed while idle, so default 0; set 1 for demos/pilot.
variable "agent_min_instances" {
  description = "Minimum warm hr-agent instances (0 = scale to zero; must be <= agent_max_instances)."
  type        = number
  default     = 0
}

variable "acl_min_instances" {
  description = "Minimum warm hr-acl instances (0 = scale to zero)."
  type        = number
  default     = 0
}

variable "deletion_protection" {
  description = "Protect Firestore / Cloud Run from accidental destroy."
  type        = bool
  default     = false
}

variable "deployer_member" {
  description = "IAM member running deploy.sh (e.g. user:me@example.com); granted actAs on hr-build-sa. deploy.sh sets it from `gcloud config get account`."
  type        = string
  default     = ""
}

variable "model_armor_malicious_uri" {
  description = "Enable Model Armor's malicious-URI filter. Unsupported in asia-southeast1 (API 400), so off by default to keep templates in-region."
  type        = bool
  default     = false
}
