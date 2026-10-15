# The AWS deployment of the telemetry platform.
#
# What this provisions, and what it deliberately does not.
#
# Provisioned here:
#   network      VPC, subnets across availability zones, NAT, VPC endpoints
#   iot          IoT Core thing type, device policy, topic rule into Kinesis
#   streaming    Kinesis data stream, plus a Firehose archive into S3
#   storage      S3 archive bucket, ECR repositories
#   eks          Kubernetes cluster and a managed node group
#
# Not provisioned here:
#   TimescaleDB  RDS does not offer the TimescaleDB extension. The two real
#                options are Timescale Cloud, which is a separate provider and a
#                separate account, or a StatefulSet on EKS. This repository takes
#                the second, so the database is in infra/k8s rather than here.
#                docs/architecture.md sets out the trade-off.
#   BigQuery     A different cloud. infra/terraform/gcp.
#   Secrets      Values are created empty; the contents are written out of band,
#                because a secret in Terraform state is a secret in a file.
#
# Nothing here has ever been applied. It is written against the documented
# resource schemas, validated in CI, and has never seen a real AWS account. Read
# it as a design that plans cleanly, not as infrastructure with a live state
# file behind it. docs/limitations.md says the same thing more bluntly.

locals {
  name = "${var.project}-${var.environment}"

  # The topic hierarchy the fleet publishes to. Kept in one place so the IoT
  # policy, the topic rule and the documentation cannot drift apart.
  telemetry_topic_filter = "dt/${var.tenant}/+/+/telemetry"
  status_topic_filter    = "dt/${var.tenant}/+/+/status"
  command_topic_prefix   = "cmd/${var.tenant}"
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "network" {
  source = "./modules/network"

  name               = local.name
  vpc_cidr           = var.vpc_cidr
  availability_zones = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)
  single_nat_gateway = var.single_nat_gateway
}

module "storage" {
  source = "./modules/storage"

  name                   = local.name
  archive_retention_days = var.archive_retention_days
  archive_expiry_days    = var.archive_expiry_days
  ecr_repositories       = ["ingest", "processing", "api", "alerting", "web"]
}

module "streaming" {
  source = "./modules/streaming"

  name            = local.name
  shard_count     = var.kinesis_shard_count
  stream_mode     = var.kinesis_mode
  retention_hours = var.kinesis_retention_hours
  archive_bucket  = module.storage.archive_bucket_arn
  archive_prefix  = "raw/"
}

module "iot" {
  source = "./modules/iot"

  name                   = local.name
  tenant                 = var.tenant
  thing_type             = var.iot_thing_type
  telemetry_topic_filter = local.telemetry_topic_filter
  status_topic_filter    = local.status_topic_filter
  command_topic_prefix   = local.command_topic_prefix
  kinesis_stream_name    = module.streaming.stream_name
  kinesis_stream_arn     = module.streaming.stream_arn
  dead_letter_stream_arn = module.streaming.dead_letter_stream_arn
  create_ca              = var.create_iot_ca
}

module "eks" {
  source = "./modules/eks"

  name               = local.name
  kubernetes_version = var.kubernetes_version
  vpc_id             = module.network.vpc_id
  subnet_ids         = module.network.private_subnet_ids
  instance_types     = var.node_instance_types
  capacity_type      = var.node_capacity_type
  min_size           = var.node_min_size
  max_size           = var.node_max_size
  desired_size       = var.node_desired_size

  # The processing and API pods read from Kinesis and write to S3. IRSA binds a
  # Kubernetes service account to an IAM role, so no static credentials are
  # mounted into a pod and nothing has to be rotated by hand.
  workload_policy_arns = [
    module.streaming.consumer_policy_arn,
    module.storage.archive_writer_policy_arn,
  ]
}
