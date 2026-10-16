# Status

Last updated: 2026-10-16.

What is true of this repository today, in one page. The detail is in
[README.md](README.md), [docs/architecture.md](docs/architecture.md) and
[docs/limitations.md](docs/limitations.md).

## Complete

- **Platform, end to end in Docker Compose.** Simulated fleet (3 profiles, 8
  injectable fault modes), Mosquitto with mutual TLS, per-device certificates,
  a `%u` ACL and a CRL, ingest with 8 dead-letter reasons, Redpanda, stream
  processing, TimescaleDB with continuous aggregates and a retention ladder, a
  FastAPI query and WebSocket API, a 13-rule alerting engine and a React
  dashboard.
- **ESP32 firmware on ESP-IDF v5.5.4**, ported from the Arduino version. Built
  in CI for `esp32` and `esp32c3`. The `esp32` build, with OpenCores Ethernet
  and simulated environmental readings, is booted in Espressif's QEMU
  (`qemu-system-xtensa`, from the `espressif/idf:v5.5.4` image) in the
  integration job, provisioned by NVS image from the stack's PKI and publishes
  over mutual TLS into the platform.
- **Certificate lifecycle tested against the live broker:** provisioning a new
  device into the running fleet, rotation with an overlap window, revocation
  with a CRL and a broker reload. Exercised by a Python client and by the
  emulated firmware.
- **AWS IoT Core module** checked by `terraform test` with mocked providers:
  the device policy confines each thing to its own client id and topics, the
  topic rule carries the authenticated client id, the dead-letter stream is
  named correctly and no CA is created by default. Both Terraform roots pass
  `fmt` and `validate`.
- **Benchmark evidence committed.** [`bench/summary.json`](bench/summary.json)
  holds the three runs the README quotes, condensed from the result files by
  `bench/summarise.py`; a unit test fails if the README and the summary
  disagree.
- **Tests:** 232 Python unit tests, 37 integration tests against the running
  stack (6 of them drive the emulated ESP32 and 3 the certificate lifecycle;
  one rollup comparison skips itself on a stack too young to have a settled
  bucket, as in CI),
  the firmware core's host tests under AddressSanitizer and UBSan, the
  dashboard's Vitest suite, the analytical SQL on DuckDB and the Terraform
  module tests.

## In progress

Nothing.

## Not started

- Running the firmware on a real ESP32 and BME280.
- `terraform plan` or `apply` against a real AWS or GCP account.
- Applying the Kubernetes manifests to a cluster.
- API authentication and per-tenant authorisation.

## Limitations

- **No hardware.** The firmware has never been flashed. Under QEMU the Wi-Fi
  radio, the BME280, the ADC, real timing and power are not emulated; the
  emulated build's temperature, humidity and pressure are simulated and the
  device says so in its boot message.
- **IoT Core is emulated, not used.** Mosquitto mirrors its certificate
  identity model; the differences (identity from the CN rather than a thing
  attachment, no client id check, revocation on reload rather than at once,
  no quotas) are listed in
  [docs/architecture.md](docs/architecture.md#how-faithful-the-local-iot-core-stand-in-is).
  LocalStack's free edition has no IoT Core, so the Terraform is tested with
  mocked providers only.
- **Benchmark figures come from one desktop** (Ryzen 7 5700X, Docker Desktop on
  Windows 11), with the load generator on the same machine. They are a floor,
  not a capacity plan.
- The device key sits in an unencrypted NVS partition; flash and NVS encryption
  are not configured.
- The rest, including the security work not done, is in
  [docs/limitations.md](docs/limitations.md).
