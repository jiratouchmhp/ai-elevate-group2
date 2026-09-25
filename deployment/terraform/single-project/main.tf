# Altostrat Singapore — HR Agentic Assistant (MVP 1) Infrastructure-as-Code
# SDD §1.3, §3.7, §4.7, §7.1, §7.2 (Project: ai-training-van-01, Region: asia-southeast1)

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
  }
}

variable "project_id" {
  type        = string
  default     = "ai-training-van-01"
  description = "GCP Project ID for Altostrat HR Agentic Assistant provisioning"
}

variable "region" {
  type        = string
  default     = "asia-southeast1"
  description = "Primary region (Singapore data residency per CON-5)"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 0. Enable Required GCP APIs on ai-training-van-01
locals {
  required_apis = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "discoveryengine.googleapis.com",
  ])
}

resource "google_project_service" "enabled_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 1. Artifact Registry Repository for Cloud Run Container Images
resource "google_artifact_registry_repository" "hr_agent_repo" {
  project       = var.project_id
  location      = var.region
  repository_id = "hr-agent"
  description   = "Container repository for Altostrat HR Agent (ACL/PDP & Chat UI BFF)"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# 2. Firestore Audit & Transaction Ledger Database (Idempotency, Saga State & BDD Audit Trail, §1.3, §3.6, §4.6)
resource "google_firestore_database" "transaction_ledger" {
  project                 = var.project_id
  name                    = "hr-agent-transaction-ledger"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_DISABLED"
  depends_on              = [google_project_service.enabled_apis]
}

# 3. Grounding & Data Plane: GCS Policy Corpus Bucket for Vertex AI RAG Engine (§3.7, D3)
resource "google_storage_bucket" "policy_corpus_bucket" {
  name                        = "${var.project_id}-hr-policy-corpus"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels = {
    app            = "altostrat-hr-agent"
    corpus_version = "2026-07-altostrat-sg-v1"
    plane          = "grounding-data"
  }
  depends_on = [google_project_service.enabled_apis]
}

# 4. Cloud Run Integration Plane: Anti-Corruption Layer (ACL) + Policy Decision Point (PDP) (§1.3, D4, D9)
resource "google_cloud_run_v2_service" "acl_pdp_service" {
  name                 = "altostrat-hr-acl-pdp"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = false

  template {
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/hr-agent/acl-pdp:1.0.0"
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "VERTEX_RAG_LOCATION"
        value = var.region
      }
      env {
        name  = "GEMINI_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "GEMINI_PRO_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "GEMINI_FLASH_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "RULES_VERSION"
        value = "1.2.0"
      }
      env {
        name  = "CORPUS_VERSION"
        value = "2026-07-altostrat-sg-v1"
      }
      env {
        name  = "VERTEX_RAG_CORPUS_ID"
        value = "4611686018427387904"
      }
      env {
        name  = "USE_CLOUD_RAG"
        value = "true"
      }
      env {
        name  = "USE_FIRESTORE"
        value = "true"
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.transaction_ledger.name
      }
      env {
        name  = "FIRESTORE_LOCATION"
        value = var.region
      }
      env {
        name  = "MCP_SERVER_BASE_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com"
      }
      env {
        name  = "WORKWEEK_MCP_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com/work-week/mcp/"
      }
      env {
        name  = "SERVICE_IMMEDIATELY_MCP_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com/service-immediately/mcp/"
      }
      env {
        name  = "MCP_TOKEN"
        value = "mcp_m6BfI7HZPQy4gSAaFHS-bha9bX8Bg_EoARM050hQhec"
      }
      env {
        name  = "MCP_AUTHENTICATED_EMPLOYEE_ID"
        value = "EMP-836"
      }
      env {
        name  = "USE_LIVE_MCP"
        value = "true"
      }
    }
  }
  depends_on = [google_artifact_registry_repository.hr_agent_repo, google_firestore_database.transaction_ledger]
}

# 5. Cloud Run Experience Plane: React + AG-UI BFF Public Demo Endpoint (§1.3, §3.10, D8)
resource "google_cloud_run_v2_service" "chat_ui_bff" {
  name                 = "altostrat-hr-chat-ui"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true
  deletion_protection  = false

  template {
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/hr-agent/acl-pdp:1.0.0"
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "VERTEX_RAG_LOCATION"
        value = var.region
      }
      env {
        name  = "GEMINI_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "GEMINI_PRO_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "GEMINI_FLASH_MODEL"
        value = "gemini-3.8-flash"
      }
      env {
        name  = "VERTEX_RAG_CORPUS_ID"
        value = "4611686018427387904"
      }
      env {
        name  = "USE_CLOUD_RAG"
        value = "true"
      }
      env {
        name  = "USE_FIRESTORE"
        value = "true"
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.transaction_ledger.name
      }
      env {
        name  = "FIRESTORE_LOCATION"
        value = var.region
      }
      env {
        name  = "MCP_SERVER_BASE_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com"
      }
      env {
        name  = "WORKWEEK_MCP_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com/work-week/mcp/"
      }
      env {
        name  = "SERVICE_IMMEDIATELY_MCP_URL"
        value = "https://mock-saas.aishprabhat.demo.altostrat.com/service-immediately/mcp/"
      }
      env {
        name  = "MCP_TOKEN"
        value = "mcp_m6BfI7HZPQy4gSAaFHS-bha9bX8Bg_EoARM050hQhec"
      }
      env {
        name  = "MCP_AUTHENTICATED_EMPLOYEE_ID"
        value = "EMP-836"
      }
      env {
        name  = "USE_LIVE_MCP"
        value = "true"
      }
    }
  }
  depends_on = [google_artifact_registry_repository.hr_agent_repo, google_firestore_database.transaction_ledger]
}

output "project_id" {
  value = var.project_id
}

output "firestore_database_name" {
  value = google_firestore_database.transaction_ledger.name
}

output "policy_corpus_bucket_uri" {
  value = "gs://${google_storage_bucket.policy_corpus_bucket.name}"
}

output "vertex_rag_corpus_resource" {
  value = "projects/${var.project_id}/locations/${var.region}/ragCorpora/4611686018427387904"
}

output "acl_pdp_service_uri" {
  value = google_cloud_run_v2_service.acl_pdp_service.uri
}

output "chat_ui_bff_uri" {
  value = google_cloud_run_v2_service.chat_ui_bff.uri
}

