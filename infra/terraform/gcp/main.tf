# BigQuery analytical layer.
#
# A separate root from the AWS one, on purpose. They have different providers,
# different credentials and different lifecycles: the AWS stack is the running
# platform and changes when the platform changes, whereas the analytical layer
# is downstream and changes when someone wants a new report. Combining them
# would mean every schema tweak plans against a live EKS cluster.
#
# The data path is: TimescaleDB continuous aggregates are exported to Parquet by
# analytics/export, landed in GCS, and loaded into the tables defined here. See
# docs/architecture.md for why the analytical copy exists at all rather than
# pointing dashboards at TimescaleDB.

terraform {
  required_version = "~> 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.10"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

variable "project_id" {
  description = "GCP project id."
  type        = string
}

variable "region" {
  description = "Region for the dataset and bucket. Keep them together or every query pays egress."
  type        = string
  default     = "europe-west2"
}

variable "dataset_id" {
  description = "BigQuery dataset name."
  type        = string
  default     = "iot_telemetry"
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "table_expiration_days" {
  description = <<-EOT
    Default partition expiry on the fact tables.

    Set generously: the point of the analytical copy is the long view that
    TimescaleDB's retention deliberately throws away.
  EOT
  type        = number
  default     = 1095
}

locals {
  labels = {
    project     = "iot-telemetry"
    environment = var.environment
    managed_by  = "terraform"
  }

  # Table schemas live as JSON next to the queries so the loader and Terraform
  # read exactly the same definition.
  schema_dir = "${path.module}/../../../analytics/schemas"
}

# --- Landing bucket --------------------------------------------------------

resource "google_storage_bucket" "landing" {
  name          = "${var.project_id}-${var.dataset_id}-landing"
  location      = upper(var.region)
  force_destroy = false

  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      # Parquet in the landing bucket is a staging copy: once it is loaded into
      # BigQuery it has served its purpose. The durable copy of the raw data is
      # the S3 archive on the AWS side.
      type = "Delete"
    }
  }

  labels = local.labels
}

# --- Dataset ---------------------------------------------------------------

resource "google_bigquery_dataset" "telemetry" {
  dataset_id  = var.dataset_id
  location    = upper(var.region)
  description = "Analytical copy of the IoT telemetry platform's rollups"

  default_partition_expiration_ms = var.table_expiration_days * 24 * 60 * 60 * 1000

  labels = local.labels
}

# --- Tables ----------------------------------------------------------------

resource "google_bigquery_table" "telemetry_hourly" {
  dataset_id = google_bigquery_dataset.telemetry.dataset_id
  table_id   = "telemetry_hourly"

  description = "Hourly rollup per device and metric, exported from telemetry_1h"

  # Partitioning by hour would create far too many partitions for a multi-year
  # table: BigQuery caps a table at 10,000 partitions, which is under 14 months
  # of hours. Daily partitioning with clustering gives the same pruning.
  time_partitioning {
    type          = "DAY"
    field         = "bucket"
    expiration_ms = var.table_expiration_days * 24 * 60 * 60 * 1000
  }

  # Clustering order matters: it is a prefix, so put the column most queries
  # filter on first. Almost every question here starts with a metric or a site.
  clustering = ["metric", "site_id", "device_id"]

  schema = file("${local.schema_dir}/telemetry_hourly.json")

  labels = local.labels
}

resource "google_bigquery_table" "device_dim" {
  dataset_id = google_bigquery_dataset.telemetry.dataset_id
  table_id   = "device_dim"

  description = "Device registry snapshot, one row per device per export"

  time_partitioning {
    type  = "DAY"
    field = "snapshot_date"
  }

  clustering = ["site_id", "device_id"]

  schema = file("${local.schema_dir}/device_dim.json")

  labels = local.labels
}

resource "google_bigquery_table" "alert_history" {
  dataset_id = google_bigquery_dataset.telemetry.dataset_id
  table_id   = "alert_history"

  description = "Every alert raised, for reliability reporting"

  time_partitioning {
    type  = "DAY"
    field = "fired_at"
  }

  clustering = ["rule_id", "severity", "device_id"]

  schema = file("${local.schema_dir}/alert_history.json")

  labels = local.labels
}

# --- Loader identity -------------------------------------------------------

resource "google_service_account" "loader" {
  account_id   = "${var.dataset_id}-loader"
  display_name = "Loads telemetry exports into BigQuery"
}

resource "google_bigquery_dataset_iam_member" "loader" {
  dataset_id = google_bigquery_dataset.telemetry.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.loader.email}"
}

resource "google_storage_bucket_iam_member" "loader" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.loader.email}"
}

# The loader also needs to run load jobs, which is a project-level permission
# rather than a dataset-level one.
resource "google_project_iam_member" "loader_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.loader.email}"
}

# --- Outputs ---------------------------------------------------------------

output "dataset_id" { value = google_bigquery_dataset.telemetry.dataset_id }
output "landing_bucket" { value = google_storage_bucket.landing.name }
output "loader_service_account" { value = google_service_account.loader.email }
output "table_ids" {
  value = [
    google_bigquery_table.telemetry_hourly.table_id,
    google_bigquery_table.device_dim.table_id,
    google_bigquery_table.alert_history.table_id,
  ]
}
