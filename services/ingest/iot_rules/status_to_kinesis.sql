-- Routes device lifecycle events into the Kinesis data stream.
--
-- Applied by infra/terraform/aws/modules/iot as aws_iot_topic_rule.status.
--
-- Same identity injection as the telemetry rule, for the same reason: a
-- lifecycle event attributed to the wrong device is worse than no event, because
-- it would mark a healthy device offline.
--
-- These are low volume, a handful per device per day, and they include the last
-- will and testament the broker publishes when a device drops without a
-- DISCONNECT. That is the only way to tell a crashed device from a quiet one
-- without waiting out a timeout.

SELECT
  *,
  clientid()  AS _client_id,
  topic(2)    AS _topic_tenant,
  topic(3)    AS _topic_site_id,
  topic(4)    AS _topic_device_id,
  timestamp() AS _ingest_ts
FROM 'dt/acme/+/+/status'
