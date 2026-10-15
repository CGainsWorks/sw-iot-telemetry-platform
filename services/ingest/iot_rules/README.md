# AWS IoT Core topic rules

The managed equivalent of [`services/ingest`](../ingest). On AWS the bridge
process does not exist: a topic rule does the same routing inside IoT Core, and
the deployment applies [`infra/terraform/aws/modules/iot`](../../../infra/terraform/aws/modules/iot/main.tf)
rather than running a container.

The SQL in this directory is the authoritative copy, kept here next to the
validation code it has to agree with. The Terraform embeds the same statements;
a test asserts they match.

**Never applied.** No AWS account is behind this repository. See
[docs/limitations.md](../../../docs/limitations.md#no-aws-account).

## Why the rule SQL selects what it does

```sql
SELECT *, clientid() AS _client_id, topic(4) AS _topic_device_id, ...
```

`clientid()` is the identity IoT Core authenticated from the device's
certificate. Adding it to the payload is the whole reason this rule is more than
a passthrough: without it, the authenticated identity is lost at the first hop
and the consumer downstream has only the payload's own claim about who sent it.

That claim is exactly what the platform does not trust. The consumer compares
`_client_id` and `_topic_device_id` against the payload's `device_id` and
dead-letters a mismatch, which is the same check
[`ingest/validation.py`](../ingest/validation.py) performs locally against the
Mosquitto-authenticated topic.

Get this wrong and the certificate story collapses silently: everything still
works, and a compromised device can attribute its output to any other.

## Why there is an error action

A rule whose action fails has nowhere else to put the message, and IoT Core does
not retry indefinitely. Without an error action, a transient Kinesis throttle
loses every message sent during it. With one, they land on the dead-letter
stream and can be replayed.

## The difference from the local path

| | Local | AWS |
| --- | --- | --- |
| Who routes | `services/ingest`, a subscriber holding a certificate | an IoT Core rule |
| Identity source | the topic, authenticated by Mosquitto's ACL | `clientid()`, injected by the rule |
| Where validation happens | in the bridge, before the stream | in the consumer, after the stream |
| Rejections | written to the DLQ topic by the bridge | error action, or the consumer |
| Scaling | one replica; two would double every message | managed |

The consequence worth noting: on AWS, validation happens **after** the stream
rather than before it, so an invalid message is stored in Kinesis and rejected
by the consumer rather than never entering the stream. That is a real
difference. It costs a little Kinesis throughput for messages that will be
discarded, and it buys the message being replayable if the rejection turns out
to have been a bug in the validator.
