# Message schema

The contract between the fleet and the platform. This is the document that
matters most, because everything else can be fixed by redeploying a service and
a mistake here is baked into firmware that is already in the field.

The machine-readable version is generated from the Pydantic models by
[`scripts/gen_schemas.py`](../scripts/gen_schemas.py) and lives in
[`services/common/telemetry_common/schemas/`](../services/common/telemetry_common/schemas/).
CI fails if the two disagree, so the published schema cannot drift from the code
that enforces it.

## Topics

```
dt/{tenant}/{site_id}/{device_id}/telemetry     device → platform, readings
dt/{tenant}/{site_id}/{device_id}/status        device → platform, lifecycle
cmd/{tenant}/{site_id}/{device_id}/req          platform → device, request
cmd/{tenant}/{site_id}/{device_id}/res          device → platform, response
```

Every segment matches `^[a-z0-9][a-z0-9-]{2,63}$`: lower case, no underscores,
no dots, three characters minimum. Lower case because MQTT topics are
case-sensitive and a fleet with both `Site-A` and `site-a` is a fleet with two
sites. No dots because they are the separator in several of the metric and
metrics-exporter conventions downstream.

**The topic is the authorisation boundary, not decoration.** The `device_id`
segment is the identity the broker authenticated from the TLS client
certificate. See [architecture.md](architecture.md#identity-comes-from-the-certificate-never-from-the-payload).

`site_id` is a wildcard in the device policy and `device_id` never is: a device
can be moved between sites without reissuing its certificate, but it can never
publish under another device's name.

## Telemetry

```json
{
  "schema_version": "1.1.0",
  "device_id": "esp32-000001",
  "site_id": "site-a",
  "seq": 48213,
  "ts": "2026-08-22T10:14:03.512Z",
  "fw_version": "1.4.2",
  "uptime_s": 482130,
  "status": "ok",
  "readings": {
    "temperature_c": 21.42,
    "humidity_pct": 48.17,
    "pressure_hpa": 1013.22,
    "battery_v": 3.921,
    "rssi_dbm": -64.5
  }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | semver | Stamped by the device from what it was built against. Never guessed. |
| `device_id` | string | Must equal the certificate common name and the topic segment. |
| `site_id` | string | Must equal the topic segment. |
| `seq` | integer ≥ 0 | Monotonic, resets to 0 on reboot. A gap is a lost message; a reset is a reboot. |
| `ts` | RFC 3339 | Sample time **on the device**, UTC. A naive timestamp is read as UTC rather than rejected, because hardware without a timezone database is normal. |
| `fw_version` | semver | Independent of `schema_version`: firmware can change behaviour without touching the contract. |
| `uptime_s` | integer ≥ 0 | Seconds since boot. With `seq`, distinguishes a reboot loop from a flaky link. |
| `status` | `ok` / `degraded` / `fault` | Self-reported, decided on the device. |
| `readings` | map of string to finite number | At least one entry. |

### Why `readings` is an open map

A typed object with a field per sensor would be stricter and would make every
new sensor type a major version bump requiring a coordinated fleet-wide deploy.
An open map means a new sensor ships by adding one entry to the metric
catalogue, and devices that report a subset simply report a subset.

What that gives up is compile-time knowledge of the field names. The metric
catalogue is what replaces it:
[`services/common/telemetry_common/metrics.py`](../services/common/telemetry_common/metrics.py)
holds the unit and the physically plausible range of every metric in one place,
so ingest can refuse nonsense without every service carrying its own copy of the
rules.

An **unknown** metric is accepted and not range-checked. Rejecting it would make
the platform impossible to extend without a coordinated deploy, which is exactly
the coupling the open map avoids.

### Metric catalogue

| Metric | Unit | Plausible range | Notes |
| --- | --- | --- | --- |
| `temperature_c` | degC | -40 to 125 | BME280 operating range |
| `humidity_pct` | % | 0 to 100 | relative |
| `pressure_hpa` | hPa | 300 to 1100 | sea level to the stratosphere |
| `battery_v` | V | 0 to 5 | |
| `rssi_dbm` | dBm | -120 to 0 | |
| `vibration_rms_g` | g | 0 to 16 | broadband RMS |
| `co2_ppm` | ppm | 0 to 40000 | |

Plausibility is checked **after** parsing, not during it. A stuck sensor
reporting 900 degC is a real message from a real device: it belongs in the
dead-letter table with a reason attached, not thrown away by a parser.

## Status

```json
{
  "schema_version": "1.1.0",
  "device_id": "esp32-000001",
  "site_id": "site-a",
  "ts": "2026-08-22T10:14:03.512Z",
  "event": "online",
  "fw_version": "1.4.2",
  "reason": null
}
```

`event` is one of `boot`, `online`, `offline`, `ota_started`, `ota_completed`,
`ota_failed`.

The `offline` variant is registered as the MQTT **last will and testament** at
connect time, published retained. The broker sends it if the connection drops
without a DISCONNECT, which is the only way to distinguish a crashed device from
a quiet one without waiting out a timeout. A graceful shutdown publishes its own
`offline` first, so a planned restart does not raise an alert.

## Versioning

Versions are `MAJOR.MINOR.PATCH`, carried in every payload.

| Change | Bump | Consumer obligation |
| --- | --- | --- |
| Documentation, relaxed constraint | patch | accept |
| New optional field, new metric name | minor | accept, ignore what you do not recognise |
| Removed field, renamed field, changed type, **changed unit** | major | refuse |

A consumer accepts any minor of a major it supports, older or newer. Newer
minors are safe because they are additive by definition; older minors are safe
because every field a newer consumer relies on either existed at `x.0` or is
optional.

This is enforced in code, not by convention:
[`telemetry_common/versioning.py`](../services/common/telemetry_common/versioning.py)
declares `SUPPORTED_MAJORS`, the wire models use `extra="ignore"` so unknown
fields are dropped rather than rejected, and ingest checks the version **before**
validation. A payload from an unsupported major may look structurally fine and
mean something entirely different, so it must never reach the model.

### Why a fleet carries several versions at once

Because it always does. Devices sit behind flaky links, some sleep for days and
a few never take the update at all. The simulator models this deliberately: the
`air_quality` profile ships firmware `1.3.0` while the others are on `1.4.2`, so
the platform is permanently proving it accepts an older minor rather than
dropping it.

Messages accepted from a **newer** minor than the platform increment
`ingest_schema_ahead_total`. Firmware running ahead of the platform means fields
are being dropped on the floor, and that should be a visible metric rather than
a silent loss.

### Rolling out a major

The only safe sequence, because there is no instant at which a fleet is on one
version:

1. Add the new major to `SUPPORTED_MAJORS` and deploy the platform. It now
   accepts both.
2. Roll the firmware out gradually. Watch the share per version with
   [`analytics/queries/firmware_rollout.sql`](../analytics/queries/firmware_rollout.sql);
   a stalled rollout shows as a curve flattening short of 100 per cent.
3. When the old version's share reaches zero and stays there, remove it from
   `SUPPORTED_MAJORS`.

Doing it the other way round means every device that has not yet updated is
dead-lettered. That is visible, because the `dead-letter-spike` rule fires on
exactly this, but it is still data lost.

## What ingest rejects, and why

In evaluation order. Each reason is a column in the dashboard's rejection panel
and a value of `ingest_rejected_total`.

| Reason | Cause |
| --- | --- |
| `malformed_topic` | Topic does not match the grammar, or is a `cmd` topic ingest does not consume. Checked first: until the topic parses there is no identity to attribute anything to. |
| `malformed_json` | Not decodable, or not a JSON object. |
| `schema_unsupported` | Major not in `SUPPORTED_MAJORS`, or the version string is malformed. Checked before validation. |
| `schema_invalid` | Fails the model. The detail names the offending field. |
| `identity_mismatch` | Payload `device_id` disagrees with the authenticated topic segment. **The one that matters.** |
| `site_mismatch` | Payload `site_id` disagrees with the topic segment. |
| `timestamp_out_of_range` | More than 5 minutes ahead of the platform clock, or more than 7 days behind. Asymmetric on purpose: a device that buffered through an outage is doing the right thing, whereas a timestamp in the future lands where no query will ever look again. |
| `implausible_reading` | Outside the catalogue range. |

Every rejection is stored in `dead_letters` with the reason, the topic and the
original payload truncated to 8 KiB, because "how many messages are we losing
and why" needs a SQL answer rather than a log search.

Each reason has a unit test in
[`tests/unit/test_ingest_validation.py`](../tests/unit/test_ingest_validation.py),
and the four most important are also asserted against the running stack through
a real mutual-TLS publish.

## Delivery guarantees

| Hop | Guarantee |
| --- | --- |
| Device to broker | at-least-once, QoS 1 |
| Broker to ingest | at-least-once, QoS 1 |
| Ingest to stream | at-least-once, `acks=all`, idempotent producer |
| Stream to database | at-least-once, offsets committed after the write |
| Database | effectively-once, via a unique index on `(device_id, metric, ts)` |

So a message can be delivered more than once and never fewer. The duplicate is
absorbed by the index. The failure mode is duplicate work, not lost data.

Ordering is per device, not global: the stream is partitioned by `device_id`, so
one device's readings stay on one partition and therefore stay in order. That is
what lets the processor treat a sequence gap as a real gap rather than as
reordering.
