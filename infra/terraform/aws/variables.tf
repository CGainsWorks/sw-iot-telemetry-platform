variable "project" {
  description = "Short project name, used as a prefix on every resource name."
  type        = string
  default     = "iot-telemetry"

  validation {
    # Resource names built from this go into places with tight character rules,
    # notably ECR repository names and Kinesis stream names.
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.project))
    error_message = "project must be 3 to 25 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment. Part of every resource name."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-west-2"
}

# --- Network ---------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.60.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "availability_zone_count" {
  description = "How many availability zones to spread subnets across."
  type        = number
  default     = 3

  validation {
    # Two is the minimum EKS accepts. Three is the minimum for a quorum-based
    # workload such as a Kafka or a Timescale replica set.
    condition     = var.availability_zone_count >= 2 && var.availability_zone_count <= 4
    error_message = "availability_zone_count must be between 2 and 4."
  }
}

variable "single_nat_gateway" {
  description = <<-EOT
    Use one NAT gateway for the whole VPC rather than one per availability zone.

    Cheaper by roughly 35 USD per month per zone avoided, at the cost of the NAT
    becoming a single point of failure and a cross-zone data charge on egress.
    Sensible for dev, not for prod. See docs/cost_model.md.
  EOT
  type        = bool
  default     = true
}

# --- IoT Core --------------------------------------------------------------

variable "tenant" {
  description = "Tenant segment of the MQTT topic hierarchy."
  type        = string
  default     = "acme"
}

variable "iot_thing_type" {
  description = "IoT thing type name for the sensor fleet."
  type        = string
  default     = "telemetry-sensor"
}

variable "create_iot_ca" {
  description = <<-EOT
    Register a self-signed certificate authority with IoT Core so devices can be
    provisioned with certificates this account signs.

    Off by default: the CA private key would end up in Terraform state, and
    state is not a key store. The production path is to register a CA whose key
    lives in a hardware module or in ACM Private CA, which is a manual,
    audited step. See docs/provisioning.md.
  EOT
  type        = bool
  default     = false
}

# --- Streaming -------------------------------------------------------------

variable "kinesis_shard_count" {
  description = <<-EOT
    Shards on the telemetry stream, used only when kinesis_mode is PROVISIONED.

    One shard ingests 1 MB/s or 1000 records/s. Size it from the benchmark:
    fleet size times publish rate times readings per message.
  EOT
  type        = number
  default     = 2

  validation {
    condition     = var.kinesis_shard_count >= 1 && var.kinesis_shard_count <= 500
    error_message = "kinesis_shard_count must be between 1 and 500."
  }
}

variable "kinesis_mode" {
  description = <<-EOT
    PROVISIONED or ON_DEMAND.

    ON_DEMAND removes capacity planning and costs several times more per GB at
    steady load. A telemetry fleet has a predictable, flat rate, which is the
    case PROVISIONED is for.
  EOT
  type        = string
  default     = "PROVISIONED"

  validation {
    condition     = contains(["PROVISIONED", "ON_DEMAND"], var.kinesis_mode)
    error_message = "kinesis_mode must be PROVISIONED or ON_DEMAND."
  }
}

variable "kinesis_retention_hours" {
  description = "Stream retention. 24 hours is the free tier; beyond that is charged."
  type        = number
  default     = 24

  validation {
    condition     = var.kinesis_retention_hours >= 24 && var.kinesis_retention_hours <= 8760
    error_message = "kinesis_retention_hours must be between 24 and 8760."
  }
}

# --- Compute ---------------------------------------------------------------

variable "kubernetes_version" {
  description = "EKS control plane version."
  type        = string
  default     = "1.31"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group, in preference order."
  type        = list(string)
  default     = ["m7g.large", "m6g.large"]
}

variable "node_capacity_type" {
  description = <<-EOT
    ON_DEMAND or SPOT.

    Spot is roughly 70 per cent cheaper and can be reclaimed at two minutes'
    notice. The stateless services tolerate that; TimescaleDB does not, which is
    why the manifests keep it off spot nodes with a taint.
  EOT
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_min_size" {
  description = "Minimum nodes in the managed node group."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Maximum nodes in the managed node group."
  type        = number
  default     = 6
}

variable "node_desired_size" {
  description = "Desired nodes at creation."
  type        = number
  default     = 3
}

# --- Storage ---------------------------------------------------------------

variable "archive_retention_days" {
  description = <<-EOT
    Days before raw archived telemetry moves to Glacier Instant Retrieval.

    The archive exists so that a bug in the processing service is recoverable by
    replay. Anything older than a quarter has almost certainly been reprocessed
    or written off, so it goes to the cheaper tier.
  EOT
  type        = number
  default     = 90
}

variable "archive_expiry_days" {
  description = "Days before archived telemetry is deleted entirely. 0 keeps it forever."
  type        = number
  default     = 730
}
