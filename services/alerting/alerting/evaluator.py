"""Turning a rule into a query, and a query result into a breach.

Every rule kind is answered by one SQL statement against data the platform
already stores. Evaluation happens on a timer against the live rollup rather
than inside the stream, for three reasons: the rollup is already there, a
restarted evaluator recovers its view of the world from the database instead of
from replayed Kafka offsets, and a rule with a fifteen-minute window does not
need fifteen minutes of state held in memory to answer.

The cost is that alert latency is bounded below by the evaluation interval.
At the default of fifteen seconds that is well inside every ``for`` duration in
the catalogue, so it changes nothing about when an alert actually fires.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from telemetry_common.logging import get_logger

from .rules import OPERATORS, Rule, RuleKind

log = get_logger(__name__)

#: Sentinel device for alerts about the platform rather than about a device.
PLATFORM_DEVICE = "platform"
PLATFORM_SITE = "platform"


@dataclass(frozen=True)
class Breach:
    """One device failing one rule, right now."""

    rule: Rule
    device_id: str
    site_id: str
    value: float
    message: str
    metric: str | None = None
    observed_at: datetime | None = None


def _scope_clauses(rule: Rule, params: list[Any], *, alias: str = "") -> list[str]:
    """Append scope parameters and return the matching SQL clauses."""
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    if rule.scope.site_id:
        params.append(rule.scope.site_id)
        clauses.append(f"{prefix}site_id = ${len(params)}")
    if rule.scope.device_id:
        params.append(rule.scope.device_id)
        clauses.append(f"{prefix}device_id = ${len(params)}")
    return clauses


class Evaluator:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def evaluate(self, rule: Rule) -> list[Breach]:
        if not rule.enabled:
            return []
        handler = {
            RuleKind.THRESHOLD: self._threshold,
            RuleKind.FLATLINE: self._flatline,
            RuleKind.RATE_OF_CHANGE: self._rate_of_change,
            RuleKind.OFFLINE: self._offline,
            RuleKind.DEAD_LETTER_RATE: self._dead_letter_rate,
            RuleKind.CERTIFICATE_EXPIRY: self._certificate_expiry,
        }[rule.kind]
        return await handler(rule)

    # -- threshold ---------------------------------------------------------

    async def _threshold(self, rule: Rule) -> list[Breach]:
        """Fire when the most recent ``for`` worth of buckets all breach.

        ``window`` is how far back to look for buckets; ``for`` decides how many
        of the newest ones must all be breaching. Requiring all of them, not
        most, is what ``for`` means: a single recovering bucket resets the clock.

        Selecting the newest N buckets rather than filtering on
        ``bucket > now() - for`` matters more than it looks. A two-minute hold
        filtered by time spans exactly 120 seconds, which contains one or two
        one-minute buckets depending on where the clock happens to sit, so the
        rule fires or does not fire according to alignment. Ranking and taking
        the newest two is stable whatever the alignment.
        """
        # Both interpolations come from closed enums: Aggregation.column is one
        # of four literal column names and OPERATORS is a fixed dict. No rule
        # author string reaches the SQL text.
        column = rule.aggregation.column
        operator = OPERATORS[rule.operator]

        params: list[Any] = [rule.threshold, rule.metric, rule.window_s, rule.min_buckets]
        clauses = [
            "metric = $2",
            "bucket > now() - make_interval(secs => $3)",
        ]
        clauses.extend(_scope_clauses(rule, params))

        sql = f"""
            WITH ranked AS (
                SELECT
                    device_id,
                    site_id,
                    bucket,
                    {column} AS level,
                    row_number() OVER (
                        PARTITION BY device_id, metric ORDER BY bucket DESC
                    ) AS recency
                FROM telemetry_1m_live
                WHERE {' AND '.join(clauses)}
            )
            SELECT
                device_id,
                site_id,
                count(*)                                     AS buckets,
                count(*) FILTER (WHERE level {operator} $1)   AS breaching,
                (array_agg(level ORDER BY bucket DESC))[1]    AS current_value,
                max(bucket)                                   AS observed_at
            FROM ranked
            WHERE recency <= $4::int
            GROUP BY device_id, site_id
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        breaches: list[Breach] = []
        for row in rows:
            if row["buckets"] < rule.min_buckets:
                # Not enough history yet. A device that has just come online has
                # not been breaching for two minutes, whatever its current value.
                continue
            if row["breaching"] != row["buckets"]:
                continue
            breaches.append(
                Breach(
                    rule=rule,
                    device_id=row["device_id"],
                    site_id=row["site_id"],
                    metric=rule.metric,
                    value=float(row["current_value"]),
                    observed_at=row["observed_at"],
                    message=(
                        f"{rule.aggregation.value}({rule.metric}) is "
                        f"{row['current_value']:.3g}, {rule.operator} {rule.threshold:g} "
                        f"for {int(rule.for_s)}s"
                    ),
                )
            )
        return breaches

    # -- flatline ----------------------------------------------------------

    async def _flatline(self, rule: Rule) -> list[Breach]:
        """Fire when a metric has barely moved across the whole window.

        Uses max(max_value) - min(min_value) rather than a standard deviation.
        The range is exact whatever the sample count, whereas a pooled standard
        deviation across buckets of unequal size needs care to be meaningful,
        and the question here is simply "did this reading move at all".
        """
        params: list[Any] = [rule.metric, rule.window_s, rule.threshold]
        clauses = ["metric = $1", "bucket > now() - make_interval(secs => $2)"]
        clauses.extend(_scope_clauses(rule, params))

        sql = f"""
            SELECT
                device_id,
                site_id,
                max(max_value) - min(min_value) AS spread,
                (array_agg(last_value ORDER BY bucket DESC))[1] AS current_value,
                count(*) AS buckets,
                max(bucket) AS observed_at
            FROM telemetry_1m_live
            WHERE {' AND '.join(clauses)}
            GROUP BY device_id, site_id
            HAVING max(max_value) - min(min_value) <= $3
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        # Require most of the window to be present. A device that reported twice
        # and went quiet has a tiny spread for reasons that have nothing to do
        # with a stuck sensor.
        required = max(2, int(rule.window_s // 60) // 2)
        return [
            Breach(
                rule=rule,
                device_id=row["device_id"],
                site_id=row["site_id"],
                metric=rule.metric,
                value=float(row["spread"]),
                observed_at=row["observed_at"],
                message=(
                    f"{rule.metric} moved only {row['spread']:.4g} over "
                    f"{int(rule.window_s / 60)} minutes and is stuck at "
                    f"{row['current_value']:.4g}"
                ),
            )
            for row in rows
            if row["buckets"] >= required
        ]

    # -- rate of change ----------------------------------------------------

    async def _rate_of_change(self, rule: Rule) -> list[Breach]:
        """Fire on the slope of a metric rather than its level.

        Two design decisions here, both learned by watching the rule misfire.

        **The slope is measured between the two halves of the window, not
        between its first and last bucket.** Endpoint sampling is at the mercy
        of whatever those two particular minutes happened to contain. Comparing
        an aggregate of the older half against an aggregate of the newer half
        uses every bucket and is far harder to fool.

        **The rule picks which level it tracks.** For a duty-cycled machine the
        mean swings between an idle level near 0.012 g and a running level near
        0.32 g every few minutes, so a slope on the mean fires on every motor
        start and means nothing. The discriminating signal is the idle floor,
        ``min(min_value)`` across the window: duty cycling moves between two
        fixed levels and leaves the floor where it is, whereas a developing
        bearing fault lifts the whole signal, floor included.

        Note that the floor has to be taken across the window, not per bucket.
        A one-minute bucket during which the motor ran the whole minute has a
        ``min_value`` at the running level, so per-bucket minima track the duty
        cycle just as faithfully as the mean does.
        """
        operator = OPERATORS[rule.operator]

        # How each aggregation collapses a set of buckets into one level. Closed
        # set, keyed by the Aggregation enum, so nothing a rule author writes
        # reaches the SQL text.
        level_expr = {
            "avg": "sum(avg_value * sample_count) / NULLIF(sum(sample_count), 0)",
            "min": "min(min_value)",
            "max": "max(max_value)",
            "last": "last(last_value, bucket)",
        }[rule.aggregation.value]

        # Each half must hold enough buckets to be worth comparing. A rule with
        # a 45 minute window that has only seen four minutes of data has not
        # observed a trend, it has observed a start-up.
        min_buckets_per_half = max(3, int(rule.window_s // 240))

        params: list[Any] = [
            rule.metric,
            rule.window_s,
            rule.threshold,
            min_buckets_per_half,
        ]
        clauses = ["metric = $1", "bucket > now() - make_interval(secs => $2)"]
        clauses.extend(_scope_clauses(rule, params))

        sql = f"""
            WITH halves AS (
                SELECT
                    device_id,
                    site_id,
                    (bucket >= now() - make_interval(secs => $2 / 2.0)) AS recent,
                    {level_expr} AS level,
                    count(*)     AS buckets,
                    max(bucket)  AS latest
                FROM telemetry_1m_live
                WHERE {' AND '.join(clauses)}
                GROUP BY device_id, site_id, recent
            ),
            paired AS (
                SELECT
                    device_id,
                    site_id,
                    max(level)   FILTER (WHERE NOT recent) AS earlier_level,
                    max(level)   FILTER (WHERE recent)     AS later_level,
                    max(buckets) FILTER (WHERE NOT recent) AS earlier_buckets,
                    max(buckets) FILTER (WHERE recent)     AS later_buckets,
                    max(latest)                            AS observed_at
                FROM halves
                GROUP BY device_id, site_id
            )
            SELECT
                device_id, site_id, earlier_level, later_level, observed_at,
                -- The two half-window centres are window/2 seconds apart, which
                -- is window/120 minutes.
                (later_level - earlier_level) / ($2 / 120.0) AS per_minute
            FROM paired
            WHERE earlier_level IS NOT NULL
              AND later_level IS NOT NULL
              AND earlier_buckets >= $4::int
              AND later_buckets   >= $4::int
              AND (later_level - earlier_level) / ($2 / 120.0) {operator} $3
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            Breach(
                rule=rule,
                device_id=row["device_id"],
                site_id=row["site_id"],
                metric=rule.metric,
                value=float(row["per_minute"]),
                observed_at=row["observed_at"],
                message=(
                    f"{rule.aggregation.value}({rule.metric}) changing at "
                    f"{row['per_minute']:.4g} per minute "
                    f"({row['earlier_level']:.4g} to {row['later_level']:.4g} "
                    f"across a {int(rule.window_s / 60)} minute window)"
                ),
            )
            for row in rows
        ]

    # -- offline -----------------------------------------------------------

    async def _offline(self, rule: Rule) -> list[Breach]:
        """Fire when a device has not been heard from.

        Reads the registry rather than the telemetry table: the absence of rows
        cannot be found by querying rows.
        """
        params: list[Any] = [rule.threshold]
        clauses = [
            "last_seen IS NOT NULL",
            "now() - last_seen > make_interval(secs => $1)",
        ]
        clauses.extend(_scope_clauses(rule, params))
        if rule.scope.profile:
            params.append(rule.scope.profile)
            clauses.append(f"profile = ${len(params)}")

        sql = f"""
            SELECT device_id, site_id, last_seen,
                   EXTRACT(EPOCH FROM (now() - last_seen)) AS silent_seconds
            FROM devices
            WHERE {' AND '.join(clauses)}
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            Breach(
                rule=rule,
                device_id=row["device_id"],
                site_id=row["site_id"],
                value=float(row["silent_seconds"]),
                observed_at=row["last_seen"],
                message=(
                    f"no telemetry for {int(row['silent_seconds'])}s "
                    f"(last seen {row['last_seen']:%Y-%m-%d %H:%M:%SZ})"
                ),
            )
            for row in rows
        ]

    # -- platform ----------------------------------------------------------

    async def _dead_letter_rate(self, rule: Rule) -> list[Breach]:
        operator = OPERATORS[rule.operator]
        sql = f"""
            SELECT count(*)::float8 AS rejected,
                   (array_agg(reason ORDER BY received_at DESC))[1] AS top_reason
            FROM dead_letters
            WHERE received_at > now() - make_interval(secs => $1)
            HAVING count(*) {operator} $2
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, rule.window_s, rule.threshold)

        if row is None:
            return []
        return [
            Breach(
                rule=rule,
                device_id=PLATFORM_DEVICE,
                site_id=PLATFORM_SITE,
                value=float(row["rejected"]),
                message=(
                    f"{int(row['rejected'])} messages rejected in the last "
                    f"{int(rule.window_s / 60)} minutes, most recently for "
                    f"{row['top_reason']}"
                ),
            )
        ]

    async def _certificate_expiry(self, rule: Rule) -> list[Breach]:
        params: list[Any] = [rule.threshold]
        clauses = [
            "certificate_expires_at IS NOT NULL",
            "certificate_expires_at < now() + make_interval(days => $1::int)",
        ]
        clauses.extend(_scope_clauses(rule, params))

        sql = f"""
            SELECT device_id, site_id, certificate_expires_at,
                   EXTRACT(EPOCH FROM (certificate_expires_at - now())) / 86400.0 AS days_left
            FROM devices
            WHERE {' AND '.join(clauses)}
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [
            Breach(
                rule=rule,
                device_id=row["device_id"],
                site_id=row["site_id"],
                value=float(row["days_left"]),
                observed_at=row["certificate_expires_at"],
                message=(
                    f"certificate expires in {row['days_left']:.1f} days "
                    f"({row['certificate_expires_at']:%Y-%m-%d})"
                ),
            )
            for row in rows
        ]


async def evaluate_all(evaluator: Evaluator, rules: Sequence[Rule]) -> list[Breach]:
    """Evaluate every rule, letting one bad rule fail without stopping the rest."""
    breaches: list[Breach] = []
    for rule in rules:
        try:
            breaches.extend(await evaluator.evaluate(rule))
        except Exception as exc:
            log.error("rule evaluation failed", rule_id=rule.rule_id, error=str(exc))
    return breaches
