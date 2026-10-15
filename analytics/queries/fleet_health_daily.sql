-- Fleet health, one row per site per day.
--
-- The headline numbers someone would put on a weekly report: how many devices
-- reported, how complete the data was, and how the fleet's firmware and schema
-- versions are distributed. The last one matters more than it looks: a fleet
-- always carries several firmware versions at once, and a rollout that has
-- stalled shows up here long before anyone notices a behavioural difference.

WITH daily_device AS (
  SELECT
    {{TO_DATE(bucket)}} AS day,
    site_id,
    device_id,
    SUM(sample_count)          AS samples,
    COUNT(DISTINCT metric)     AS metrics_reported,
    COUNT(DISTINCT {{TRUNC_HOUR(bucket)}}) AS active_hours
  FROM {{table:telemetry_hourly}}
  WHERE bucket >= {{DAYS_AGO(30)}}
  GROUP BY 1, 2, 3
),

versions AS (
  SELECT
    snapshot_date AS day,
    site_id,
    COUNT(DISTINCT fw_version)     AS firmware_versions,
    COUNT(DISTINCT schema_version) AS schema_versions,
    -- Devices whose certificate expires inside the next 30 days. Rotation needs
    -- the device to be awake and reachable, so this is a lead indicator, not an
    -- alarm.
    SUM(CASE
          WHEN certificate_expires_at IS NOT NULL
           AND {{SECONDS_UNTIL(certificate_expires_at)}} < 30 * 86400
          THEN 1 ELSE 0
        END) AS certificates_expiring_soon
  FROM {{table:device_dim}}
  WHERE snapshot_date >= {{DATE_DAYS_AGO(30)}}
  GROUP BY 1, 2
)

SELECT
  d.day,
  d.site_id,
  COUNT(DISTINCT d.device_id)                          AS devices_reporting,
  SUM(d.samples)                                       AS samples_total,
  ROUND(AVG(d.active_hours), 2)                        AS mean_active_hours,
  -- A device active for fewer than 20 of 24 hours spent real time offline.
  SUM(CASE WHEN d.active_hours < 20 THEN 1 ELSE 0 END) AS devices_with_gaps,
  MAX(v.firmware_versions)                             AS firmware_versions,
  MAX(v.schema_versions)                               AS schema_versions,
  MAX(v.certificates_expiring_soon)                    AS certificates_expiring_soon
FROM daily_device d
LEFT JOIN versions v
       ON v.day = d.day AND v.site_id = d.site_id
GROUP BY 1, 2
ORDER BY d.day DESC, d.site_id
