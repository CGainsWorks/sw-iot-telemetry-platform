-- 0001_init: extensions, device registry and the raw telemetry hypertable.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------------
-- Device registry.
--
-- The row of record for a device, updated by the processing service as messages
-- arrive and by the provisioning tooling when a certificate is issued. Small and
-- mutable, so an ordinary table rather than a hypertable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    device_id              TEXT PRIMARY KEY,
    site_id                TEXT        NOT NULL,
    profile                TEXT,
    fw_version             TEXT,
    schema_version         TEXT,
    first_seen             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen              TIMESTAMPTZ,
    last_status            TEXT,
    last_lifecycle_event   TEXT,
    last_lifecycle_at      TIMESTAMPTZ,
    -- Certificate common name. Equal to device_id by construction, kept
    -- explicitly so a mismatch introduced by a bad provisioning run is visible.
    certificate_cn         TEXT,
    certificate_expires_at TIMESTAMPTZ,
    provisioned_at         TIMESTAMPTZ,
    metadata               JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS devices_site_idx ON devices (site_id);
CREATE INDEX IF NOT EXISTS devices_last_seen_idx ON devices (last_seen DESC NULLS LAST);

-- ---------------------------------------------------------------------------
-- Raw telemetry.
--
-- Long form: one row per metric per sample rather than one row per message with
-- a column per sensor. Wide would be narrower on disk for a fixed sensor set,
-- but every new metric would then be a schema migration across a live fleet, and
-- devices that report only a subset would carry columns of nulls. Long form also
-- happens to be what time_bucket and the continuous aggregates want.
--
-- site_id, fw_version, schema_version and device_status are denormalised onto
-- every row. That is deliberate: they are the columns queries filter and group
-- by, and Timescale's columnar compression segments by device and metric, so
-- the repetition costs very little once a chunk is compressed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry (
    ts             TIMESTAMPTZ      NOT NULL,
    device_id      TEXT             NOT NULL,
    site_id        TEXT             NOT NULL,
    metric         TEXT             NOT NULL,
    value          DOUBLE PRECISION NOT NULL,
    seq            BIGINT           NOT NULL,
    fw_version     TEXT             NOT NULL,
    schema_version TEXT             NOT NULL,
    device_status  TEXT             NOT NULL,
    -- When the platform first saw the message. ingest_ts minus ts is the
    -- end-to-end lag the benchmark reports, and it is stored rather than
    -- computed so a late replay is still attributable.
    ingest_ts      TIMESTAMPTZ      NOT NULL
);

SELECT create_hypertable(
    'telemetry',
    'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Delivery from the broker through the stream is at least once, so a redelivery
-- after a consumer restart will replay rows that are already stored. This unique
-- index turns the writer's ON CONFLICT DO NOTHING into real idempotency.
-- A unique index on a hypertable must include the partitioning column, hence ts.
CREATE UNIQUE INDEX IF NOT EXISTS telemetry_dedupe_idx
    ON telemetry (device_id, metric, ts);

CREATE INDEX IF NOT EXISTS telemetry_device_ts_idx ON telemetry (device_id, ts DESC);
CREATE INDEX IF NOT EXISTS telemetry_site_metric_ts_idx ON telemetry (site_id, metric, ts DESC);
CREATE INDEX IF NOT EXISTS telemetry_metric_ts_idx ON telemetry (metric, ts DESC);

-- ---------------------------------------------------------------------------
-- Lifecycle events: boot, online, offline, OTA.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS device_events (
    ts         TIMESTAMPTZ NOT NULL,
    device_id  TEXT        NOT NULL,
    site_id    TEXT        NOT NULL,
    event      TEXT        NOT NULL,
    fw_version TEXT,
    reason     TEXT
);

SELECT create_hypertable(
    'device_events',
    'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS device_events_device_ts_idx ON device_events (device_id, ts DESC);

-- ---------------------------------------------------------------------------
-- Dead letters: everything ingest refused, with the reason.
--
-- Stored rather than only logged, because "how many messages are we losing and
-- why" is a question that needs a SQL answer, not a log search.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dead_letters (
    received_at TIMESTAMPTZ NOT NULL,
    reason      TEXT        NOT NULL,
    detail      TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    raw         TEXT        NOT NULL
);

SELECT create_hypertable(
    'dead_letters',
    'received_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS dead_letters_reason_idx ON dead_letters (reason, received_at DESC);
