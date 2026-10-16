# Limitations

What this repository has not done, stated plainly. The short version: the data
platform runs and is tested; the firmware runs under emulation but has never
been flashed; the cloud infrastructure is written and validated but has never
been deployed.

## No hardware

**There is no ESP32 behind this repository.** There is no BME280, no battery, no
enclosure and no roof.

The firmware in [`edge/firmware`](../edge/firmware) is built with ESP-IDF for
`esp32` and `esp32c3` on every push. The `esp32` build is also booted in
Espressif's QEMU in CI, where it is provisioned from the stack's PKI, connects
over mutual TLS and publishes into the platform; the integration tests assert
its readings in TimescaleDB, a command reaching it, a certificate rotation and a
revocation. It has never been flashed to a board.

Emulation settles some questions the previous Arduino version left open. The
handshake order (address, then SNTP, then TLS) works; the mutual-TLS handshake
with a 2048-bit RSA client certificate completes within the ESP32's default heap
as QEMU models it; the JSON is accepted by ingest without a single dead letter.

What QEMU does not model and so stays **unverified**:

- Wi-Fi. The emulated build uses the OpenCores Ethernet MAC that QEMU provides;
  the Wi-Fi station code is compiled for both targets and never run.
- The BME280. QEMU has no sensor on its I2C bus, so the emulated build's
  temperature, humidity and pressure are **simulated** and labelled as such in
  the device's boot message. The I2C driver is compiled and never run; its
  compensation arithmetic is host-tested against the Bosch datasheet formulas
  and worked example, which is not the same as reading a part.
- The ADC. The battery reading uses ESP-IDF's calibration schemes rather than a
  raw conversion, which addresses the ESP32 ADC's non-linearity on paper but
  no voltage has ever been read.
- Timing and memory headroom on silicon. Emulation runs at its own pace, which
  is why the QEMU watchdog is disabled; real heap fragmentation after weeks of
  reconnects is not something a short emulated run reveals.
- Power consumption. The deep sleep path is written and the trade-off is
  reasoned about but no figure in this repository comes from a multimeter. The
  claim that the handshake costs more than staying awake below a 30 second
  interval is arithmetic, not measurement.
- Sensor self-heating. A BME280 next to a Wi-Fi radio in a sealed enclosure
  reads high, by an amount that depends on the enclosure. Not modelled.
- Field reliability: antenna performance, flash wear, what happens at -20 degC,
  whether the I2C bus survives a long cable run.
- Key protection. The device key sits in an unencrypted NVS partition. A
  production build would turn on flash and NVS encryption or use the digital
  signature peripheral; neither is configured or tested.

What the simulator **does** verify is that the platform handles the messages a
device of this shape produces, including eight deliberately injected fault modes.
That is a genuinely useful thing to have verified, and it is not the same as
having run a device.

## No AWS account

**Nothing in [`infra/terraform/`](../infra/terraform/) has ever been applied.**

Both roots pass `terraform fmt -check` and `terraform validate` in CI, which
means the HCL is well formed and type-correct against the real provider schemas.
That is a meaningful check and it is a long way short of a successful apply.

Specifically untested:

- Whether a `plan` against a real account succeeds. Validation does not resolve
  data sources, so `aws_availability_zones`, `aws_caller_identity` and
  `aws_iot_endpoint` are unexercised.
- Whether the IoT policy variables behave as intended. The
  `${iot:Connection.Thing.ThingName}` substitution is the security mechanism of
  the whole AWS path, and the only evidence it works is the AWS documentation.
  Its local equivalent, Mosquitto's `%u` pattern, **is** tested adversarially.
- Whether the IoT topic rule's error action is syntactically accepted. The
  dead-letter stream name is extracted from an ARN with `split`, which is the
  kind of thing a real apply rejects.
- EKS: the add-on versions, whether the OIDC thumbprint approach still works,
  whether the ARM64 node group schedules.
- Every cost figure in [`cost_model.md`](cost_model.md) comes from published
  price lists, not an invoice.

The same applies to [`infra/k8s/`](../infra/k8s/): the manifests render with
`kustomize build` and have never been applied to a cluster.

## The database is not production-shaped

TimescaleDB runs as a **single-replica StatefulSet** with no replica, no backup
schedule and no tested failover. That is fine for a demonstration and for a
development cluster, and it is not what anyone should put a production fleet's
data into.

For production the recommendation is Timescale Cloud, which is a connection
string change. Self-hosting properly would need streaming replication, a
`pgBackRest` or `wal-g` schedule to object storage, and a failover procedure
somebody has actually practised. None of that is here.

## Scale is unproven beyond what was measured

