# Analytical layer

Five queries that answer questions TimescaleDB is the wrong tool for, plus the
exporter that gets the data there and a local runner that makes the SQL
testable.

## Why a second copy of the data exists

TimescaleDB answers "what is this device doing now" in milliseconds and
deliberately throws history away: raw samples last 30 days, minute rollups 180.
BigQuery answers "how has the fleet behaved over three years" and does not care
that it takes eight seconds.

Pointing analytical dashboards at TimescaleDB instead puts a scan across every
chunk in the way of the queries the live dashboard depends on, which is how a
reporting request takes an operational system down.

## The queries

| Query | Answers |
| --- | --- |
| `data_completeness.sql` | how much of what the fleet should have sent actually arrived, per device per day, scaled by each profile's own cadence |
| `fleet_health_daily.sql` | devices reporting, gaps, firmware and schema spread, certificates expiring, per site per day |
| `metric_outliers.sql` | devices whose readings disagree with their neighbours, which is the only way to spot a drifting sensor |
| `alert_reliability.sql` | whether the alerting is noise: counts, durations, flapping |
| `firmware_rollout.sql` | share of the fleet on each version over time, which is how a stalled rollout becomes visible |

Two are worth reading for their method.

**`metric_outliers.sql`** uses a median and a median absolute deviation rather
than a mean and a standard deviation. A plain z-score is dragged around by the
very outlier it is looking for: on a site with three devices, one bad sensor
moves the mean by a third of its own error. The 1.4826 factor rescales the MAD
so a threshold of 3 means roughly what it would for a z-score.

**`data_completeness.sql`** joins to the device dimension to get each profile's
publish interval. Treating a 30 second air quality node as though it should
report every 10 seconds would report two thirds of its data missing.

## One set of queries, two dialects

The queries target BigQuery. Testing them there needs a project, a billing
account and credentials, which in practice means the queries in a repository are
the ones nobody has ever run.

So they are written once against a small macro vocabulary and rendered into
either BigQuery Standard SQL or DuckDB. CI runs all five against DuckDB on
generated fixtures, with `--require-rows`, because a query that returns nothing
has only been shown to parse.

```bash
# Against generated fixtures. No stack needed.
python analytics/local/run_queries.py --fixtures --require-rows

# Against real exported data.
python analytics/export/export.py --days 7 --out data/export
python analytics/local/run_queries.py --data data/export
```

The macro vocabulary is deliberately tiny and only covers constructs where the
two dialects genuinely differ. See [`dialect.py`](dialect.py) for the table.

**Macros do not nest.** Writing one inside another looks like it should work and
does not: the renderer treats the inner call as a literal argument and emits a
function neither warehouse has. Rather than make the renderer recursive, the
compositions that are needed have their own entry, and a guard turns the mistake
into an error at render time rather than a billing event. That guard exists
because the mistake was made.

## The data path

```
TimescaleDB          analytics/export        GCS landing        BigQuery
telemetry_1h    ->   Parquet, snappy    ->   30 day expiry  ->  telemetry_hourly
devices         ->                                          ->  device_dim
alerts          ->                                          ->  alert_history
```

Parquet because both warehouses load it natively, it is columnar so a query
touching two of nine columns reads two, and it carries its own schema so a
column rename is a load error rather than a silently misaligned column.

Table schemas live in [`schemas/`](schemas/) as JSON and are read by **both** the
Terraform and the loader, so there is one definition.

## Partitioning

Daily, not hourly, even though the data is hourly. BigQuery caps a table at
10,000 partitions, which is under 14 months of hours; daily partitioning with
clustering on `(metric, site_id, device_id)` gives the same pruning without the
ceiling.

Clustering order is a prefix, so the column most queries filter on goes first.
Get this wrong and BigQuery becomes the largest line item in the system: an
unpartitioned table means every query scans everything, and a dashboard
refreshing every minute against a year of data adds up quickly at per-terabyte
pricing.
