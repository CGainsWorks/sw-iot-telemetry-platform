# Architecture

## The shape of it

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  EDGE                                                                       │
│                                                                             │
│   ESP32 + BME280            Device simulator                                │
│   edge/firmware             edge/simulator                                  │
│   compiled, not flashed     the one that actually runs                      │
│         │                          │                                        │
│         └──────────┬───────────────┘                                        │
│                    │  MQTT over mutual TLS, one certificate per device      │
└────────────────────┼────────────────────────────────────────────────────────┘
                     │  dt/{tenant}/{site}/{device}/telemetry
                     ▼
         ┌───────────────────────────┐
         │  BROKER                   │   local:  Mosquitto, require_certificate
         │  identity + authorisation │   AWS:    IoT Core, policy variables
         └───────────┬───────────────┘
                     │  the topic now carries an authenticated identity
                     ▼
         ┌───────────────────────────┐
         │  INGEST                   │   local:  services/ingest, a subscriber
         │  validate, reject, fan    │   AWS:    an IoT Core topic rule
         │  out one row per reading  │
         └─────┬───────────────┬─────┘
               │               │
      accepted │               │ rejected, with a reason
               ▼               ▼
      telemetry.normalised   telemetry.deadletter
         ┌───────────────────────────┐
         │  STREAM                   │   local:  Redpanda, Kafka protocol
         │  durable, replayable,     │   AWS:    Kinesis Data Streams
         │  partitioned by device    │           + Firehose archive to S3
         └───────────┬───────────────┘
                     ▼
         ┌───────────────────────────┐
         │  PROCESSING               │   services/processing
         │  write raw, maintain the  │
         │  live rollups, update the │
         │  device registry          │
         └───────────┬───────────────┘
                     ▼
    ┌────────────────────────────────────────────┐
    │  TIMESCALEDB                               │
    │                                            │
    │   telemetry            raw, 30 days        │
    │   telemetry_1m_live    seconds behind, 2d  │  ← written by the stream
    │   telemetry_1m         correct, 180 days   │  ← continuous aggregate
    │   telemetry_1h         correct, 730 days   │  ← aggregate of an aggregate
    │   devices, alerts, dead_letters            │
    └───────┬──────────────────────┬─────────────┘
            │                      │
            ▼                      ▼
    ┌───────────────┐      ┌──────────────────┐
    │  QUERY API    │      │  ALERTING        │
    │  services/api │      │  services/alert… │
    │               │      │  13 rules, every │
    │  picks which  │      │  15 seconds      │
    │  table to     │      └────────┬─────────┘
    │  read, and    │               │  alerts.fired
    │  says why     │               ▼
    └───────┬───────┘      ┌──────────────────┐
            │              │  log, webhook    │
            │              └──────────────────┘
            │  REST + WebSocket
            ▼
    ┌───────────────┐              ┌──────────────────────────────┐
    │  DASHBOARD    │              │  ANALYTICS                   │
    │  web/         │              │  Parquet → BigQuery          │
    │  React + TS   │              │  five queries, DuckDB-tested │
    └───────────────┘              └──────────────────────────────┘
