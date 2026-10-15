-- migrate:no-transaction
-- 0004: compression and retention.
--
-- Storage policy, stated once, in the database, rather than as a cron job
-- somewhere else that nobody remembers to check.
--
--   telemetry            compressed after 7 days, dropped after 30
--   telemetry_1m         kept 180 days
--   telemetry_1h         kept 730 days
--   telemetry_1m_live    dropped after 2 days, it is a cache
--   device_events        kept 90 days
--   dead_letters         dropped after 7 days
--
-- The shape of that ladder is the whole argument for a time-series store: full
-- resolution is only useful while someone might still investigate an incident,
-- and after that the minute and hour rollups answer every question anyone
-- actually asks, at a fraction of the size.

-- ---------------------------------------------------------------------------
-- Columnar compression.
--
-- segmentby device_id, metric groups every sample from one series into one
-- compressed batch, which is what makes the repeated site_id, fw_version and
-- schema_version columns nearly free. orderby ts DESC keeps recent-first scans
-- cheap and gives delta encoding a monotonic column to work with.
--
-- Compression waits 7 days rather than 1. Inserts into a compressed chunk work,
-- but ON CONFLICT against one does not, and the writer relies on ON CONFLICT for
-- idempotent replay. Seven days is far longer than any realistic redelivery or
-- device backlog, so the conflict path never meets a compressed chunk.
-- ---------------------------------------------------------------------------
ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, metric',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('telemetry', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE device_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('device_events', INTERVAL '14 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Retention.
--
-- telemetry is dropped at 30 days while telemetry_1m keeps 180. The continuous
-- aggregate refresh window on telemetry_1m is 2 hours, far inside 30 days, so a
-- bucket is always materialised long before the raw rows behind it are dropped.
-- Getting that ordering wrong is how retention policies silently create holes in
-- the rollups.
-- ---------------------------------------------------------------------------
SELECT add_retention_policy('telemetry',         INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_1m',      INTERVAL '180 days', if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_1h',      INTERVAL '730 days', if_not_exists => TRUE);
SELECT add_retention_policy('telemetry_1m_live', INTERVAL '2 days',  if_not_exists => TRUE);
SELECT add_retention_policy('device_events',     INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_retention_policy('dead_letters',      INTERVAL '7 days',  if_not_exists => TRUE);
