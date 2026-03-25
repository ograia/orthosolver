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
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
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

variable "lean_engine_url" {
  type    = string
  default = "http://localhost:8081"
}

variable "orchestrator_image" {
  type    = string
  default = "gcr.io/orthos-ai-prod/orthos-orchestrator:latest"
}

variable "worker_image" {
  type    = string
  default = "gcr.io/orthos-ai-prod/orthos-worker:latest"
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
  service_names = [
    "orchestrator",
    "decomposition-worker",
    "decomposition-vetter-worker",
    "lemma-solver-worker",
    "lemma-vetter-worker",
  ]

  pubsub_topics = [
    "decomposition-jobs",
    "decomposition-vetting-jobs",
    "lemma-solver-jobs",
    "lemma-vetter-jobs",
    "lean-engine-jobs",
    "lean-engine-callbacks",
  ]
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
    "sqladmin.googleapis.com",
    "servicenetworking.googleapis.com",
    "vpcaccess.googleapis.com",
    "secretmanager.googleapis.com",
    "pubsub.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "main" {
  name                    = "orthos-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "main" {
  name          = "orthos-subnet"
  region        = var.region
  network       = google_compute_network.main.id
  ip_cidr_range = "10.40.0.0/20"
}

resource "google_compute_global_address" "private_service_range" {
  name          = "orthos-private-service-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.main.id
}

resource "google_service_networking_connection" "private_vpc_connection" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_service_range.name]
  depends_on              = [google_project_service.services]
}

resource "google_vpc_access_connector" "serverless_connector" {
  name          = "orthos-serverless-connector"
  region        = var.region
  ip_cidr_range = "10.50.0.0/28"
  network       = google_compute_network.main.name
  min_instances = 2
  max_instances = 6
  depends_on    = [google_project_service.services]
}

resource "google_storage_bucket" "artifacts" {
  name                        = "orthos-artifacts-${var.environment}"
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

resource "google_pubsub_topic" "job_topics" {
  for_each = toset(local.pubsub_topics)
  name     = "orthos-${each.key}"
}

resource "google_pubsub_topic" "dead_letter" {
  name = "orthos-dead-letter"
}

resource "google_pubsub_subscription" "job_subscriptions" {
  for_each = google_pubsub_topic.job_topics
  name     = "orthos-${each.key}-sub"
  topic    = each.value.name

  ack_deadline_seconds = 30
  message_retention_duration = "86400s"

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = 10
  }
}

resource "random_password" "db_password" {
  length  = 32
  special = true
}

resource "google_sql_database_instance" "main" {
  name             = "orthos-db-${var.environment}"
  database_version = "POSTGRES_16"
  region           = var.region

  settings {
    tier              = "db-custom-2-8192"
    availability_type = "REGIONAL"
    disk_size         = 100
    disk_type         = "PD_SSD"
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      start_time                     = "03:00"
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.main.id
    }
  }

  deletion_protection = true
  depends_on          = [google_service_networking_connection.private_vpc_connection]
}

resource "google_sql_database" "orthos" {
  name     = "orthos"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "orthos-app"
  instance = google_sql_database_instance.main.name
  password = random_password.db_password.result
}

resource "google_secret_manager_secret" "openai_key" {
  secret_id = "orthos-openai-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "orthos-db-password"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "db_password_version" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db_password.result
}

resource "google_service_account" "orchestrator" {
  account_id   = "orthos-orchestrator"
  display_name = "Orthos Orchestrator"
}

resource "google_service_account" "workers" {
  account_id   = "orthos-workers"
  display_name = "Orthos Worker Services"
}

resource "google_project_iam_member" "orchestrator_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.orchestrator.email}"
}

resource "google_project_iam_member" "worker_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/secretmanager.secretAccessor",
    "roles/storage.objectAdmin",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.workers.email}"
}