The benchmark numbers in the README come from real runs on one developer
desktop against Docker Desktop, condensed into `bench/summary.json`. They are reproducible with one command and they say
nothing about:

- A fleet of thousands rather than tens. The simulator opens one TLS connection
  per device, which is realistic and means a laptop runs out of file handles and
  memory long before a broker would.
- Redpanda or Kafka with more than one node. Replication factor is 1 throughout
  the local stack, so nothing here exercises partition leadership, ISR
  behaviour or what happens when a broker dies.
- Kinesis shard limits, resharding, or the hot-shard problem that a skewed
  partition key causes.
- TimescaleDB under sustained write load with compression and continuous
  aggregate refreshes competing for the same I/O. The benchmark runs for a
  minute; the interesting failures happen after a week.

## Security work that has not been done

The device identity path is genuinely tested, adversarially, against the running
stack. These other things are not:

- **The API has no authentication.** It is read-only and bound to localhost in
  the local stack, and the Kubernetes manifests put it behind an ingress with
  TLS, but there is no authentication or authorisation on any endpoint. A real
  deployment needs at minimum an API key or OIDC, and per-tenant authorisation
  if the tenant segment in the topic is to mean anything.
- **Multi-tenancy is structural, not enforced.** The topic grammar has a tenant
  segment and the broker ACL is generated per tenant, but the database has no
  row-level security and the API does not filter by tenant. Two tenants on one
  deployment would see each other's data.
- No secrets management in the local stack. The Compose defaults are a password
  of `telemetry` on a loopback port, which is appropriate for a local stack and
  nothing else.
- No rate limiting anywhere. A device in a reboot loop can publish as fast as it
  can connect.
- No audit log. Who queried what is not recorded.
- The CRL refresh problem is documented in
  [`provisioning.md`](provisioning.md#revocation) and not solved: Mosquitto
  reads the CRL at startup, so revocation needs a broker reload.
- No penetration testing, no dependency vulnerability scanning in CI, no SBOM.

## Things that are approximate by design

These are not gaps so much as documented trade-offs, listed here so they are not
mistaken for bugs.

- **`telemetry_1m_live` is approximate.** It is written as windows fill, so a
  bucket is provisional until its window closes, and a sample arriving after
  that is counted and dropped from the live path rather than folded in. The
  continuous aggregate is the correct version. See
  [architecture.md](architecture.md#two-rollup-paths-deliberately).
- **Alert latency is bounded below by the evaluation interval**, 15 seconds. Well
  inside every `for` duration in the catalogue, but it means the platform cannot
  alert on a sub-second event.
- **End-to-end latency is measured by polling**, at 20 ms resolution. Precise
  enough against latencies in the hundreds of milliseconds. The alternative, a
  `stored_at` column, would cost eight bytes on every row of the largest table
  forever to answer a question only asked during a benchmark.
- **The analytical queries are tested against DuckDB, not BigQuery.** They are
  written once against a small macro vocabulary and rendered into either
  dialect. DuckDB is close enough on the constructs used that a query working
  against one works against the other, but it is not BigQuery and the
  `APPROX_QUANTILES` rendering in particular is an approximation there and exact
  here.
- The `device_health` view's thresholds are fixed at 2 and 10 minutes rather
  than scaled per device profile. A 30 second air quality node and a 2 second
  machine monitor are judged by the same clock, which makes the former look
  stale sooner than it should.

## What is genuinely verified

For balance, because the above is a long list. All of the following have been run
and are asserted by automated tests:

- The whole stack from `docker compose up` on a clean machine, including PKI
  generation, schema migration and topic creation.
- 232 unit tests covering the message contract, the versioning policy, the topic
  grammar, every ingest rejection reason, the rollup arithmetic, source
  selection, rule validation and the simulator's own determinism.
- 37 integration tests against the running stack, including: an anonymous client
  cannot connect, there is no plaintext port, a device cannot publish to another
  device's topic, a spoofed payload is dead-lettered, replay is idempotent, the
  rollup means match the raw data, the API's retention table matches the
  database's policies, a sustained breach fires exactly one alert, and the
  vibration rule fires on a real fault while staying quiet on duty cycling.
- A full certificate lifecycle against the live broker: provision a new device
  into the running fleet, rotate with an overlap window, revoke, regenerate the
  CRL, reload the broker. Asserted in `test_certificate_lifecycle.py` and again
  with the emulated firmware.
- All five analytical queries, against fixtures shaped so each returns rows.
- The dashboard, rendering live data from the WebSocket feed.
- The firmware, built with ESP-IDF for esp32 and esp32c3 and the esp32 build
  booted in Espressif's QEMU, publishing over mutual TLS into the stack.
