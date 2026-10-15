-- Devices whose readings disagree with their neighbours.
--
-- A drifting sensor is invisible to a threshold rule: it reads 23 degC in a room
-- that is 19, which is entirely plausible and never crosses any limit. What
-- gives it away is that every other device at the same site, on the same day,
-- reads 19.
--
-- So the comparison is against the site's own distribution rather than against a
-- fixed number, and the score is a robust one. A plain z-score uses the mean and
-- the standard deviation, both of which are dragged around by the very outlier
-- being looked for; on a site with three devices, one bad sensor moves the mean
-- by a third of its own error. The median and the median absolute deviation are
-- not. The 1.4826 factor makes the MAD comparable to a standard deviation for
-- normally distributed data, so a threshold of 3 means roughly what it would
-- mean for a z-score.

WITH device_day AS (
  SELECT
    {{TO_DATE(bucket)}} AS day,
    site_id,
    device_id,
    metric,
    {{SAFE_DIVIDE(SUM(avg_value * sample_count), SUM(sample_count))}} AS mean_value,
    SUM(sample_count) AS samples
  FROM {{table:telemetry_hourly}}
  WHERE bucket >= {{DAYS_AGO(14)}}
  GROUP BY 1, 2, 3, 4
  -- A device with a handful of samples has a mean that means very little.
  HAVING SUM(sample_count) >= 100
),

site_day AS (
  SELECT
    day,
    site_id,
    metric,
    COUNT(*)                                      AS device_count,
    {{MEDIAN(mean_value)}}    AS median_value
  FROM device_day
  GROUP BY 1, 2, 3
  -- Two devices have no majority to disagree with. Three is the minimum where
  -- "the odd one out" means anything.
  HAVING COUNT(*) >= 3
),

deviations AS (
  SELECT
    d.day,
    d.site_id,
    d.metric,
    d.device_id,
    d.mean_value,
    s.median_value,
    s.device_count,
    ABS(d.mean_value - s.median_value) AS absolute_deviation
  FROM device_day d
  JOIN site_day s
    ON s.day = d.day AND s.site_id = d.site_id AND s.metric = d.metric
),

scale AS (
  SELECT
    day,
    site_id,
    metric,
    {{MEDIAN(absolute_deviation)}} AS median_absolute_deviation
  FROM deviations
  GROUP BY 1, 2, 3
)

SELECT
  v.day,
  v.site_id,
  v.metric,
  v.device_id,
  ROUND(v.mean_value, 3)    AS device_mean,
  ROUND(v.median_value, 3)  AS site_median,
  v.device_count,
  ROUND(
    {{SAFE_DIVIDE(v.absolute_deviation, 1.4826 * s.median_absolute_deviation)}},
    2
  ) AS robust_score
FROM deviations v
JOIN scale s
  ON s.day = v.day AND s.site_id = v.site_id AND s.metric = v.metric
-- A zero MAD means every device agreed exactly, which for real sensor data means
-- the readings are synthetic or stuck rather than that everyone is an outlier.
WHERE s.median_absolute_deviation > 0
  AND {{SAFE_DIVIDE(v.absolute_deviation, 1.4826 * s.median_absolute_deviation)}} > 3.0
ORDER BY robust_score DESC
LIMIT 200
