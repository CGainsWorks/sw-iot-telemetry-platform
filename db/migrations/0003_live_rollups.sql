-- 0003: the low-latency rollup table written by the stream processor.
--
-- This is the table the dashboard polls and the alerting engine evaluates. It is
-- updated in place as a window fills, so a one-minute bucket is readable from the
-- moment its first sample arrives rather than after the next continuous aggregate
-- refresh. The cost is that a bucket is provisional until its window closes, and
-- that data arriving after the window has been evicted from the processor's
-- memory never lands here at all. Both are acceptable for a live view and not
-- acceptable for history, which is why telemetry_1m exists alongside it.

CREATE TABLE IF NOT EXISTS telemetry_1m_live (
    bucket       TIMESTAMPTZ      NOT NULL,
    device_id    TEXT             NOT NULL,
    site_id      TEXT             NOT NULL,
    metric       TEXT             NOT NULL,
    avg_value    DOUBLE PRECISION NOT NULL,
    min_value    DOUBLE PRECISION NOT NULL,
    max_value    DOUBLE PRECISION NOT NULL,
    last_value   DOUBLE PRECISION NOT NULL,
    sample_count INTEGER          NOT NULL,
    -- Sum of squares, carried so a merge of two partial windows can recover the
    -- standard deviation. Recomputing it from avg and count alone is impossible.
    sum_squares  DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at   TIMESTAMPTZ      NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'telemetry_1m_live',
    'bucket',
    chunk_time_interval => INTERVAL '6 hours',
    if_not_exists => TRUE
);

-- The processor upserts into this table, so it needs a unique constraint to
-- conflict against. Includes the partitioning column as Timescale requires.
CREATE UNIQUE INDEX IF NOT EXISTS telemetry_1m_live_key_idx
    ON telemetry_1m_live (device_id, metric, bucket);

CREATE INDEX IF NOT EXISTS telemetry_1m_live_recent_idx
    ON telemetry_1m_live (bucket DESC);
CREATE INDEX IF NOT EXISTS telemetry_1m_live_site_idx
    ON telemetry_1m_live (site_id, metric, bucket DESC);

-- ---------------------------------------------------------------------------
-- Device health, derived rather than stored.
--
-- A view instead of a column on devices, because "is this device healthy" is a
-- question about the current time and a stored answer starts going stale the
-- moment it is written. The thresholds are expressed as a multiple of the
-- device's own expected interval, so a 2 second machine monitor and a 30 second
-- air quality node are judged on the same terms.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW device_health AS
SELECT
    d.device_id,
    d.site_id,
    d.profile,
    d.fw_version,
    d.schema_version,
    d.last_seen,
    d.last_status,
    d.last_lifecycle_event,
    d.certificate_expires_at,
    EXTRACT(EPOCH FROM (now() - d.last_seen))                AS seconds_since_last_seen,
    CASE
        WHEN d.last_seen IS NULL                             THEN 'never_seen'
        WHEN now() - d.last_seen > INTERVAL '10 minutes'      THEN 'offline'
        WHEN d.last_lifecycle_event = 'offline'               THEN 'offline'
        WHEN d.last_status = 'fault'                          THEN 'fault'
        WHEN d.last_status = 'degraded'                       THEN 'degraded'
        WHEN now() - d.last_seen > INTERVAL '2 minutes'       THEN 'stale'
        ELSE 'healthy'
    END                                                       AS health,
    CASE
        WHEN d.certificate_expires_at IS NULL                 THEN NULL
        ELSE EXTRACT(DAY FROM (d.certificate_expires_at - now()))
    END                                                       AS certificate_days_remaining
FROM devices d;