```

## The decisions worth defending

### The simulator came first, and that is why everything else works

The brief asked for the simulator before the hardware. It is the single most
consequential decision in the repository.

By the time any firmware existed, the schema, the topic grammar, the identity
check, the rollups, the retention ladder, the alerting rules and the dashboard
had all been exercised against hundreds of thousands of messages, including
deliberately malformed ones. Eight fault modes are injectable, each mapping to a
platform path that is otherwise untestable without waiting for real hardware to
misbehave. See [`edge/simulator/telemetry_simulator/faults.py`](../edge/simulator/telemetry_simulator/faults.py).

Building the device first would have meant debugging a schema, a broker ACL and
a stuck I2C bus simultaneously, with no way to tell which was at fault.

### Identity comes from the certificate, never from the payload

This is the security boundary of the whole platform.

A device presents an X.509 client certificate. The broker takes the identity
from that certificate and restricts which topics it may write:

- **Mosquitto** with `use_identity_as_username true` sets the MQTT username to
  the certificate's common name, and the ACL pattern `pattern write
  dt/acme/+/%u/telemetry` confines it to its own name.
- **AWS IoT Core** substitutes `${iot:Connection.Thing.ThingName}` into one
  policy document attached to every certificate, which achieves the same thing
  without a policy per device.

So the `device_id` **in the topic** is authenticated. The `device_id` **in the
payload** is just a claim. Ingest compares the two and dead-letters any
mismatch, which is what stops one compromised device writing telemetry
attributed to its neighbours.

Both halves are tested against the running stack, not asserted in a comment:
`test_a_device_cannot_publish_to_another_devices_topic` and
`test_a_spoofed_payload_on_its_own_topic_is_dead_lettered` in
[`tests/integration/test_end_to_end.py`](../tests/integration/test_end_to_end.py).

There is no plaintext MQTT listener and no username-and-password fallback. A
test asserts port 1883 is closed, because a fallback would make the whole
certificate story decorative.

### Two rollup paths, deliberately

The same measurement is readable from four places, and the reason is a conflict
that cannot be resolved in one table:

| Source | Resolution | Freshness | Retention | Correct under late data |
| --- | --- | --- | --- | --- |
| `telemetry` | per sample | seconds | 30 days | yes |
| `telemetry_1m_live` | 1 minute | seconds | 2 days | no |
| `telemetry_1m` | 1 minute | ~1 minute | 180 days | yes |
| `telemetry_1h` | 1 hour | ~1 hour | 730 days | yes |

The dashboard cannot wait a minute for a continuous aggregate to refresh. A
continuous aggregate cannot be made to refresh continuously without the cost
becoming absurd. So the stream processor keeps a small window of state in memory
and writes a provisional rollup within seconds, and the continuous aggregate
overwrites history with the correct version later.

The cost is honest and stated: `telemetry_1m_live` is approximate at the edges
of a window and loses data that arrives after its window closed. That is
acceptable for a live view and not acceptable for history, which is exactly why
both exist. Late samples are counted, not silently folded in, by
`WindowAggregator.note_late`.

Choosing between them is one function,
[`services/api/api/resolution.py`](../services/api/api/resolution.py), and every
telemetry response reports which source answered and why.

### Long form storage, not a column per sensor

`telemetry` is `(ts, device_id, site_id, metric, value, …)`, one row per metric
per sample.

A wide table with a column per sensor would be smaller for a fixed sensor set.
It would also make every new metric a schema migration across a live fleet, and
leave devices that report a subset carrying columns of nulls. The fleet here
already has three device classes reporting overlapping but different metrics,
and an open `readings` map in the message schema means a new sensor type ships
without touching the platform.

Compression makes the repetition nearly free: `segmentby device_id, metric`
groups every sample of one series into one compressed batch, so the repeated
`site_id` and `fw_version` columns cost almost nothing once a chunk is
compressed.

### TimescaleDB is not in the Terraform

RDS does not offer the TimescaleDB extension. The options are Timescale Cloud, a
managed service in a different account with a different provider, or
self-hosting on the cluster.

This repository takes the second and is explicit about what that means: a
single-replica StatefulSet in [`infra/k8s/base/timescaledb.yaml`](../infra/k8s/base/timescaledb.yaml),
fine for a demonstration and for a development cluster, and not what anyone
should put a production fleet's data into without a replica, a backup schedule
and someone who has practised a failover. For production, Timescale Cloud is the
recommendation, and the only change is a connection string.

### At-least-once, made idempotent

Delivery is at-least-once from the device through to storage. QoS 1 at the
broker, manual offset commits after the write in the processor, so a crash
between writing and committing replays a batch that is already stored.

That is absorbed by a unique index on `(device_id, metric, ts)` and
`ON CONFLICT DO NOTHING`. The failure mode is duplicate work, never lost data.
`test_the_dedupe_index_makes_replay_idempotent` asserts it against the real
table.

Exactly-once would need either a transactional sink or a deduplication store,
both of which cost more than the unique index and buy nothing here.

### Alerting evaluates on a timer, not in the stream

The rule engine queries the live rollups every 15 seconds rather than
maintaining windows in memory as messages pass.

Three reasons. The rollups already exist. A restarted evaluator recovers its
entire view of the world from the database rather than from replayed offsets, so
there is no state to lose. And a rule with a 45 minute window does not need 45
minutes of samples held in memory to answer.

The cost is that alert latency is bounded below by the evaluation interval,
which at 15 seconds is well inside every `for` duration in the catalogue.

Alert state lives in the database too. Deduplication is a partial unique index,
`(rule_id, device_id, metric) WHERE state = 'firing'`, so two replicas racing on
the same breach produce one alert rather than needing a lock.

### An analytical copy, rather than pointing dashboards at TimescaleDB

TimescaleDB answers "what is this device doing now" in milliseconds and
deliberately discards history. BigQuery answers "how has the fleet behaved over
three years" and does not care that it takes eight seconds.

Running the second kind of question against TimescaleDB puts a scan across every
chunk in the way of the queries the live dashboard depends on, which is how a
reporting request takes an operational system down.

The queries are written once against a small macro vocabulary and rendered into
either BigQuery or DuckDB, so CI runs every one of them against generated
fixtures. Testing them against BigQuery would need a project, a billing account
and credentials, which in practice means the queries in the repository would be
the ones nobody had ever run. See [`analytics/dialect.py`](../analytics/dialect.py).

## Local against AWS

| Concern | Local stack | AWS | Shared |
| --- | --- | --- | --- |
| Broker | Mosquitto, mutual TLS | IoT Core | topic grammar, identity binding |
| Identity | ACL `%u` from certificate CN | `${iot:Connection.Thing.ThingName}` | the guarantee |
| Ingest | `services/ingest`, a subscriber | IoT Core topic rule | `ingest/validation.py` |
| Stream | Redpanda | Kinesis + Firehose to S3 | `StreamSink` protocol |
| Processing | same container | same container on EKS | all of it |
| Database | Compose service | StatefulSet on EKS | the migrations |
| Secrets | Compose defaults | Secrets Manager via External Secrets | nothing |

The validation logic is shared between both ingest paths, so what is enforced
does not depend on which one is running. The sink is behind a protocol with a
Kafka and a Kinesis implementation, so the choice is a flag rather than a second
codebase.

What differs and matters: the AWS path has no `services/ingest` process at all,
because the topic rule does that routing in managed infrastructure. The rule SQL
selects `clientid()` into the payload as `_client_id` precisely so the downstream
consumer can perform the same identity check the bridge performs locally. Without
it the authenticated identity would be lost at the first hop and the whole
certificate story would collapse.

### How faithful the local IoT Core stand-in is

Mosquitto is configured to present the same door as IoT Core and the parts
that matter for security are exercised against the running stack. It is not
IoT Core and the differences are listed rather than glossed over.

**The same and tested**

| Property | Where it is tested |
| --- | --- |
| Mutual TLS only: no certificate, no connection; no plaintext port | `TestBrokerSecurity` in `test_end_to_end.py` |
| One X.509 certificate per device, issued by one CA | `pki.sh`, run by the `pki` job on every `up` |
| One authorisation document for the whole fleet, scoped by the authenticated identity (`%u` locally, `${iot:Connection.Thing.ThingName}` on AWS) | ACL test in `test_end_to_end.py`; the AWS policy in `infra/terraform/aws/modules/iot/tests` |
| A device cannot publish as another device | `test_a_device_cannot_publish_to_another_devices_topic` |
| A new device provisioned into a running fleet is accepted and recorded | `test_certificate_lifecycle.py` and the emulated ESP32 in `test_emulated_device.py` |
| Rotation with an overlap window: old and new certificates both accepted | both of the above |
| A revoked certificate is refused at the handshake; its replacement is not | both of the above |
| The firmware uses its device id as its MQTT client id, which the AWS policy requires | `edge/firmware/main/link.c`, booted in QEMU |

**Different and why it is acceptable here**

| Local | IoT Core | Consequence |
| --- | --- | --- |
| Identity is the certificate's common name | Identity is the thing the certificate is attached to; the common name is ignored | Locally the CN is the thing name by convention. On AWS, `attach-thing-principal` is the step that binds them and it is not exercised. |
| The client id is not checked | `iot:Connect` is allowed only as `client/${iot:Connection.Thing.ThingName}` | The firmware and the simulator already connect with their device id. The integration tests use suffixed client ids that IoT Core would refuse. |
| The CRL is read when the broker loads its configuration, so a revocation needs a reload (SIGHUP); the lifecycle test asserts both halves of that | Setting a certificate `INACTIVE` takes effect at once | An operational step locally, documented in [provisioning.md](provisioning.md#revocation). |
| An unauthorised publish is silently dropped | The client is disconnected | The test asserts the outcome, that nothing is stored, which holds for both. |
| The broker certificate is signed by the same local CA | The endpoint presents an Amazon Trust Services certificate | The firmware's `ca_cert` NVS key is the broker's trust anchor; on AWS it would hold the Amazon root, not the device CA. |
| No limits on message rate or size beyond `max_packet_size` | Per-connection and per-account quotas | Not emulated. |
| Certificates issued by `pki.sh` | Fleet provisioning by claim, or just-in-time registration | Described in [provisioning.md](provisioning.md), not emulated. |
| `services/ingest` subscribes and validates | A topic rule routes to Kinesis | The rule's SQL and Kinesis action are checked by `terraform test` with mocked providers, never by AWS. |

LocalStack's free edition does not include IoT Core, so there is no closer
emulator to reach for without a paid licence or a real account.

## What has actually been run

| Component | Status |
| --- | --- |
| Device simulator, 3 profiles, 8 fault modes | run continuously |
| Mosquitto with mutual TLS, ACL, CRL | run, and tested adversarially |
| PKI: issue, rotate with overlap, revoke, CRL | run end to end and asserted in the integration suite |
| Ingest, validation, dead-lettering | run; all 8 rejection reasons observed |
| Redpanda, 4 topics | run |
| Processing, rollups, registry | run |
| TimescaleDB: hypertables, 2 continuous aggregates, compression, 6 retention policies | run |
| Query API, REST and WebSocket | run |
| Alerting, 13 rules | run; true and false positives both tested |
| React dashboard | run |
| Benchmark | run |
| Analytics: export and all 5 queries | run, against DuckDB |
| Terraform, both roots | `fmt` and `validate`; the IoT module also `terraform test` with mocked providers; never applied |
| Kubernetes manifests | `kustomize build` only, never applied |
| ESP32 firmware, ESP-IDF | built for esp32 and esp32c3; the esp32 build booted in Espressif's QEMU in CI and publishing into the stack; never flashed |

The last three are the honest gaps. [`docs/limitations.md`](limitations.md) is
blunter about them.
