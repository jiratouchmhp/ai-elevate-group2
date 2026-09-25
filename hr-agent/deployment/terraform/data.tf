# Transaction Ledger (SDD §1.3 plane 4, NFR-4.3): idempotency keys, saga state and
# reconciliation tasks survive Cloud Run restarts. A dedicated named database avoids
# colliding with any existing (default) database in the project.
resource "google_firestore_database" "ledger" {
  name                    = "hr-ledger"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  concurrency_mode        = "OPTIMISTIC"
  delete_protection_state = var.deletion_protection ? "DELETE_PROTECTION_ENABLED" : "DELETE_PROTECTION_DISABLED"
  deletion_policy         = var.deletion_protection ? "ABANDON" : "DELETE"
  depends_on              = [google_project_service.apis]
}

# Cloud Storage (SDD §1.3 plane 5): the governed policy corpus that feeds RAG Engine,
# and the ADK artifact bucket (LOGS_BUCKET_NAME).
resource "google_storage_bucket" "corpus" {
  name                        = "${var.project_id}-hr-policy-corpus"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  versioning {
    enabled = true
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project_id}-hr-agent-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = !var.deletion_protection
  lifecycle_rule {
    condition {
      age = 30 # SDD OQ-7: transcript retention placeholder until Compliance decides
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

# Cloud Build source staging (deploy.sh uses --gcs-source-staging-dir).
resource "google_storage_bucket" "build_staging" {
  name                        = "${var.project_id}-hr-agent-build"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "agent_artifacts" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_storage_bucket_iam_member" "build_staging" {
  bucket = google_storage_bucket.build_staging.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.build.email}"
}

# RAG Engine's service agent reads the corpus objects during import_files. The agent is
# created lazily with the first corpus, so rag_sync grants it on bootstrap and Terraform
# takes ownership once rag_corpus is known.
resource "google_storage_bucket_iam_member" "rag_reads_corpus" {
  count      = var.rag_corpus != "" ? 1 : 0
  bucket     = google_storage_bucket.corpus.name
  role       = "roles/storage.objectViewer"
  member     = "serviceAccount:service-${local.project_number}@gcp-sa-vertex-rag.iam.gserviceaccount.com"
  depends_on = [google_project_service.apis]
}
