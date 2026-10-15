"""Database access for the query API.

Read-only. Everything that writes lives in the processing and alerting services,
so nothing here needs a transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from telemetry_common.logging import get_logger

from .resolution import Plan, Source

log = get_logger(__name__)

#: Hard cap on rows returned by one telemetry query. A caller asking for a month
#: of raw samples across the whole fleet would otherwise pull the table into
#: memory on both ends. The response says when this was hit.
MAX_ROWS = 50_000


class Database:
    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min_size, max_size=self._max_size, command_timeout=30.0
        )
        log.info("api database pool ready")

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.start() was not awaited")
        return self._pool

    async def ping(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("SELECT 1")

    # -- devices -----------------------------------------------------------

    async def list_devices(
        self, *, site_id: str | None = None, health: str | None = None
    ) -> list[asyncpg.Record]:
        clauses: list[str] = []
        params: list[Any] = []
        if site_id:
            params.append(site_id)
            clauses.append(f"site_id = ${len(params)}")
        if health:
            params.append(health)
            clauses.append(f"health = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"SELECT * FROM device_health {where} ORDER BY device_id", *params
            )

    async def get_device(self, device_id: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM device_health WHERE device_id = $1", device_id
            )

    async def device_metrics(self, device_id: str) -> list[str]:
        """Which metrics this device has actually reported recently.

        Read from the live rollup rather than from the raw table: it is two
        orders of magnitude smaller and holds the same set of metric names for
        any device that is currently reporting.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT metric
                FROM telemetry_1m_live
                WHERE device_id = $1 AND bucket > now() - INTERVAL '6 hours'
                ORDER BY metric
                """,
                device_id,
            )
        return [row["metric"] for row in rows]

    # -- telemetry ---------------------------------------------------------

    async def query_telemetry(
        self,
        plan: Plan,
        *,
        start: datetime,
        end: datetime,
        device_id: str | None = None,
        site_id: str | None = None,
        metric: str | None = None,
        limit: int = MAX_ROWS,
    ) -> list[asyncpg.Record]:
        # The table name is interpolated, not parameterised, because PostgreSQL
        # does not accept a parameter in that position. It is safe because plan
        # comes from resolution.choose() and Source is a closed enum, so the only
        # possible values are the four table names defined there. No caller
        # string reaches this line.
        table = plan.source.value
        time_column = plan.time_column

        if plan.aggregated:
            select = (
                f"{time_column} AS ts, device_id, site_id, metric, "
                "avg_value AS value, min_value, max_value, sample_count"
            )
        else:
            select = (
                f"{time_column} AS ts, device_id, site_id, metric, value, "
                "NULL::float8 AS min_value, NULL::float8 AS max_value, "
                "NULL::int AS sample_count"
            )

        params: list[Any] = [start, end]
        clauses = [f"{time_column} >= $1", f"{time_column} < $2"]
        if device_id:
            params.append(device_id)
            clauses.append(f"device_id = ${len(params)}")
        if site_id:
            params.append(site_id)
            clauses.append(f"site_id = ${len(params)}")
        if metric:
            params.append(metric)
            clauses.append(f"metric = ${len(params)}")

        params.append(min(limit, MAX_ROWS))
        sql = (
            f"SELECT {select} FROM {table} "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {time_column} ASC, device_id, metric "
            f"LIMIT ${len(params)}"
        )

        async with self.pool.acquire() as conn:
            return await conn.fetch(sql, *params)

    async def latest_readings(
        self, *, site_id: str | None = None, metric: str | None = None
    ) -> list[asyncpg.Record]:
        """Newest value per device and metric.

        DISTINCT ON over the live rollup, which is small and indexed on
        (bucket DESC). The same query against the raw hypertable would have to
        scan every chunk in the window.
        """
        params: list[Any] = []
        clauses = ["bucket > now() - INTERVAL '15 minutes'"]
        if site_id:
            params.append(site_id)
            clauses.append(f"site_id = ${len(params)}")
        if metric:
            params.append(metric)
            clauses.append(f"metric = ${len(params)}")

        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"""
                SELECT DISTINCT ON (device_id, metric)
                    device_id, site_id, metric, last_value AS value,
                    min_value, max_value, sample_count, bucket AS ts
                FROM telemetry_1m_live
                WHERE {' AND '.join(clauses)}
                ORDER BY device_id, metric, bucket DESC
                """,
                *params,
            )

    async def site_rollup(self, *, hours: int = 1) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT
                    site_id,
                    metric,
                    sum(avg_value * sample_count) / NULLIF(sum(sample_count), 0) AS avg_value,
                    min(min_value)          AS min_value,
                    max(max_value)          AS max_value,
                    count(DISTINCT device_id) AS device_count,
                    sum(sample_count)::bigint AS sample_count
                FROM telemetry_1m_live
                WHERE bucket > now() - make_interval(hours => $1)
                GROUP BY site_id, metric
                ORDER BY site_id, metric
                """,
                hours,
            )

    # -- alerts ------------------------------------------------------------

    async def list_alerts(
        self,
        *,
        state: str | None = "firing",
        severity: str | None = None,
        device_id: str | None = None,
        limit: int = 500,
    ) -> list[asyncpg.Record]:
        params: list[Any] = []
        clauses: list[str] = []
        if state:
            params.append(state)
            clauses.append(f"a.state = ${len(params)}")
        if severity:
            params.append(severity)
            clauses.append(f"a.severity = ${len(params)}")
        if device_id:
            params.append(device_id)
            clauses.append(f"a.device_id = ${len(params)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self.pool.acquire() as conn:
            return await conn.fetch(
                f"""
                SELECT
                    a.alert_id::text, a.rule_id, r.name AS rule_name, a.device_id, a.site_id,
                    a.metric, a.severity, a.state, a.value, a.threshold, a.message,
                    a.fired_at, a.last_seen_at, a.resolved_at, a.notification_count
                FROM alerts a
                LEFT JOIN alert_rules r ON r.rule_id = a.rule_id
                {where}
                ORDER BY a.fired_at DESC
                LIMIT ${len(params)}
                """,
                *params,
            )

    async def list_rules(self) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT rule_id, name, severity, metric, kind, description, "
                "definition, enabled, updated_at FROM alert_rules ORDER BY rule_id"
            )

    # -- dead letters ------------------------------------------------------

    async def dead_letter_summary(self, *, hours: int = 24) -> list[asyncpg.Record]:
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                """
                SELECT
                    reason,
                    count(*)::bigint AS count,
                    max(received_at) AS latest,
                    (array_agg(detail ORDER BY received_at DESC))[1] AS example_detail
                FROM dead_letters
                WHERE received_at > now() - make_interval(hours => $1)
                GROUP BY reason
                ORDER BY count DESC
                """,
                hours,
            )

    # -- platform ----------------------------------------------------------

    async def platform_stats(self) -> dict[str, Any]:
        """One round trip for the header tiles.

        Split into CTEs rather than several awaits so every number describes the
        same instant. Separate queries would let a device appear in one tile and
        not another.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH device_counts AS (
                    SELECT
                        count(*)::bigint AS total,
                        count(*) FILTER (WHERE health = 'healthy')::bigint AS healthy,
                        count(*) FILTER (WHERE health IN ('offline', 'never_seen'))::bigint
                            AS offline
                    FROM device_health
                ),
                recent AS (
                    SELECT
                        sum(sample_count) FILTER (WHERE bucket > now() - INTERVAL '1 hour')
                            AS last_hour,
                        count(DISTINCT metric) AS metrics_tracked
                    FROM telemetry_1m_live
                    WHERE bucket > now() - INTERVAL '1 hour'
                ),
                daily AS (
                    SELECT sum(sample_count) AS last_24h
                    FROM telemetry_1m
                    WHERE bucket > now() - INTERVAL '24 hours'
                ),
                alerting AS (
                    SELECT count(*)::bigint AS active FROM alerts WHERE state = 'firing'
                ),
                dlq AS (
                    SELECT count(*)::bigint AS last_hour
                    FROM dead_letters WHERE received_at > now() - INTERVAL '1 hour'
                ),
                span AS (
                    SELECT min(bucket) AS oldest, max(bucket) AS newest FROM telemetry_1m_live
                )
                SELECT
                    device_counts.total          AS devices_total,
                    device_counts.healthy        AS devices_healthy,
                    device_counts.offline        AS devices_offline,
                    COALESCE(recent.last_hour, 0)::bigint  AS readings_last_hour,
                    COALESCE(daily.last_24h, 0)::bigint    AS readings_last_24h,
                    alerting.active              AS active_alerts,
                    dlq.last_hour                AS dead_letters_last_hour,
                    COALESCE(recent.metrics_tracked, 0)::bigint AS metrics_tracked,
                    span.oldest                  AS oldest_reading,
                    span.newest                  AS newest_reading
                FROM device_counts, recent, daily, alerting, dlq, span
                """
            )
        return dict(row) if row else {}

    async def retention_settings(self) -> dict[str, timedelta]:
        """Retention actually configured in the database.

        Used by a test to prove the table in resolution.py has not drifted away
        from the migrations.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT hypertable_name, config ->> 'drop_after' AS drop_after
                FROM timescaledb_information.jobs
                WHERE proc_name = 'policy_retention'
                """
            )
        result: dict[str, timedelta] = {}
        for row in rows:
            if row["drop_after"]:
                result[row["hypertable_name"]] = _parse_interval(row["drop_after"])
        return result


def _parse_interval(raw: str) -> timedelta:
    """Parse the ``N days`` form PostgreSQL returns for these policies."""
    parts = raw.split()
    if len(parts) >= 2 and parts[1].startswith("day"):
        return timedelta(days=int(parts[0]))
    if len(parts) >= 2 and parts[1].startswith("hour"):
        return timedelta(hours=int(parts[0]))
    return timedelta(0)


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = ["Database", "MAX_ROWS", "Source", "utcnow"]
