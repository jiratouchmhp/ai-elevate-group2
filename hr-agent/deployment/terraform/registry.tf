resource "google_artifact_registry_repository" "hr" {
  location      = var.region
  repository_id = "hr-agent"
  description   = "hr-agent and hr-acl container images"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.apis]
}

locals {
  registry = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.hr.repository_id}"
}
