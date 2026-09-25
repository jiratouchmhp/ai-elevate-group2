# Altostrat HR Agent — MVP 1 infrastructure (SDD §1.3, §4, §7.2).
# Remote state lives in a GCS bucket created by deployment/deploy.sh (bootstrap step);
# the bucket name is passed at init time:
#   terraform init -backend-config="bucket=<project>-hr-agent-tfstate"

terraform {
  required_version = ">= 1.6"

  backend "gcs" {
    prefix = "hr-agent/mvp"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 7.0, < 9.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 7.0, < 9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project               = var.project_id
  region                = var.region
  default_labels        = local.labels
  user_project_override = true
  billing_project       = var.project_id
}

provider "google-beta" {
  project               = var.project_id
  region                = var.region
  default_labels        = local.labels
  user_project_override = true
  billing_project       = var.project_id
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # SDD §6.5 FinOps attribution labels on every resource.
  labels = merge({
    app         = "hr-agent"
    env         = var.environment
    cost-centre = var.cost_centre
    managed-by  = "terraform"
  }, var.extra_labels)

  project_number = data.google_project.this.number
  placeholder    = "us-docker.pkg.dev/cloudrun/container/hello"
  agent_image    = var.agent_image != "" ? var.agent_image : local.placeholder
  acl_image      = var.acl_image != "" ? var.acl_image : local.placeholder
}
