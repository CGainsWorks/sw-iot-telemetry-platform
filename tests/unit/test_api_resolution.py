"""Source selection for telemetry queries.

Getting this wrong is not a crash, it is a chart that is quietly empty or a
query that scans thirty days of raw samples to draw nine hundred pixels. Both
are the kind of bug that survives to production, so the rules get their own
tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api.resolution import (
    CAGG_LAG,
    RETENTION,
    Resolution,
    RetentionError,
    Source,
    choose,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def plan(*, span: timedelta, ends_ago: timedelta = timedelta(0), requested=Resolution.AUTO):
    end = NOW - ends_ago
    return choose(start=end - span, end=end, requested=requested, now=NOW)


class TestExplicitResolution:
    def test_raw_is_honoured_for_a_recent_window(self):
        assert plan(span=timedelta(hours=6), requested=Resolution.RAW).source is Source.RAW

    def test_raw_beyond_retention_is_refused_rather_than_silently_downgraded(self):
        # Returning minute rollups to someone who asked for raw samples would be
        # a different answer to the question they asked.
        with pytest.raises(RetentionError, match="raw samples are kept"):
            plan(span=timedelta(days=1), ends_ago=timedelta(days=40), requested=Resolution.RAW)

    def test_hourly_is_honoured_even_for_a_short_window(self):
        assert plan(span=timedelta(minutes=10), requested=Resolution.HOUR).source is Source.HOUR

    def test_hourly_beyond_its_retention_is_refused(self):
        with pytest.raises(RetentionError, match="hourly rollups"):
            plan(span=timedelta(days=1), ends_ago=timedelta(days=800), requested=Resolution.HOUR)

    def test_minute_beyond_its_retention_is_refused(self):
        with pytest.raises(RetentionError, match="minute rollups"):
            plan(span=timedelta(days=1), ends_ago=timedelta(days=200), requested=Resolution.MINUTE)


class TestLiveVersusMaterialised:
    def test_a_window_ending_now_prefers_the_live_rollup(self):
        # The continuous aggregate has not materialised the last minute yet, so
        # asking it would return an empty tail on every dashboard.
        result = plan(span=timedelta(hours=2), requested=Resolution.MINUTE)
        assert result.source is Source.LIVE_MINUTE
        assert "fresher" in result.reason

    def test_a_window_ending_well_in_the_past_uses_the_continuous_aggregate(self):
        result = plan(
            span=timedelta(hours=2),
            ends_ago=CAGG_LAG + timedelta(minutes=5),
            requested=Resolution.MINUTE,
        )
        assert result.source is Source.MINUTE

    def test_a_recent_window_older_than_the_live_table_falls_back(self):
        # telemetry_1m_live only keeps two days, so a five-day window cannot be
        # served from it however recently it ends.
        result = plan(span=timedelta(days=5), requested=Resolution.MINUTE)
        assert result.source is Source.MINUTE


class TestAutomatic:
    def test_a_short_window_uses_raw_samples(self):
        result = plan(span=timedelta(minutes=30))
        assert result.source is Source.RAW
        assert result.resolution is Resolution.RAW

    def test_a_multi_day_window_steps_up_to_minute_rollups(self):
        # 1200 minutes is the point where raw stops fitting inside the target.
        result = plan(span=timedelta(days=3))
        assert result.resolution is Resolution.MINUTE

    def test_a_multi_month_window_steps_up_to_hourly(self):
        result = plan(span=timedelta(days=90))
        assert result.source is Source.HOUR
        assert "hourly" in result.reason

    def test_a_window_older_than_raw_retention_downgrades_rather_than_failing(self):
        # Automatic mode has no obligation to the raw table specifically, so it
        # answers from the rollup instead of refusing.
        result = plan(span=timedelta(hours=2), ends_ago=timedelta(days=45))
        assert result.source is Source.MINUTE

    def test_a_window_older_than_every_retention_is_refused(self):
        with pytest.raises(RetentionError):
            plan(span=timedelta(days=200), ends_ago=timedelta(days=900))

    @pytest.mark.parametrize(
        "span",
        [timedelta(minutes=5), timedelta(hours=1), timedelta(days=1), timedelta(days=30)],
    )
    def test_every_plan_reports_the_column_the_source_actually_uses(self, span):
        result = plan(span=span)
        expected = "ts" if result.source is Source.RAW else "bucket"
        assert result.time_column == expected

    @pytest.mark.parametrize("span", [timedelta(minutes=5), timedelta(days=3), timedelta(days=90)])
    def test_every_plan_explains_itself(self, span):
        assert plan(span=span).reason


class TestGuards:
    def test_an_inverted_window_is_an_error(self):
        with pytest.raises(ValueError, match="end must be after start"):
            choose(start=NOW, end=NOW - timedelta(hours=1), now=NOW)

    def test_a_zero_length_window_is_an_error(self):
        with pytest.raises(ValueError):
            choose(start=NOW, end=NOW, now=NOW)

    def test_aggregated_sources_are_marked_as_aggregated(self):
        assert plan(span=timedelta(minutes=10)).aggregated is False
        assert plan(span=timedelta(days=90)).aggregated is True

    def test_retention_table_covers_every_source(self):
        # A source missing from the table would be treated as having no
        # retention at all, and would happily be chosen for a window that no
        # longer exists.
        assert set(RETENTION) == set(Source)
