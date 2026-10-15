# Provider versions are pinned to a minor. Terraform's AWS provider makes
# breaking changes at major boundaries and adds resources at minor ones, so
# pinning the major alone would let a fresh `terraform init` change a plan that
# was reviewed weeks earlier.
terraform {
  required_version = "~> 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State lives in S3 with DynamoDB locking. Left as a partial configuration so
  # the bucket name is supplied at init time rather than committed:
  #
  #   terraform init \
  #     -backend-config="bucket=my-tf-state" \
  #     -backend-config="key=iot-telemetry/terraform.tfstate" \
  #     -backend-config="region=eu-west-2" \
  #     -backend-config="dynamodb_table=my-tf-locks"
  #
  # Commented out so that `terraform init -backend=false` works for validation
  # in CI, which is the only thing this repository's automation does with it.
  # backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "sw-iot-telemetry-platform"
    }
  }
}
