# S3 archive bucket and the ECR repositories the images are pushed to.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

variable "name" { type = string }
variable "archive_retention_days" { type = number }
variable "archive_expiry_days" { type = number }
variable "ecr_repositories" { type = list(string) }

# Bucket names are globally unique across every AWS account, so a fixed name
# fails for anyone but the first person to apply this.
resource "random_id" "suffix" {
  byte_length = 4
}

resource "aws_s3_bucket" "archive" {
  bucket = "${var.name}-archive-${random_id.suffix.hex}"
  tags   = { Name = "${var.name}-archive" }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket = aws_s3_bucket.archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # S3 makes a KMS call per object without this. With Firehose writing an
    # object every five minutes per partition it is a real cost and a real
    # throttling risk.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id

  versioning_configuration {
    # The archive is append-only by construction: Firehose writes each object
    # once under a timestamped key and nothing ever overwrites one. Versioning
    # would only add storage cost and noncurrent versions to clean up.
    status = "Disabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    id     = "tier-and-expire"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    transition {
      days          = var.archive_retention_days
      storage_class = "GLACIER_IR"
    }

    dynamic "expiration" {
      for_each = var.archive_expiry_days > 0 ? [1] : []
      content {
        days = var.archive_expiry_days
      }
    }

    # Firehose uses multipart uploads. An aborted one leaves parts that are
    # charged for and never appear in a bucket listing, which is a classic
    # source of a storage bill nobody can account for.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "expire-delivery-errors"
    status = "Enabled"

    filter {
      prefix = "errors/"
    }

    expiration {
      days = 30
    }
  }
}

# --- Container images ------------------------------------------------------

resource "aws_ecr_repository" "service" {
  for_each = toset(var.ecr_repositories)

  name                 = "${var.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE" # a tag must always mean the same image

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = { Name = "${var.name}-${each.value}" }
}

resource "aws_ecr_lifecycle_policy" "service" {
  for_each = aws_ecr_repository.service

  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 20 tagged images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v", "sha-"]
          countType     = "imageCountMoreThan"
          countNumber   = 20
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
    ]
  })
}

# --- Archive writer permissions -------------------------------------------

data "aws_iam_policy_document" "archive_writer" {
  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.archive.arn, "${aws_s3_bucket.archive.arn}/*"]
  }
}

resource "aws_iam_policy" "archive_writer" {
  name   = "${var.name}-archive-writer"
  policy = data.aws_iam_policy_document.archive_writer.json
}

# --- Outputs ---------------------------------------------------------------

output "archive_bucket_name" { value = aws_s3_bucket.archive.bucket }
output "archive_bucket_arn" { value = aws_s3_bucket.archive.arn }
output "archive_writer_policy_arn" { value = aws_iam_policy.archive_writer.arn }
output "ecr_repository_urls" {
  value = { for name, repo in aws_ecr_repository.service : name => repo.repository_url }
}
