# Governance Plane (SDD §4.6, NFR-1.2): structured audit records (allowed AND denied)
# are written to stdout with the marker `hr_audit=true` (app/integration/audit.py);
# Cloud Logging routes them to BigQuery for analysis.

resource "google_bigquery_dataset" "audit" {
  dataset_id                 = "hr_audit"
  friendly_name              = "HR agent audit trail"
  description                = "SDD §4.6 — every turn incl. PDP denials and guardrail blocks"
  location                   = var.region
  delete_contents_on_destroy = !var.deletion_protection
  depends_on                 = [google_project_service.apis]
}

resource "google_logging_project_sink" "audit_to_bq" {
  name                   = "hr-audit-to-bigquery"
  destination            = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.audit.dataset_id}"
  filter                 = "jsonPayload.hr_audit=true"
  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_writer" {
  dataset_id = google_bigquery_dataset.audit.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.audit_to_bq.writer_identity
}

# Leading indicators (SDD §4.6): a spike in denials or blocks means an attack or a regression.
resource "google_logging_metric" "pdp_denials" {
  name        = "hr_pdp_denials"
  description = "PDP DENY decisions (SDD §4.6)"
  filter      = "jsonPayload.hr_audit=true AND jsonPayload.pdp_decision=\"DENY\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "guardrail_blocks" {
  name        = "hr_guardrail_blocks"
  description = "Guardrail block events (SDD §4.6)"
  filter      = "jsonPayload.hr_audit=true AND jsonPayload.event=~\"^guardrail\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_logging_metric" "retrieval_errors" {
  name        = "hr_retrieval_errors"
  description = "RAG Engine failures that forced a refusal (SDD §5.4)"
  filter      = "jsonPayload.hr_audit=true AND jsonPayload.outcome=\"RETRIEVAL_ERROR\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}
