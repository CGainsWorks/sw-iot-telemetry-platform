"""Choosing which table answers a telemetry query.

There are four places the same measurement can be read from, and picking the
wrong one is how a dashboard ends up either slow or subtly wrong:

===================  ============  ===========  =========================
Source               Resolution    Freshness    Retention
===================  ============  ===========  =========================
``telemetry``        per sample    seconds      30 days
``telemetry_1m_live``1 minute      seconds      2 days
``telemetry_1m``     1 minute      ~1 minute    180 days
``telemetry_1h``     1 hour        ~1 hour      730 days
===================  ============  ===========  =========================

The rules below encode the trade-off once, here, rather than leaving each
caller to guess:

* An explicit resolution is honoured, because a caller who asked for raw samples
  wants raw samples.
* Otherwise the coarsest resolution that still yields a useful number of points
  across the requested window wins. Returning 400,000 raw rows to draw a 900
  pixel chart helps nobody.
* A window ending in the last couple of minutes prefers the live rollup over the
  continuous aggregate, because the continuous aggregate has not materialised
  that far yet and would return an empty tail.
* A window older than a source's retention is never served from that source,
  even if the resolution would fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class Resolution(str, Enum):
    RAW = "raw"
    MINUTE = "1m"
    HOUR = "1h"
    AUTO = "auto"


class Source(str, Enum):
    RAW = "telemetry"
    LIVE_MINUTE = "telemetry_1m_live"
    MINUTE = "telemetry_1m"
    HOUR = "telemetry_1h"


#: Retention per source, matching db/migrations/0004_retention_compression.sql.
#: Duplicated here rather than read from the database because the API has to be
#: able to answer "that window is beyond retention" without a round trip. The
#: test suite asserts the two agree.
RETENTION: dict[Source, timedelta] = {
    Source.RAW: timedelta(days=30),
    Source.LIVE_MINUTE: timedelta(days=2),
    Source.MINUTE: timedelta(days=180),
    Source.HOUR: timedelta(days=730),
}

#: How far behind the continuous aggregate can be before the live rollup is a
#: better answer for the tail of a window. Matches the 1 minute end_offset plus
#: the 1 minute schedule_interval of the refresh policy, with margin.
CAGG_LAG = timedelta(minutes=3)

#: Target number of points to return. Roughly a wide chart's pixel count, which
#: is the most a caller can actually display.
TARGET_POINTS = 1200


@dataclass(frozen=True)
class Plan:
    source: Source
    resolution: Resolution
    #: Column holding the timestamp in the chosen source.
    time_column: str
    #: True when values are pre-aggregated and carry avg/min/max rather than one
    #: reading.
    aggregated: bool
    reason: str


_PLANS: dict[Source, tuple[Resolution, str, bool]] = {
    Source.RAW: (Resolution.RAW, "ts", False),
    Source.LIVE_MINUTE: (Resolution.MINUTE, "bucket", True),
    Source.MINUTE: (Resolution.MINUTE, "bucket", True),
    Source.HOUR: (Resolution.HOUR, "bucket", True),
}


def _plan(source: Source, reason: str) -> Plan:
    resolution, time_column, aggregated = _PLANS[source]
    return Plan(
        source=source,
        resolution=resolution,
        time_column=time_column,
        aggregated=aggregated,
        reason=reason,
    )


class RetentionError(ValueError):
    """Raised when no source still holds the requested window."""


def choose(
    *,
    start: datetime,
    end: datetime,
    requested: Resolution = Resolution.AUTO,
    now: datetime | None = None,
    target_points: int = TARGET_POINTS,
) -> Plan:
    """Pick the source that should answer a query for ``start`` to ``end``."""
    current = now or datetime.now(UTC)
    if end <= start:
        raise ValueError("end must be after start")

    age = current - start
    span = end - start
    tail_is_recent = (current - end) <= CAGG_LAG

    if requested is Resolution.RAW:
        if age > RETENTION[Source.RAW]:
            raise RetentionError(
                f"raw samples are kept for {RETENTION[Source.RAW].days} days; "
                f"the window starts {age.days} days ago"
            )
        return _plan(Source.RAW, "caller asked for raw samples")

    if requested is Resolution.MINUTE:
        return _minute_source(age, tail_is_recent, "caller asked for minute resolution")

    if requested is Resolution.HOUR:
        if age > RETENTION[Source.HOUR]:
            raise RetentionError(
                f"hourly rollups are kept for {RETENTION[Source.HOUR].days} days; "
                f"the window starts {age.days} days ago"
            )
        return _plan(Source.HOUR, "caller asked for hourly resolution")

    # Automatic. Work from coarse to fine and take the first that gives enough
    # points, so the cheapest adequate source wins.
    if span / timedelta(hours=1) >= target_points:
        if age > RETENTION[Source.HOUR]:
            raise RetentionError("window is older than every retention policy")
        return _plan(
            Source.HOUR,
            f"window spans {span.days} days, hourly keeps the result under "
            f"{target_points} points",
        )

    if span / timedelta(minutes=1) >= target_points:
        return _minute_source(
            age, tail_is_recent, f"window spans {_hours(span):.1f} hours, minute resolution fits"
        )

    if age > RETENTION[Source.RAW]:
        return _minute_source(
            age, tail_is_recent, "window is older than raw retention, using the minute rollup"
        )

    return _plan(Source.RAW, f"window spans {_minutes(span):.0f} minutes, raw samples fit")


def _minute_source(age: timedelta, tail_is_recent: bool, reason: str) -> Plan:
    """Live rollup or continuous aggregate, for a minute-resolution query."""
    if age > RETENTION[Source.MINUTE]:
        raise RetentionError(
            f"minute rollups are kept for {RETENTION[Source.MINUTE].days} days; "
            f"the window starts {age.days} days ago"
        )
    if tail_is_recent and age <= RETENTION[Source.LIVE_MINUTE]:
        return _plan(
            Source.LIVE_MINUTE,
            f"{reason}; window ends inside the continuous aggregate's refresh lag, "
            "so the live rollup is fresher",
        )
    return _plan(Source.MINUTE, reason)


def _hours(delta: timedelta) -> float:
    return delta.total_seconds() / 3600.0


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0
