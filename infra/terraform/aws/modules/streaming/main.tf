# Kinesis Data Streams, plus a Firehose that archives the raw stream to S3.
#
# The archive is not a backup of the database. It is the raw record of what the
# fleet actually sent, kept so that a bug in the processing service can be fixed
# and the affected window replayed. Without it, a rollup computed wrongly is
# wrong permanently, because the raw hypertable only keeps 30 days and the
# stream keeps 24 hours.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

variable "name" { type = string }
variable "shard_count" { type = number }
variable "stream_mode" { type = string }
variable "retention_hours" { type = number }
variable "archive_bucket" { type = string }
variable "archive_prefix" { type = string }

resource "aws_kinesis_stream" "telemetry" {
  name             = "${var.name}-telemetry"
  retention_period = var.retention_hours

  # Only set for provisioned mode. On-demand rejects the argument entirely.
  shard_count = var.stream_mode == "PROVISIONED" ? var.shard_count : null

  stream_mode_details {
    stream_mode = var.stream_mode
  }

  # Server-side encryption with the AWS-managed key. Telemetry is not especially
  # sensitive on its own, but a fleet's movement and occupancy patterns are, and
  # the managed key costs nothing.
  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  # Shard-level metrics are charged per shard per metric. IncomingRecords and
  # WriteProvisionedThroughputExceeded are the two that tell you whether the
  # shard count is right, which is the question worth paying for.
  shard_level_metrics = [
    "IncomingRecords",
    "WriteProvisionedThroughputExceeded",
  ]

  tags = { Name = "${var.name}-telemetry" }
}

# Rejected messages. Separate stream rather than a separate partition key so a
# flood of rejections from a bad firmware rollout cannot consume the throughput
# the healthy fleet needs.
resource "aws_kinesis_stream" "dead_letter" {
  name             = "${var.name}-deadletter"
  retention_period = 168 # 7 days: long enough to investigate a bad rollout

  shard_count = var.stream_mode == "PROVISIONED" ? 1 : null

  stream_mode_details {
    stream_mode = var.stream_mode
  }

  encryption_type = "KMS"
  kms_key_id      = "alias/aws/kinesis"

  tags = { Name = "${var.name}-deadletter" }
}

# --- Archive ---------------------------------------------------------------

data "aws_iam_policy_document" "firehose_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["firehose.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "firehose" {
  name               = "${var.name}-firehose"
  assume_role_policy = data.aws_iam_policy_document.firehose_assume.json
}

data "aws_iam_policy_document" "firehose" {
  statement {
    effect = "Allow"
    actions = [
      "kinesis:DescribeStream",
      "kinesis:GetShardIterator",
      "kinesis:GetRecords",
      "kinesis:ListShards",
    ]
    resources = [aws_kinesis_stream.telemetry.arn]
  }

  statement {
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
    ]
    resources = [var.archive_bucket, "${var.archive_bucket}/*"]
  }
}

resource "aws_iam_policy" "firehose" {
  name   = "${var.name}-firehose"
  policy = data.aws_iam_policy_document.firehose.json
}

resource "aws_iam_role_policy_attachment" "firehose" {
  role       = aws_iam_role.firehose.name
  policy_arn = aws_iam_policy.firehose.arn
}

resource "aws_kinesis_firehose_delivery_stream" "archive" {
  name        = "${var.name}-archive"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.telemetry.arn
    role_arn           = aws_iam_role.firehose.arn
  }

  extended_s3_configuration {
    role_arn   = aws_iam_role.firehose.arn
    bucket_arn = var.archive_bucket

    # Hive-style partitioning so Athena and the BigQuery loader can prune by
    # date instead of scanning the whole archive.
    prefix              = "${var.archive_prefix}dt=!{timestamp:yyyy-MM-dd}/hour=!{timestamp:HH}/"
    error_output_prefix = "errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/"

    # 128 MB or 5 minutes, whichever comes first. Small objects are the classic
    # way to make an S3 archive expensive to query: a million 4 KB files cost
    # far more to list and scan than a few thousand large ones.
    buffering_size     = 128
    buffering_interval = 300

    compression_format = "GZIP"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = "/aws/kinesisfirehose/${var.name}-archive"
      log_stream_name = "s3-delivery"
    }
  }

  tags = { Name = "${var.name}-archive" }
}

# --- Consumer permissions --------------------------------------------------
#
# Attached to the processing service's IRSA role. Read only: nothing in the
# cluster has any reason to write back to the telemetry stream.

data "aws_iam_policy_document" "consumer" {
  statement {
    effect = "Allow"
    actions = [
      "kinesis:DescribeStream",
      "kinesis:DescribeStreamSummary",
      "kinesis:GetRecords",
      "kinesis:GetShardIterator",
      "kinesis:ListShards",
      "kinesis:SubscribeToShard",
    ]
    resources = [
      aws_kinesis_stream.telemetry.arn,
      aws_kinesis_stream.dead_letter.arn,
    ]
  }
}

resource "aws_iam_policy" "consumer" {
  name   = "${var.name}-stream-consumer"
  policy = data.aws_iam_policy_document.consumer.json
}

# --- Outputs ---------------------------------------------------------------

output "stream_name" { value = aws_kinesis_stream.telemetry.name }
output "stream_arn" { value = aws_kinesis_stream.telemetry.arn }
output "dead_letter_stream_name" { value = aws_kinesis_stream.dead_letter.name }
output "dead_letter_stream_arn" { value = aws_kinesis_stream.dead_letter.arn }
output "consumer_policy_arn" { value = aws_iam_policy.consumer.arn }
output "archive_stream_name" { value = aws_kinesis_firehose_delivery_stream.archive.name }
