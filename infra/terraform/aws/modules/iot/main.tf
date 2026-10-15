# AWS IoT Core: device identity, authorisation and the rule that routes
# telemetry into the stream.
#
# This module is the production equivalent of what Mosquitto and the ingest
# bridge do locally. The important part is the policy: it uses IoT policy
# variables so that one policy document, attached to every certificate, confines
# each device to its own topic space. Without those variables you would need one
# policy per device, which does not scale past a few hundred, or one permissive
# policy, which means any compromised device can publish as any other.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

variable "name" { type = string }
variable "tenant" { type = string }
variable "thing_type" { type = string }
variable "telemetry_topic_filter" { type = string }
variable "status_topic_filter" { type = string }
variable "command_topic_prefix" { type = string }
variable "kinesis_stream_name" { type = string }
variable "kinesis_stream_arn" { type = string }
variable "dead_letter_stream_arn" { type = string }
variable "create_ca" { type = bool }

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  topic_arn_prefix  = "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topic"
  client_arn_prefix = "arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:client"
}

resource "aws_iot_thing_type" "sensor" {
  name = var.thing_type

  properties {
    description           = "Environmental and machine sensor nodes for ${var.name}"
    searchable_attributes = ["site_id", "profile", "fw_version"]
  }
}

# --- Device policy ---------------------------------------------------------
#
# ${iot:Connection.Thing.ThingName} is substituted by IoT Core at authorisation
# time with the thing name the connection authenticated as. A device therefore
# cannot publish under any name but its own, whatever its payload claims. This
# is the same guarantee the local broker gets from `use_identity_as_username`
# plus a %u ACL pattern, and it is why ingest can treat the topic as trusted and
# the payload as a claim.

data "aws_iam_policy_document" "device" {
  # Connect only as the thing you are.
  statement {
    sid       = "ConnectAsSelfOnly"
    effect    = "Allow"
    actions   = ["iot:Connect"]
    resources = ["${local.client_arn_prefix}/$${iot:Connection.Thing.ThingName}"]
  }

  # Publish telemetry and status only beneath your own name. The site segment is
  # a wildcard because a device can be moved between sites without reissuing its
  # certificate; the device segment never is.
  statement {
    sid     = "PublishOwnTelemetry"
    effect  = "Allow"
    actions = ["iot:Publish"]
    resources = [
      "${local.topic_arn_prefix}/dt/${var.tenant}/*/$${iot:Connection.Thing.ThingName}/telemetry",
      "${local.topic_arn_prefix}/dt/${var.tenant}/*/$${iot:Connection.Thing.ThingName}/status",
      "${local.topic_arn_prefix}/${var.command_topic_prefix}/*/$${iot:Connection.Thing.ThingName}/res",
    ]
  }

  # Receive commands addressed to you.
  statement {
    sid       = "SubscribeOwnCommands"
    effect    = "Allow"
    actions   = ["iot:Subscribe"]
    resources = ["arn:aws:iot:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:topicfilter/${var.command_topic_prefix}/*/$${iot:Connection.Thing.ThingName}/req"]
  }

  statement {
    sid       = "ReceiveOwnCommands"
    effect    = "Allow"
    actions   = ["iot:Receive"]
    resources = ["${local.topic_arn_prefix}/${var.command_topic_prefix}/*/$${iot:Connection.Thing.ThingName}/req"]
  }
}

resource "aws_iot_policy" "device" {
  name   = "${var.name}-device"
  policy = data.aws_iam_policy_document.device.json
}

# --- Topic rule ------------------------------------------------------------
#
# The rule is the managed replacement for the ingest bridge. Note what the SQL
# selects: clientid() is the authenticated identity, added to the payload as
# `_client_id` so the downstream consumer can perform the same identity check
# the bridge performs locally. Without it the consumer would have only the
# payload's own claim, and the whole point of certificate-bound identity would be
# lost at the first hop.

