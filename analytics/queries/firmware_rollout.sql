-- How a firmware or schema rollout is progressing.
--
-- A fleet is never on one version. The useful view is the share of devices on
-- each version over time, because that is what shows a rollout that has stalled:
-- the curve for the new version flattens well short of 100 per cent, and the
-- devices left behind are the ones that are asleep, on a bad link, or bricked.
--
-- Schema version is tracked alongside firmware because they move independently.
-- A firmware release can change behaviour without touching the message contract,
-- and the platform cares about the contract.

WITH snapshots AS (
  SELECT
    snapshot_date,
    fw_version,
    schema_version,
    COUNT(*) AS devices
  FROM {{table:device_dim}}
  WHERE snapshot_date >= {{DATE_DAYS_AGO(90)}}
    AND fw_version IS NOT NULL
  GROUP BY 1, 2, 3
),

totals AS (
  SELECT snapshot_date, SUM(devices) AS fleet_size
  FROM snapshots
  GROUP BY 1
)

SELECT
  s.snapshot_date,
  s.fw_version,
  s.schema_version,
  s.devices,
  t.fleet_size,
  ROUND(100.0 * {{SAFE_DIVIDE(s.devices, t.fleet_size)}}, 2) AS share_pct
FROM snapshots s
JOIN totals t ON t.snapshot_date = s.snapshot_date
ORDER BY s.snapshot_date DESC, s.devices DESC
