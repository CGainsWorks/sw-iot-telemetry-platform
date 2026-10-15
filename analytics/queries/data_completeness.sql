-- Data completeness per device per day.
--
-- The question this answers is "how much of what the fleet should have sent did
-- we actually receive", which is the first thing anyone asks of a telemetry
-- platform and the hardest to answer after the fact. It works by comparing the
-- samples actually stored against the samples the device's profile says it
-- should have produced.
--
-- Note the join to device_dim rather than a hard-coded interval: profiles have
-- different cadences, and treating a 30 second air quality node as though it
-- should report every 10 seconds would report two thirds of its data missing.

WITH expected AS (
  SELECT
    profile,
    CASE profile
      WHEN 'environment' THEN 10.0
      WHEN 'machine'     THEN 2.0
      WHEN 'air_quality' THEN 30.0
      ELSE 10.0
    END AS interval_seconds
  FROM (SELECT DISTINCT profile FROM {{table:device_dim}} WHERE profile IS NOT NULL)
),

daily AS (
  SELECT
    {{TO_DATE(t.bucket)}}    AS day,
    t.device_id,
    t.site_id,
    t.metric,
    SUM(t.sample_count)      AS samples_received,
    COUNT(*)                 AS hours_with_data
  FROM {{table:telemetry_hourly}} t
  WHERE t.bucket >= {{DAYS_AGO(30)}}
  GROUP BY 1, 2, 3, 4
)

SELECT
  d.day,
  d.device_id,
  d.site_id,
  d.metric,
  d.samples_received,
  d.hours_with_data,
  -- 86400 seconds in a day divided by the profile's publish interval.
  {{AS_INT(86400.0 / e.interval_seconds)}} AS samples_expected,
  ROUND(
    100.0 * {{SAFE_DIVIDE(d.samples_received, 86400.0 / e.interval_seconds)}},
    2
  ) AS completeness_pct
FROM daily d
LEFT JOIN {{table:device_dim}} dim
       ON dim.device_id = d.device_id
      AND dim.snapshot_date = d.day
LEFT JOIN expected e
       ON e.profile = dim.profile
-- Anything under 95 per cent is worth a look; under 50 usually means the device
-- spent most of the day offline.
WHERE {{SAFE_DIVIDE(d.samples_received, 86400.0 / e.interval_seconds)}} < 0.95
ORDER BY completeness_pct ASC, d.day DESC
LIMIT 500
