"""Tumbling-window rollups computed in the stream.

Pure data structures, no database and no Kafka, so the aggregation maths is unit
tested on its own.

Why compute rollups here at all when TimescaleDB has continuous aggregates: the
continuous aggregate is the correct answer and it arrives a refresh interval
late. The dashboard and the alerting engine cannot wait that long, and neither
can tolerate a bucket that changes under them. So the stream keeps a small
window of state in memory, emits a provisional rollup within seconds, and the
continuous aggregate quietly overwrites history with the correct version later.

The aggregate carries a sum of squares rather than a running standard deviation,
because two partial windows can be merged by adding their sums but not by
combining their deviations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

WINDOW = timedelta(minutes=1)


def floor_to_window(ts: datetime, window: timedelta = WINDOW) -> datetime:
    """Round ``ts`` down to the start of its window.

    Uses the Unix epoch as the origin so every process in the fleet agrees on
    where a bucket starts, regardless of when it happened to be started.
    """
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    seconds = (ts - epoch).total_seconds()
    step = window.total_seconds()
    return epoch + timedelta(seconds=math.floor(seconds / step) * step)


@dataclass
class Aggregate:
    """Running statistics for one (device, metric, bucket)."""

    device_id: str
    site_id: str
    metric: str
    bucket: datetime
    count: int = 0
    total: float = 0.0
    sum_squares: float = 0.0
    minimum: float = math.inf
    maximum: float = -math.inf
    last_value: float = 0.0
    last_ts: datetime | None = None

    def add(self, value: float, ts: datetime) -> None:
        self.count += 1
        self.total += value
        self.sum_squares += value * value
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        # Guard against out-of-order arrival inside a window: last_value must be
        # the newest sample by device time, not by arrival time.
        if self.last_ts is None or ts >= self.last_ts:
            self.last_value = value
            self.last_ts = ts

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def stddev(self) -> float:
        """Sample standard deviation, or 0.0 for fewer than two samples."""
        if self.count < 2:
            return 0.0
        variance = (self.sum_squares - (self.total * self.total) / self.count) / (self.count - 1)
        # Floating point can push a variance that should be zero slightly
        # negative when every sample is identical.
        return math.sqrt(max(variance, 0.0))

    def merge(self, other: Aggregate) -> None:
        """Fold another partial aggregate for the same key into this one."""
        if other.count == 0:
            return
        self.count += other.count
        self.total += other.total
        self.sum_squares += other.sum_squares
        self.minimum = min(self.minimum, other.minimum)
        self.maximum = max(self.maximum, other.maximum)
        if other.last_ts is not None and (self.last_ts is None or other.last_ts >= self.last_ts):
            self.last_value = other.last_value
            self.last_ts = other.last_ts


@dataclass
class WindowAggregator:
    """Holds open windows and hands back the ones ready to write.

    ``lateness`` is how long a window stays open after its nominal end. A device
    with a slow link or a small clock offset delivers samples for a window a few
    seconds after it closed, and dropping them would make the live rollup
    systematically undercount the busiest devices. Anything later than this is
    counted and dropped from the live path; the continuous aggregate still picks
    it up from the raw table.
    """

    window: timedelta = WINDOW
    lateness: timedelta = timedelta(seconds=15)
    _open: dict[tuple[str, str, datetime], Aggregate] = field(default_factory=dict)
    late_samples: int = 0

    def add(self, *, device_id: str, site_id: str, metric: str, value: float, ts: datetime) -> None:
        bucket = floor_to_window(ts, self.window)
        key = (device_id, metric, bucket)
        aggregate = self._open.get(key)
        if aggregate is None:
            aggregate = Aggregate(
                device_id=device_id, site_id=site_id, metric=metric, bucket=bucket
            )
            self._open[key] = aggregate
        aggregate.add(value, ts)

    def ready(self, now: datetime) -> list[Aggregate]:
        """Remove and return every window whose lateness allowance has expired."""
        cutoff = now - self.window - self.lateness
        done = [agg for key, agg in self._open.items() if agg.bucket <= cutoff]
        for agg in done:
            del self._open[(agg.device_id, agg.metric, agg.bucket)]
        return done

    def snapshot(self) -> list[Aggregate]:
        """Every open window, without closing any of them.

        Used for the periodic upsert that makes a window visible while it is
        still filling. The write is an upsert precisely so the partial value can
        be replaced by the final one when the window closes.
        """
        return list(self._open.values())

    def drain(self) -> list[Aggregate]:
        """Close everything. Called on shutdown so nothing in memory is lost."""
        done = list(self._open.values())
        self._open.clear()
        return done

    def note_late(self, ts: datetime, now: datetime) -> bool:
        """Record a sample that arrived after its window was written out."""
        if floor_to_window(ts, self.window) <= now - self.window - self.lateness:
            self.late_samples += 1
            return True
        return False

    @property
    def open_windows(self) -> int:
        return len(self._open)
