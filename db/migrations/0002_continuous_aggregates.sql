-- migrate:no-transaction
-- 0002: rollups.
--
-- CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous) cannot run inside a
-- transaction block, hence the no-transaction marker read by scripts/migrate.py.
--
-- There are two rollup paths in this platform and they are not redundant:
--
--   telemetry_1m_live   written by the stream processor, seconds behind the
--                       device, approximate at the edges of a window, dropped
--                       after two days. The dashboard and the alerting engine
--                       read this one.
--
--   telemetry_1m / _1h  continuous aggregates, minutes behind, correct even when
--                       a device delivers a backlog hours late, kept for months.
--                       The query API and the analytics export read these.
--
-- Trying to serve both needs from one table is where this design usually goes
-- wrong: make it fast and late data corrupts it, make it correct and the live
-- view is always a refresh interval behind.

-- ---------------------------------------------------------------------------
-- One-minute rollup. materialized_only = true because a hierarchical continuous
-- aggregate cannot be built on top of one with real-time aggregation enabled,
-- and telemetry_1h below is built on this. The gap at the leading edge is what
-- telemetry_1m_live covers.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1m
    WITH (timescaledb.continuous, timescaledb.materialized_only = true) AS
SELECT
    time_bucket(INTERVAL '1 minute', ts) AS bucket,
    device_id,
    site_id,
    metric,
    avg(value)                AS avg_value,
    min(value)                AS min_value,
    max(value)                AS max_value,
    count(*)                  AS sample_count,
    stddev_samp(value)        AS stddev_value,
    last(value, ts)           AS last_value,
    max(ingest_ts)            AS last_ingest_ts
FROM telemetry
GROUP BY bucket, device_id, site_id, metric
WITH NO DATA;

-- ---------------------------------------------------------------------------
-- One-hour rollup, built on the one-minute rollup rather than on raw telemetry.
-- Rolling up the rollup is far cheaper, but the mean has to be re-weighted by
-- sample_count: averaging a set of averages is only correct when every bucket
-- holds the same number of samples, and buckets here do not, because devices
-- publish at different cadences and drop out.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_1h
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT
    time_bucket(INTERVAL '1 hour', bucket) AS bucket,
    device_id,
    site_id,
    metric,
    sum(avg_value * sample_count) / NULLIF(sum(sample_count), 0) AS avg_value,
    min(min_value)   AS min_value,
    max(max_value)   AS max_value,
    sum(sample_count) AS sample_count
FROM telemetry_1m
GROUP BY 1, 2, 3, 4
WITH NO DATA;

-- ---------------------------------------------------------------------------
-- Refresh policies.
--
-- end_offset keeps the refresh away from the newest data. Without it a bucket
-- would be materialised while samples for it are still arriving, and the stored
-- value would be wrong until the next invalidation caught up.
-- ---------------------------------------------------------------------------
SELECT add_continuous_aggregate_policy(
    'telemetry_1m',
    start_offset      => INTERVAL '2 hours',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE
);

SELECT add_continuous_aggregate_policy(
    'telemetry_1h',
    start_offset      => INTERVAL '3 days',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists     => TRUE
);

CREATE INDEX IF NOT EXISTS telemetry_1m_device_metric_idx
    ON telemetry_1m (device_id, metric, bucket DESC);
CREATE INDEX IF NOT EXISTS telemetry_1m_site_metric_idx
    ON telemetry_1m (site_id, metric, bucket DESC);
