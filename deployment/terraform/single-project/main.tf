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
    "compute.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "bigquery.googleapis.com",
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

# 0b. Artifact Registry Repository for Cloud Run Container Images
resource "google_artifact_registry_repository" "hr_agent_repo" {
  project       = var.project_id
  location      = var.region
  repository_id = "hr-agent"
  description   = "Container repository for Altostrat HR Agent (ACL/PDP & Chat UI BFF)"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# 1. VPC & Private Service Connect (PSC) Endpoint for Regional Model Armor (CON-6, §4.7)
# Note: VPC Service Controls perimeter is explicitly deferred to Pilot (OOS-1).
resource "google_compute_network" "agent_vpc" {
  name                    = "altostrat-hr-agent-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.enabled_apis]
}

resource "google_compute_subnetwork" "agent_subnet" {
  name                     = "altostrat-hr-agent-sg-subnet"
  ip_cidr_range            = "10.20.0.0/24"
  region                   = var.region
  network                  = google_compute_network.agent_vpc.id
  private_ip_google_access = true
}

resource "google_compute_address" "psc_model_armor_ip" {
  name         = "psc-model-armor-asia-southeast1"
  region       = var.region
  subnetwork   = google_compute_subnetwork.agent_subnet.id
  address_type = "INTERNAL"
}

# 2. Secret Manager for Per-Persona MCP PATs (OQ-12 / D10)
resource "google_secret_manager_secret" "persona_mcp_tokens" {
  for_each  = toset(["emp-sg-001", "emp-sg-002", "emp-sg-003"])
  secret_id = "mcp-pat-${each.key}"
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  labels = {
    app         = "altostrat-hr-agent"
    env         = "mvp1"
    cost_centre = "hr-it-shared"
  }
  depends_on = [google_project_service.enabled_apis]
}

# 3. Firestore Transaction Ledger (Idempotency + Saga State, §1.3 & §3.6)
resource "google_firestore_database" "transaction_ledger" {
  project     = var.project_id
  name        = "hr-agent-transaction-ledger"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.enabled_apis]
}

# 4. BigQuery Audit & Evaluation Warehouse (NFR-1.2, §4.6, §9)
resource "google_bigquery_dataset" "audit_and_eval_warehouse" {
  dataset_id = "altostrat_hr_agent_audit"
  location   = var.region
  labels = {
    app         = "altostrat-hr-agent"
    env         = "mvp1"
    cost_centre = "hr-it-shared"
  }
  depends_on = [google_project_service.enabled_apis]
}

# 5. Grounding & Data Plane: GCS Policy Corpus Bucket for Vertex AI RAG Engine (§3.7, D3)
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

# 6. Cloud Run Integration Plane: Anti-Corruption Layer (ACL) + Policy Decision Point (PDP) (§1.3, D4, D9)
resource "google_cloud_run_v2_service" "acl_pdp_service" {
  name     = "altostrat-hr-acl-pdp"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

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
    }
  }
  depends_on = [google_artifact_registry_repository.hr_agent_repo]
}

# 7. Cloud Run Experience Plane: React + AG-UI BFF behind IAP (§1.3, §3.10, D8)
resource "google_cloud_run_v2_service" "chat_ui_bff" {
  name     = "altostrat-hr-chat-ui"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/hr-agent/chat-ui-bff:1.0.0"
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = "global"
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
    }
  }
  depends_on = [google_artifact_registry_repository.hr_agent_repo]
}

output "project_id" {
  value = var.project_id
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
