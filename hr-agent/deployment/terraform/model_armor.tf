# Safety & Trust Plane (SDD §1.3 plane 2, D5, FR-1.3, FR-1.4).
#
# Basic SDP in Model Armor covers only six US-centric categories and cannot
# de-identify; there is no Singapore NRIC/FIN detector (SDD §4.5, CON-7). We therefore
# use ADVANCED SDP: our own inspect template with a custom SG_NRIC_FIN infoType plus a
# de-identify template, both referenced by the Model Armor templates.

locals {
  dlp_parent = "projects/${var.project_id}/locations/${var.region}"
  sdp_info_types = [
    "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD_NUMBER", "IBAN_CODE",
    "STREET_ADDRESS", "PASSPORT", "GCP_CREDENTIALS", "GCP_API_KEY",
  ]
  # Singapore NRIC / FIN: prefix S,T,F,G,M + 7 digits + checksum letter.
  sg_nric_regex = "\\b[STFGMstfgm]\\d{7}[A-Za-z]\\b"
}

resource "google_data_loss_prevention_inspect_template" "spii" {
  parent       = local.dlp_parent
  template_id  = "hr-spii-inspect"
  display_name = "HR agent SPII inspect (SG)"
  description  = "SDD §4.5 — SG NRIC/FIN custom infoType + contact SPII"

  inspect_config {
    min_likelihood = "POSSIBLE"

    dynamic "info_types" {
      for_each = local.sdp_info_types
      content {
        name = info_types.value
      }
    }

    custom_info_types {
      info_type {
        name = "SG_NRIC_FIN"
      }
      likelihood = "VERY_LIKELY"
      regex {
        pattern = local.sg_nric_regex
      }
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_data_loss_prevention_deidentify_template" "spii" {
  parent       = local.dlp_parent
  template_id  = "hr-spii-deidentify"
  display_name = "HR agent SPII de-identify"
  description  = "Replace detected SPII with its infoType label before persistence"

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config = true
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

locals {
  inspect_template_name    = "${local.dlp_parent}/inspectTemplates/${google_data_loss_prevention_inspect_template.spii.template_id}"
  deidentify_template_name = "${local.dlp_parent}/deidentifyTemplates/${google_data_loss_prevention_deidentify_template.spii.template_id}"
  rai_filters = {
    HATE_SPEECH       = "MEDIUM_AND_ABOVE"
    HARASSMENT        = "MEDIUM_AND_ABOVE"
    DANGEROUS         = "MEDIUM_AND_ABOVE"
    SEXUALLY_EXPLICIT = "MEDIUM_AND_ABOVE"
  }
}

# INPUT template: injection/jailbreak, RAI, malicious URIs, inbound SPII.
resource "google_model_armor_template" "input" {
  location    = var.region
  template_id = "hr-input"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    # Not offered in every region (e.g. asia-southeast1) — toggle via var.model_armor_malicious_uri.
    dynamic "malicious_uri_filter_settings" {
      for_each = var.model_armor_malicious_uri ? [1] : []
      content {
        filter_enforcement = "ENABLED"
      }
    }
    rai_settings {
      dynamic "rai_filters" {
        for_each = local.rai_filters
        content {
          filter_type      = rai_filters.key
          confidence_level = rai_filters.value
        }
      }
    }
    sdp_settings {
      advanced_config {
        inspect_template    = local.inspect_template_name
        deidentify_template = local.deidentify_template_name
      }
    }
  }

  template_metadata {
    log_sanitize_operations = true
  }

  depends_on = [google_project_service.apis]
}

# OUTPUT template: toxicity, leakage (SPII), malicious URIs. No PI/jailbreak on output.
resource "google_model_armor_template" "output" {
  location    = var.region
  template_id = "hr-output"

  filter_config {
    # Not offered in every region (e.g. asia-southeast1) — toggle via var.model_armor_malicious_uri.
    dynamic "malicious_uri_filter_settings" {
      for_each = var.model_armor_malicious_uri ? [1] : []
      content {
        filter_enforcement = "ENABLED"
      }
    }
    rai_settings {
      dynamic "rai_filters" {
        for_each = local.rai_filters
        content {
          filter_type      = rai_filters.key
          confidence_level = rai_filters.value
        }
      }
    }
    sdp_settings {
      advanced_config {
        inspect_template    = local.inspect_template_name
        deidentify_template = local.deidentify_template_name
      }
    }
  }

  template_metadata {
    log_sanitize_operations = true
  }

  depends_on = [google_project_service.apis]
}
