-- Whether the alerting is any good.
--
-- An alerting system is judged by two failure modes, and neither is visible from
-- the alerts themselves:
--
--   Noise      rules that fire constantly and get ignored. Shown by a high
--              count with a short median duration: the condition was never
--              really there.
--   Flapping   the same rule firing repeatedly on the same device. Shown by
--              alerts per device: a well-tuned rule with recovery hysteresis
--              produces roughly one alert per incident.
--
-- Mean time to resolve is included because it is always asked for, but the
-- median is the number to read. One alert left open over a weekend drags the
-- mean somewhere useless.

WITH resolved AS (
  SELECT
    rule_id,
    severity,
    device_id,
    site_id,
    fired_at,
    resolved_at,
    notification_count,
    {{SECONDS_BETWEEN(fired_at, resolved_at)}} AS duration_seconds
  FROM {{table:alert_history}}
  WHERE fired_at >= {{DAYS_AGO(30)}}
    AND resolved_at IS NOT NULL
)

SELECT
  rule_id,
  severity,
  COUNT(*)                                                  AS alerts,
  COUNT(DISTINCT device_id)                                 AS devices_affected,
  ROUND({{SAFE_DIVIDE(COUNT(*), COUNT(DISTINCT device_id))}}, 2)
                                                            AS alerts_per_device,
  ROUND({{MEDIAN(duration_seconds)}}, 0) AS median_duration_s,
  ROUND(AVG(duration_seconds), 0)                           AS mean_duration_s,
  ROUND(MAX(duration_seconds), 0)                           AS longest_duration_s,
  SUM(notification_count)                                   AS notifications_sent,
  -- Alerts that resolved inside two minutes. A rule with a `for` hold should
  -- almost never produce these, so a high share means the hold is too short or
  -- the recovery window is.
  SUM(CASE WHEN duration_seconds < 120 THEN 1 ELSE 0 END)   AS short_lived_alerts,
  ROUND(
    100.0 * {{SAFE_DIVIDE(SUM(CASE WHEN duration_seconds < 120 THEN 1 ELSE 0 END), COUNT(*))}},
    1
  ) AS short_lived_pct
FROM resolved
GROUP BY 1, 2
ORDER BY alerts DESC
