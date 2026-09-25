# Least-privilege identities (SDD §4.4, §4.7). One SA per service; the ACL is the only
# identity that can read the vendor PAT, and the agent is the only one that can invoke the ACL.

resource "google_service_account" "agent" {
  account_id   = "hr-agent-sa"
  display_name = "HR agent (UI + BFF + ADK agents) — Cloud Run"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "acl" {
  account_id   = "hr-acl-sa"
  display_name = "HR Integration Plane (PDP + ACL) — Cloud Run"
  depends_on   = [google_project_service.apis]
}

resource "google_service_account" "build" {
  account_id   = "hr-build-sa"
  display_name = "Cloud Build — hr-agent images"
  depends_on   = [google_project_service.apis]
}

locals {
  agent_project_roles = [
    "roles/aiplatform.user",   # Gemini + RAG Engine retrieval
    "roles/modelarmor.user",   # sanitize prompt / response
    "roles/logging.logWriter", # audit to Cloud Logging -> BigQuery
    "roles/cloudtrace.agent",  # OTel traces
    "roles/monitoring.metricWriter",
    "roles/telemetry.tracesWriter",
  ]
  acl_project_roles = [
    "roles/datastore.user", # Firestore ledger (idempotency + saga)
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
  ]
  build_project_roles = [
    "roles/logging.logWriter",
  ]
}

resource "google_project_iam_member" "agent" {
  for_each = toset(local.agent_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_project_iam_member" "acl" {
  for_each = toset(local.acl_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.acl.email}"
}

resource "google_project_iam_member" "build" {
  for_each = toset(local.build_project_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.build.email}"
}

resource "google_artifact_registry_repository_iam_member" "build_writer" {
  location   = google_artifact_registry_repository.hr.location
  repository = google_artifact_registry_repository.hr.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build.email}"
}

resource "google_artifact_registry_repository_iam_member" "runtime_readers" {
  for_each = {
    agent = google_service_account.agent.email
    acl   = google_service_account.acl.email
  }
  location   = google_artifact_registry_repository.hr.location
  repository = google_artifact_registry_repository.hr.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value}"
}

# The person/CI running deploy.sh submits builds as hr-build-sa.
# ADC tokens usually lack the userinfo.email scope, so prefer the explicit variable.
data "google_client_openid_userinfo" "me" {}

locals {
  deployer_member = var.deployer_member != "" ? var.deployer_member : (
    data.google_client_openid_userinfo.me.email == null ? "" : "user:${data.google_client_openid_userinfo.me.email}"
  )
}

resource "google_service_account_iam_member" "deployer_acts_as_build" {
  count              = local.deployer_member == "" ? 0 : 1
  service_account_id = google_service_account.build.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}

# Model Armor calls Sensitive Data Protection on our behalf for the advanced SDP
# templates (SDD CON-7); its service agent needs DLP access.
resource "google_project_iam_member" "model_armor_dlp" {
  for_each   = toset(["roles/dlp.user", "roles/dlp.reader"])
  project    = var.project_id
  role       = each.value
  member     = "serviceAccount:service-${local.project_number}@gcp-sa-modelarmor.iam.gserviceaccount.com"
  depends_on = [google_model_armor_template.input]
}
