# Experience + Agent Plane (hr-agent) and Integration Plane (hr-acl) — SDD §1.3, D7, D9.
#
#   browser --IAP--> hr-agent --(ID token + HMAC identity envelope)--> hr-acl --X-MCP-Token--> vendor
#
# Only hr-agent-sa holds run.invoker on hr-acl; only the IAP service agent holds
# run.invoker on hr-agent; iap_members hold iap.httpsResourceAccessor.

locals {
  common_env = {
    GOOGLE_CLOUD_PROJECT = var.project_id
    AGENT_VERSION        = var.agent_version
    LEDGER_BACKEND       = "firestore"
    FIRESTORE_DATABASE   = google_firestore_database.ledger.name
    AUDIT_SINK           = "stdout"
  }

  acl_env = merge(local.common_env, {
    BACKEND_MODE                   = "mcp"
    BACKEND_BASE_URL               = var.backend_base_url
    DEMO_EMPLOYEE_ID               = var.demo_employee_id
    VENDOR_DEFAULT_LOCATION_STATUS = var.vendor_default_location_status
  })

  agent_env = merge(local.common_env, {
    GOOGLE_GENAI_USE_VERTEXAI      = "true"
    GOOGLE_CLOUD_LOCATION          = var.model_location
    HR_DEFAULT_MODEL               = var.default_model
    MAX_LLM_CALLS_PER_TURN         = tostring(var.max_llm_calls_per_turn)
    GUARDRAIL_MODE                 = var.guardrail_mode
    HR_ESCALATION_CHANNEL          = var.hr_escalation_channel
    INTEGRATION_MODE               = "remote"
    ACL_URL                        = google_cloud_run_v2_service.acl.uri
    BACKEND_MODE                   = "mcp" # informs the UI; the agent never calls the vendor directly
    DEMO_EMPLOYEE_ID               = var.demo_employee_id
    PERSONA_MAP_JSON               = jsonencode(var.persona_map)
    UI_DEV_PERSONAS                = "0"
    MODEL_ARMOR_LOCATION           = var.region
    MODEL_ARMOR_TEMPLATE_ID_INPUT  = google_model_armor_template.input.template_id
    MODEL_ARMOR_TEMPLATE_ID_OUTPUT = google_model_armor_template.output.template_id
    RAG_CORPUS                     = var.rag_corpus
    RAG_LOCATION                   = var.region
    RAG_DISTANCE_THRESHOLD         = tostring(var.rag_distance_threshold)
    POLICY_CORPUS_BUCKET           = google_storage_bucket.corpus.name
    LOGS_BUCKET_NAME               = google_storage_bucket.artifacts.name
  }, var.agent_env_overrides)

  shared_secret_env = {
    IDENTITY_ENVELOPE_SECRET = google_secret_manager_secret.envelope.secret_id
    CONFIRM_TOKEN_SECRET     = google_secret_manager_secret.confirm.secret_id
  }

  acl_secret_env = merge(local.shared_secret_env, {
    BACKEND_SHARED_TOKEN = google_secret_manager_secret.vendor_pat.secret_id
    }, var.enable_per_persona_tokens ? {
    BACKEND_TOKENS_JSON = google_secret_manager_secret.vendor_tokens[0].secret_id
  } : {})
}

# ------------------------------------------------------------------ hr-acl
resource "google_cloud_run_v2_service" "acl" {
  name                = "hr-acl"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # admitted by IAM only (no allUsers)
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.acl.email
    max_instance_request_concurrency = 40
    timeout                          = "30s"

    scaling {
      min_instance_count = var.acl_min_instances
      max_instance_count = var.acl_max_instances
    }

    containers {
      image = local.acl_image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      dynamic "env" {
        for_each = local.acl_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.acl_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 5
        failure_threshold     = 12
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.shared,
    google_secret_manager_secret_iam_member.tokens_acl,
    google_artifact_registry_repository_iam_member.runtime_readers,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "agent_invokes_acl" {
  location = google_cloud_run_v2_service.acl.location
  name     = google_cloud_run_v2_service.acl.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.agent.email}"
}

# ------------------------------------------------------------------ hr-agent
resource "google_cloud_run_v2_service" "agent" {
  provider            = google-beta
  name                = "hr-agent"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  iap_enabled         = true # SDD §3.10 / §4.4: IAP establishes *who*
  deletion_protection = var.deletion_protection

  template {
    service_account                  = google_service_account.agent.email
    session_affinity                 = true # in-memory ADK sessions (see agent_max_instances)
    max_instance_request_concurrency = 20
    timeout                          = "300s" # SSE streams

    scaling {
      min_instance_count = var.agent_min_instances
      max_instance_count = var.agent_max_instances
    }

    containers {
      image = local.agent_image

      resources {
        limits = {
          cpu    = "2"
          memory = "2Gi"
        }
        cpu_idle          = true
        startup_cpu_boost = true
      }

      dynamic "env" {
        for_each = local.agent_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.shared_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }

      startup_probe {
        tcp_socket {
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 24
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.shared,
    google_artifact_registry_repository_iam_member.runtime_readers,
    google_project_iam_member.agent,
  ]
}

# IAP forwards authenticated requests using its service agent.
resource "google_cloud_run_v2_service_iam_member" "iap_invokes_agent" {
  provider = google-beta
  location = google_cloud_run_v2_service.agent.location
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_project_service_identity.iap.email}"
}

resource "google_iap_web_cloud_run_service_iam_member" "users" {
  provider               = google-beta
  for_each               = toset(var.iap_members)
  project                = var.project_id
  location               = google_cloud_run_v2_service.agent.location
  cloud_run_service_name = google_cloud_run_v2_service.agent.name
  role                   = "roles/iap.httpsResourceAccessor"
  member                 = each.value
}
