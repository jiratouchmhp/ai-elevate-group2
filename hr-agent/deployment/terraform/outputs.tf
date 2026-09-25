output "agent_url" {
  description = "Chat UI (IAP-protected)."
  value       = google_cloud_run_v2_service.agent.uri
}

output "acl_url" {
  description = "Integration Plane (IAM-gated; hr-agent-sa only)."
  value       = google_cloud_run_v2_service.acl.uri
}

output "artifact_registry" {
  value = local.registry
}

output "corpus_bucket" {
  value = google_storage_bucket.corpus.name
}

output "build_staging_bucket" {
  value = google_storage_bucket.build_staging.name
}

output "build_service_account" {
  value = google_service_account.build.email
}

output "agent_service_account" {
  value = google_service_account.agent.email
}

output "acl_service_account" {
  value = google_service_account.acl.email
}

output "vendor_pat_secret" {
  description = "Push the PAT with: deployment/deploy.sh push-secrets"
  value       = google_secret_manager_secret.vendor_pat.secret_id
}

output "model_armor_templates" {
  value = {
    input  = google_model_armor_template.input.id
    output = google_model_armor_template.output.id
  }
}

output "audit_dataset" {
  value = "${var.project_id}.${google_bigquery_dataset.audit.dataset_id}"
}