resource "aws_iot_topic_rule" "telemetry" {
  name        = replace("${var.name}_telemetry", "-", "_")
  description = "Route fleet telemetry into the Kinesis stream"
  enabled     = true
  sql         = <<-SQL
    SELECT
      *,
      clientid() AS _client_id,
      topic(3)   AS _topic_site_id,
      topic(4)   AS _topic_device_id,
      timestamp() AS _ingest_ts
    FROM '${var.telemetry_topic_filter}'
  SQL
  sql_version = "2016-03-23"

  kinesis {
    role_arn    = aws_iam_role.rule.arn
    stream_name = var.kinesis_stream_name
    # Partition by device so one device's readings stay on one shard and
    # therefore stay in order.
    partition_key = "$${topic(4)}"
  }

  # A rule whose action fails has nowhere else to put the message, and IoT Core
  # does not retry indefinitely. The error action is the difference between a
  # transient Kinesis throttle costing nothing and it costing every message sent
  # during the throttle.
  error_action {
    kinesis {
      role_arn      = aws_iam_role.rule.arn
      stream_name   = element(split("/", var.dead_letter_stream_arn), 1)
      partition_key = "$${topic(4)}"
    }
  }
}

resource "aws_iot_topic_rule" "status" {
  name        = replace("${var.name}_status", "-", "_")
  description = "Route device lifecycle events into the Kinesis stream"
  enabled     = true
  sql         = <<-SQL
    SELECT
      *,
      clientid() AS _client_id,
      topic(4)   AS _topic_device_id,
      timestamp() AS _ingest_ts
    FROM '${var.status_topic_filter}'
  SQL
  sql_version = "2016-03-23"

  kinesis {
    role_arn      = aws_iam_role.rule.arn
    stream_name   = var.kinesis_stream_name
    partition_key = "$${topic(4)}"
  }
}

data "aws_iam_policy_document" "rule_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rule" {
  name               = "${var.name}-iot-rule"
  assume_role_policy = data.aws_iam_policy_document.rule_assume.json
}

data "aws_iam_policy_document" "rule" {
  statement {
    effect    = "Allow"
    actions   = ["kinesis:PutRecord", "kinesis:PutRecords"]
    resources = [var.kinesis_stream_arn, var.dead_letter_stream_arn]
  }
}

resource "aws_iam_policy" "rule" {
  name   = "${var.name}-iot-rule"
  policy = data.aws_iam_policy_document.rule.json
}

resource "aws_iam_role_policy_attachment" "rule" {
  role       = aws_iam_role.rule.name
  policy_arn = aws_iam_policy.rule.arn
}

# --- Optional CA -----------------------------------------------------------
#
# Off by default, and it should stay off outside a throwaway environment: the CA
# private key is generated by Terraform and therefore lives in Terraform state
# in plain text. State is not a key store. The production path is ACM Private CA
# or an offline hardware-backed CA whose certificate is registered by hand, which
# is a deliberate, audited step rather than something a plan applies.

resource "tls_private_key" "ca" {
  count     = var.create_ca ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "tls_self_signed_cert" "ca" {
  count = var.create_ca ? 1 : 0

  private_key_pem       = tls_private_key.ca[0].private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 87600 # 10 years

  subject {
    common_name  = "${var.name} device CA"
    organization = "CGainsWorks IoT"
  }

  allowed_uses = ["cert_signing", "crl_signing", "digital_signature"]
}

# --- Outputs ---------------------------------------------------------------

output "thing_type_name" { value = aws_iot_thing_type.sensor.name }
output "device_policy_name" { value = aws_iot_policy.device.name }
output "telemetry_rule_name" { value = aws_iot_topic_rule.telemetry.name }
output "rule_role_arn" { value = aws_iam_role.rule.arn }

output "ca_certificate_pem" {
  description = "Self-signed device CA, when create_ca is enabled."
  value       = var.create_ca ? tls_self_signed_cert.ca[0].cert_pem : null
}
