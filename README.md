# sw-iot-telemetry-platform

> **Status: planned. This repository is a brief, not an implementation.**
> Nothing here is built yet. The sections below define what goes in it.

Sensor data from edge devices through to a live dashboard: MQTT publishing with certificate-based device identity, cloud ingestion, stream processing, time-series storage with rollups, alerting and a typed React dashboard. Pairs naturally with the embedded repos, which can supply the real devices.

---

## What this covers

CV entries this repository is the evidence for:

- End-to-End IoT Telemetry Platform
- Large-dataset querying with SQL and BigQuery (Bottle Bond)

---

## Requirements

### Hardware

- Edge sensor nodes: ESP32, Raspberry Pi or similar with sensors
- Simulation substitute: a synthetic device fleet publisher

### Software

- MQTT client firmware
- AWS IoT Core
- Kafka or Kinesis stream processing
- TimescaleDB
- React and TypeScript dashboard
- Alerting
- Terraform
- Kubernetes
- SQL and BigQuery for analytical queries

### Report and documentation deliverables

- Architecture diagram
- Device provisioning and certificate rotation procedure
- Message schema definition
- Throughput and latency benchmarks
- Alerting rule catalogue
- Cost model

---

## Inputs

- Sensor readings
- Device certificates and identities
- MQTT topic schema
- Alert threshold configuration
- Terraform variables

## Outputs

- Ingested time-series records
- Aggregated rollups
- Live dashboard views
- Alert notifications
- Device health status
- Provisioned infrastructure

---

## Intended structure

```
edge/
  firmware/               MQTT client for ESP32 / Pi
  simulator/              synthetic device fleet
services/
  ingest/                 IoT Core rules, stream consumers
  processing/             Kafka/Kinesis stream jobs, rollups
  api/                    query API over TimescaleDB
  alerting/               rule engine, notifications
web/                      React and TypeScript dashboard
analytics/                BigQuery schemas and analytical queries
infra/
  terraform/              IoT Core, MSK/Kinesis, EKS, TimescaleDB
  k8s/
docs/                     architecture.md, provisioning.md,
                          message_schema.md, alert_catalogue.md
```

Plus the standard baseline: `README.md`, `LICENSE`, `.gitignore`, `.gitattributes`
(Git LFS rules for weights, bags and media), `.github/workflows/ci.yml`, `docs/`,
`scripts/setup.sh`, `scripts/run_demo.sh` and `tests/`.

---

## Build checklist

- [ ] Message schema definition and versioning strategy
- [ ] Device simulator publishing over MQTT
- [ ] AWS IoT Core ingestion with certificate-based device identity
- [ ] Device provisioning and certificate rotation procedure
- [ ] Stream processing with rollups
- [ ] TimescaleDB schema with retention and continuous aggregates
- [ ] Query API
- [ ] React dashboard with live updates
- [ ] Alerting rule engine and notifications
- [ ] Real edge device firmware
- [ ] Terraform provisioning of the whole stack
- [ ] Throughput and latency benchmark
- [ ] BigQuery analytical layer
- [ ] Cost model

---

## Definition of done

This repository is finished when:

- [ ] The whole thing runs from one command on a clean machine.
- [ ] The README opens with a diagram or a GIF, not a wall of text.
- [ ] Every number quoted in the README is reproducible from a script in the repo.
- [ ] Limitations are stated honestly in their own section.
- [ ] A licence is declared.
- [ ] Large binaries live in Releases or LFS, not in the Git history.
- [ ] The repository URL is added to the matching CV bullet point.

---

## Starter prompt

Paste this into your coding agent of choice to begin.

<details>
<summary>Expand</summary>

```
You are helping me build a portfolio project from scratch. Read this brief and then
propose a concrete implementation plan before writing any code.

PROJECT: sw-iot-telemetry-platform
Sensor data from edge devices through to a live dashboard: MQTT publishing with certificate-based device identity, cloud ingestion, stream processing, time-series storage with rollups, alerting and a typed React dashboard. Pairs naturally with the embedded repos, which can supply the real devices.

TARGET STACK
- MQTT client firmware
- AWS IoT Core
- Kafka or Kinesis stream processing
- TimescaleDB
- React and TypeScript dashboard
- Alerting
- Terraform
- Kubernetes
- SQL and BigQuery for analytical queries

HARDWARE CONTEXT
- Edge sensor nodes: ESP32, Raspberry Pi or similar with sensors
- Simulation substitute: a synthetic device fleet publisher

THE SYSTEM MUST TAKE THESE INPUTS
- Sensor readings
- Device certificates and identities
- MQTT topic schema
- Alert threshold configuration
- Terraform variables

AND PRODUCE THESE OUTPUTS
- Ingested time-series records
- Aggregated rollups
- Live dashboard views
- Alert notifications
- Device health status
- Provisioned infrastructure

INTENDED REPOSITORY LAYOUT
edge/
  firmware/               MQTT client for ESP32 / Pi
  simulator/              synthetic device fleet
services/
  ingest/                 IoT Core rules, stream consumers
  processing/             Kafka/Kinesis stream jobs, rollups
  api/                    query API over TimescaleDB
  alerting/               rule engine, notifications
web/                      React and TypeScript dashboard
analytics/                BigQuery schemas and analytical queries
infra/
  terraform/              IoT Core, MSK/Kinesis, EKS, TimescaleDB
  k8s/
docs/                     architecture.md, provisioning.md,
                          message_schema.md, alert_catalogue.md

CONSTRAINTS
- Everything must run from a single command: `docker compose up` or `make demo`.
- No placeholder or stub code presented as working. If something is not implemented, say so.
- Every quantitative claim in the README must be reproducible from a script in this repo.
- Use only public or synthetic data. Record the licence of every dataset used.


START BY
1. Confirming or challenging the proposed repository layout.
2. Listing the specific libraries and versions you intend to pin.
3. Identifying the single riskiest technical unknown and how to de-risk it first.
Then work through the milestones in the README build checklist, one at a time.

```

</details>

---

## Notes

- Do not backdate commits to match CV dates. Publishing older work today is normal and expected.
- Record dataset licences before training on anything. Several common research datasets are
  non-commercial only.
- Flip this repository to public once there is something in it worth showing.
