# Plan-time tests for the IoT Core module, with mocked providers.
#
#   terraform -chdir=infra/terraform/aws/modules/iot init -backend=false
#   terraform -chdir=infra/terraform/aws/modules/iot test
#
# No AWS account and no credentials: the AWS and TLS providers are mocked and
# the two data sources the module reads are given fixed values. What is checked
# is the module's own logic, the parts that `validate` cannot see because they
# are values rather than types:
#
#   * the device policy confines every thing to its own client id and its own
#     topics through ${iot:Connection.Thing.ThingName}, mirroring the local
#     broker's `%u` ACL patterns in config/mosquitto/acl;
#   * the topic rule carries the authenticated client id downstream, so ingest
#     can make the same identity check it makes locally;
#   * the dead-letter stream name is correctly extracted from its ARN, which
#     docs/limitations.md flagged as unverified;
#   * the module does not create a CA unless asked.
#
# This is not an apply. Whether IoT Core accepts the policy and the rule is
# still untested; see docs/limitations.md.

mock_provider "aws" {
  # A mocked data source returns random strings for computed attributes, which
  # the IAM and IoT resources then reject as policy JSON. The assertions below
  # read the policy statements, which are configuration and therefore real, so
  # the rendered JSON only has to be well formed.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  override_data {
    target = data.aws_region.current
    values = {
      name = "eu-west-2"
    }
  }

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }
}

mock_provider "tls" {}

variables {
  name                   = "iot-telemetry-test"
  tenant                 = "acme"
  thing_type             = "telemetry-sensor"
  telemetry_topic_filter = "dt/acme/+/+/telemetry"
  status_topic_filter    = "dt/acme/+/+/status"
  command_topic_prefix   = "cmd/acme"
  kinesis_stream_name    = "iot-telemetry-test-telemetry"
  kinesis_stream_arn     = "arn:aws:kinesis:eu-west-2:123456789012:stream/iot-telemetry-test-telemetry"
  dead_letter_stream_arn = "arn:aws:kinesis:eu-west-2:123456789012:stream/iot-telemetry-test-deadletter"
  create_ca              = false
}

run "a_device_connects_only_as_itself" {
  command = plan

  assert {
    condition = data.aws_iam_policy_document.device.statement[0].resources == toset([
      "arn:aws:iot:eu-west-2:123456789012:client/$${iot:Connection.Thing.ThingName}",
    ])
    error_message = "iot:Connect must be limited to the client id equal to the thing name."
  }

  assert {
    condition     = data.aws_iam_policy_document.device.statement[0].actions == toset(["iot:Connect"])
    error_message = "The connect statement must grant iot:Connect and nothing else."
  }
}

run "a_device_publishes_only_beneath_its_own_name" {
  command = plan

  # The same three grants the local ACL gives a device through %u:
  #   pattern write dt/acme/+/%u/telemetry
  #   pattern write dt/acme/+/%u/status
  #   pattern write cmd/acme/+/%u/res
  assert {
    condition = data.aws_iam_policy_document.device.statement[1].resources == toset([
      "arn:aws:iot:eu-west-2:123456789012:topic/dt/acme/*/$${iot:Connection.Thing.ThingName}/telemetry",
      "arn:aws:iot:eu-west-2:123456789012:topic/dt/acme/*/$${iot:Connection.Thing.ThingName}/status",
      "arn:aws:iot:eu-west-2:123456789012:topic/cmd/acme/*/$${iot:Connection.Thing.ThingName}/res",
    ])
    error_message = "Publish must be confined to the thing's own telemetry, status and command response topics."
  }

  # And the one read grant: pattern read cmd/acme/+/%u/req
  assert {
    condition = data.aws_iam_policy_document.device.statement[2].resources == toset([
      "arn:aws:iot:eu-west-2:123456789012:topicfilter/cmd/acme/*/$${iot:Connection.Thing.ThingName}/req",
    ])
    error_message = "Subscribe must be confined to the thing's own command topic."
  }

  assert {
    condition = data.aws_iam_policy_document.device.statement[3].resources == toset([
      "arn:aws:iot:eu-west-2:123456789012:topic/cmd/acme/*/$${iot:Connection.Thing.ThingName}/req",
    ])
    error_message = "Receive must be confined to the thing's own command topic."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.device.statement :
      alltrue([for r in s.resources : !endswith(r, "/#") && !endswith(r, "/*") && !strcontains(r, "/+/")])
    ])
    error_message = "No device grant may end in a wildcard or use an MQTT wildcard in an ARN."
  }
}

run "the_topic_rule_carries_the_authenticated_identity" {
  command = plan

  assert {
    condition     = strcontains(aws_iot_topic_rule.telemetry.sql, "clientid() AS _client_id")
    error_message = "The rule must pass the authenticated client id downstream for the identity check."
  }

  assert {
    condition     = strcontains(aws_iot_topic_rule.telemetry.sql, "FROM 'dt/acme/+/+/telemetry'")
    error_message = "The rule must select the fleet's telemetry topics."
  }

  assert {
    condition     = [for k in aws_iot_topic_rule.telemetry.kinesis : k.partition_key] == ["$${topic(4)}"]
    error_message = "Partitioning by the device segment keeps one device's readings in order."
  }
}

run "the_dead_letter_stream_name_is_taken_from_its_arn" {
  command = plan

  assert {
    condition = [
      for a in aws_iot_topic_rule.telemetry.error_action : [for k in a.kinesis : k.stream_name]
    ] == [["iot-telemetry-test-deadletter"]]
    error_message = "The error action must name the dead-letter stream, not a fragment of its ARN."
  }
}

run "no_ca_unless_asked" {
  command = plan

  assert {
    condition     = length(tls_private_key.ca) == 0 && output.ca_certificate_pem == null
    error_message = "The module must not generate a CA key, which would land in state, by default."
  }
}

run "a_ca_when_asked" {
  command = plan

  variables {
    create_ca = true
  }

  assert {
    condition     = length(tls_private_key.ca) == 1 && length(tls_self_signed_cert.ca) == 1
    error_message = "create_ca = true must create the CA key and certificate."
  }
}