resource "google_cloud_run_v2_service" "services" {
  for_each = toset(local.service_names)
  name     = "orthos-${each.key}"
  location = var.region

  template {
    service_account = each.key == "orchestrator" ? google_service_account.orchestrator.email : google_service_account.workers.email
    timeout         = "3600s"

    scaling {
      min_instance_count = each.key == "orchestrator" ? 1 : 0
      max_instance_count = each.key == "orchestrator" ? 8 : 20
    }

    vpc_access {
      connector = google_vpc_access_connector.serverless_connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = each.key == "orchestrator" ? var.orchestrator_image : var.worker_image
      ports {
        container_port = 8080
      }
      env {
        name  = "ENV"
        value = var.environment
      }
      env {
        name  = "LEAN_ENGINE_BASE_URL"
        value = var.lean_engine_url
      }
      env {
        name  = "ARTIFACT_STORE_DIR"
        value = "/artifacts"
      }
      env {
        name  = "GCS_BUCKET"
        value = google_storage_bucket.artifacts.name
      }
      env {
        name  = "DATABASE_URL"
        value = "postgresql+psycopg://${google_sql_user.app.name}:${random_password.db_password.result}@/${google_sql_database.orthos.name}"
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

  depends_on = [google_project_service.services]
}

resource "google_cloud_run_service_iam_member" "public_invoker" {
  service  = google_cloud_run_v2_service.services["orchestrator"].name
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
    route_name = google_cloud_run_v2_service.services["orchestrator"].name
  }
}

output "orchestrator_url" {
  value = google_cloud_run_v2_service.services["orchestrator"].uri
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "Orthos Alerts Email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

locals {
  alert_channels = var.alert_email == "" ? [] : [google_monitoring_notification_channel.email[0].name]
}

resource "google_monitoring_dashboard" "orthos_costs" {
  dashboard_json = jsonencode({
    displayName = "Orthos Cost Dashboard"
    gridLayout = {
      columns = "2"
      widgets = [
        {
          title = "Estimated Cost USD / day"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"custom.googleapis.com/orthos/estimated_cost_usd\""
                    aggregation = {
                      alignmentPeriod    = "3600s"
                      perSeriesAligner   = "ALIGN_SUM"
                      crossSeriesReducer = "REDUCE_SUM"
                    }
                  }
                }
              }
            ]
          }
        },
        {
          title = "Token usage by model"
          xyChart = {
            dataSets = [
              {
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = "metric.type=\"custom.googleapis.com/orthos/input_tokens\""
                    aggregation = {
                      alignmentPeriod  = "3600s"
                      perSeriesAligner = "ALIGN_SUM"
                    }
                  }
                }
              }
            ]
          }
        }
      ]
    }
  })
}

resource "google_monitoring_dashboard" "orthos_operational" {
  dashboard_json = jsonencode({
    displayName = "Orthos Operational Dashboard"
    gridLayout = {
      columns = "2"
      widgets = [
        {
          title = "Run success/failure"
          scorecard = {
            timeSeriesQuery = {
              timeSeriesFilter = {
                filter = "metric.type=\"custom.googleapis.com/orthos/problem_terminal\""
                aggregation = {
                  alignmentPeriod    = "3600s"
                  perSeriesAligner   = "ALIGN_SUM"
                  crossSeriesReducer = "REDUCE_SUM"
                }
              }
            }
          }
        },
        {
          title = "Queue backlog"
          scorecard = {
            timeSeriesQuery = {
              timeSeriesFilter = {
                filter = "metric.type=\"custom.googleapis.com/orthos/worker_queue_backlog\""
                aggregation = {
                  alignmentPeriod    = "300s"
                  perSeriesAligner   = "ALIGN_MAX"
                  crossSeriesReducer = "REDUCE_MAX"
                }
              }
            }
          }
        }
      ]
    }
  })
}

resource "google_monitoring_alert_policy" "high_failure_rate" {
  display_name = "Orthos High Failure Rate (P2)"
  combiner     = "OR"
  notification_channels = local.alert_channels
  documentation {
    content = "High failure rate detected. Runbook: ${var.operations_runbook_url}"
  }
  conditions {
    display_name = "Problem failures > threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/orthos/problem_failures\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "queue_backlog" {
  display_name = "Orthos Queue Backlog Growing (P2)"
  combiner     = "OR"
  notification_channels = local.alert_channels
  documentation {
    content = "Worker queue backlog is growing. Runbook: ${var.operations_runbook_url}"
  }
  conditions {
    display_name = "Backlog max > threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/orthos/worker_queue_backlog\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 50
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "stuck_runs" {
  display_name = "Orthos Stuck Runs (P1)"
  combiner     = "OR"
  notification_channels = local.alert_channels
  documentation {
    content = "Runs may be stuck (no progression). Runbook: ${var.operations_runbook_url}"
  }
  conditions {
    display_name = "No progress age > threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/orthos/run_staleness_seconds\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 900
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "timeout_spike" {
  display_name = "Orthos Timeout Spike (P2)"
  combiner     = "OR"
  notification_channels = local.alert_channels
  documentation {
    content = "Global timeout spike detected. Runbook: ${var.operations_runbook_url}"
  }
  conditions {
    display_name = "Timeout count > threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/orthos/global_timeouts\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}

resource "google_monitoring_alert_policy" "cost_burn_anomaly" {
  display_name = "Orthos Cost Burn Anomaly (P3)"
  combiner     = "OR"
  notification_channels = local.alert_channels
  documentation {
    content = "Cost burn anomaly detected. Runbook: ${var.operations_runbook_url}"
  }
  conditions {
    display_name = "Estimated cost/day > threshold"
    condition_threshold {
      filter          = "metric.type=\"custom.googleapis.com/orthos/estimated_cost_usd\""
      duration        = "3600s"
      comparison      = "COMPARISON_GT"
      threshold_value = 50
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
}
