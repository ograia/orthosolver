terraform {
  required_version = ">= 1.7.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    bucket = "orthos-terraform-state"
    prefix = "prod"
  }
}

variable "project_id" {
  type    = string
  default = "orthos-ai-prod"
}

variable "region" {
  type    = string
  default = "us-east1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "custom_domain" {
  type    = string
  default = ""
}

variable "orchestrator_image" {
  type    = string
  default = "gcr.io/orthos-ai-prod/orthos-orchestrator:latest"
}

variable "worker_image" {
  type    = string
  default = "gcr.io/orthos-ai-prod/orthos-worker:latest"
}

variable "lean_engine_image" {
  type    = string
  default = "gcr.io/orthos-ai-prod/orthos-lean-engine:latest"
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "operations_runbook_url" {
  type    = string
  default = "https://github.com/orthos-ai/nl-engine/blob/main/docs/OPERATIONS.md"
}

locals {
  worker_service_names = [
    "decomposition-worker",
    "decomposition-vetter-worker",
    "lemma-solver-worker",
    "lemma-vetter-worker",
  ]

  runtime_bucket_name = "orthos-runtime-${var.environment}"
  nl_state_prefix     = "nl-engine/state/${var.environment}"
  nl_artifact_prefix  = "nl-engine/artifacts/${var.environment}"
  lean_state_prefix   = "lean-engine/state/${var.environment}"
  lean_artifact_prefix = "lean-engine/artifacts/${var.environment}"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "services" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "cloudbuild.googleapis.com",
    "monitoring.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "runtime" {
  name                        = local.runtime_bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      age = 180
    }
    action {
      type = "Delete"
    }
  }
}

resource "google_secret_manager_secret" "openai_key" {
  secret_id = "orthos-openai-api-key"
  replication {
    auto {}
  }
}

resource "google_service_account" "orchestrator" {
  account_id   = "orthos-orchestrator"
  display_name = "Orthos Orchestrator"
}

resource "google_service_account" "workers" {
  account_id   = "orthos-workers"
  display_name = "Orthos Worker Services"
}

resource "google_service_account" "lean_engine" {
  account_id   = "orthos-lean-engine"
  display_name = "Orthos Lean Engine"
}

resource "google_project_iam_member" "orchestrator_roles" {
  for_each = toset([
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.workers.email}"
}

resource "google_project_iam_member" "lean_engine_roles" {
  for_each = toset([
    "roles/storage.objectAdmin",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.lean_engine.email}"
}

resource "google_cloud_run_v2_service" "lean_engine" {
  name     = "orthos-lean-engine"
  location = var.region

  template {
    service_account = google_service_account.lean_engine.email
    timeout         = "3600s"

    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }

    containers {
      image = var.lean_engine_image
      ports {
        container_port = 8080
      }
      env {
        name  = "LEAN_ENGINE_STORAGE_BACKEND"
        value = "gcs"
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.runtime.name
      }
      env {
        name  = "LEAN_ENGINE_GCS_STATE_PREFIX"
        value = local.lean_state_prefix
      }
      env {
        name  = "LEAN_ENGINE_GCS_ARTIFACT_PREFIX"
        value = local.lean_artifact_prefix
      }
      env {
        name  = "PORT"
        value = "8080"
      }
    }
  }

  depends_on = [google_project_service.services]
}

resource "google_cloud_run_v2_service" "orchestrator" {
  name     = "orthos-orchestrator"
  location = var.region

  template {
    service_account = google_service_account.orchestrator.email
    timeout         = "3600s"

    scaling {
      min_instance_count = 1
      max_instance_count = 8
    }

    containers {
      image = var.orchestrator_image
      ports {
        container_port = 8080
      }
      env {
        name  = "ENV"
        value = var.environment
      }
      env {
        name  = "STORAGE_BACKEND"
        value = "gcs"
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.runtime.name
      }
      env {
        name  = "GCS_STATE_PREFIX"
        value = local.nl_state_prefix
      }
      env {
        name  = "GCS_ARTIFACT_PREFIX"
        value = local.nl_artifact_prefix
      }
      env {
        name  = "LEAN_ENGINE_BASE_URL"
        value = google_cloud_run_v2_service.lean_engine.uri
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.services, google_cloud_run_v2_service.lean_engine]
}

resource "google_cloud_run_v2_service" "worker_services" {
  for_each = toset(local.worker_service_names)
  name     = "orthos-${each.key}"
  location = var.region

  template {
    service_account = google_service_account.workers.email
    timeout         = "3600s"

    scaling {
      min_instance_count = 0
      max_instance_count = 20
    }

    containers {
      image = var.worker_image
      ports {
        container_port = 8080
      }
      env {
        name  = "ENV"
        value = var.environment
      }
      env {
        name  = "STORAGE_BACKEND"
        value = "gcs"
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.runtime.name
      }
      env {
        name  = "GCS_STATE_PREFIX"
        value = local.nl_state_prefix
      }
      env {
        name  = "GCS_ARTIFACT_PREFIX"
        value = local.nl_artifact_prefix
      }
      env {
        name  = "LEAN_ENGINE_BASE_URL"
        value = google_cloud_run_v2_service.lean_engine.uri
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [google_project_service.services, google_cloud_run_v2_service.lean_engine]
}

resource "google_cloud_run_service_iam_member" "public_invoker" {
  service  = google_cloud_run_v2_service.orchestrator.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_domain_mapping" "orchestrator_domain" {
  count    = var.custom_domain == "" ? 0 : 1
  provider = google-beta
  location = var.region
  name     = var.custom_domain

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.orchestrator.name
  }
}

output "runtime_bucket" {
  value = google_storage_bucket.runtime.name
}

output "orchestrator_url" {
  value = google_cloud_run_v2_service.orchestrator.uri
}

output "lean_engine_url" {
  value = google_cloud_run_v2_service.lean_engine.uri
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Orthos Alerts Email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}
