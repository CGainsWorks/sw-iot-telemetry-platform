-- Routes fleet telemetry into the Kinesis data stream.
--
-- Applied by infra/terraform/aws/modules/iot as aws_iot_topic_rule.telemetry.
-- SQL version 2016-03-23.
--
-- `SELECT *` keeps the device's payload intact. The added underscore-prefixed
-- fields are what IoT Core knows and the payload does not:
--
--   _client_id        the identity authenticated from the client certificate.
--                     The single most important field here: the consumer
--                     compares it against the payload's device_id and
--                     dead-letters any mismatch. Without it the authenticated
--                     identity is lost at the first hop.
--   _topic_site_id    topic segment 3, which the device policy allows as a
--                     wildcard so a device can move site without a new
--                     certificate.
--   _topic_device_id  topic segment 4, which the policy pins to the thing name.
--   _ingest_ts        platform receipt time in epoch milliseconds. Subtracting
--                     the payload's own ts gives device-to-ingest lag, which is
--                     how a broken device clock is told apart from real lag.
--
-- Note that topic() is 1-indexed in IoT Core rule SQL, so dt/{tenant}/{site}/
-- {device}/telemetry puts the site at 3 and the device at 4.

SELECT
  *,
  clientid()  AS _client_id,
  topic(2)    AS _topic_tenant,
  topic(3)    AS _topic_site_id,
  topic(4)    AS _topic_device_id,
  timestamp() AS _ingest_ts
FROM 'dt/acme/+/+/telemetry'
