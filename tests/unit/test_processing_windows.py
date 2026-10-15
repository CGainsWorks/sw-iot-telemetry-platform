"""Tumbling-window rollup maths.

The aggregate is the one place in the platform doing arithmetic that nothing
downstream can check. If the mean is wrong, every chart and every threshold rule
is wrong with it and nothing complains.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from processing.windows import Aggregate, WindowAggregator, floor_to_window

MINUTE = timedelta(minutes=1)
T0 = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)


def make_aggregate(**overrides) -> Aggregate:
    base = {
        "device_id": "esp32-000001",
        "site_id": "site-a",
        "metric": "temperature_c",
        "bucket": T0,
    }
    base.update(overrides)
    return Aggregate(**base)


class TestFloorToWindow:
    def test_rounds_down_to_the_minute(self):
        assert floor_to_window(T0 + timedelta(seconds=59, microseconds=999)) == T0

    def test_a_bucket_boundary_maps_to_itself(self):
        assert floor_to_window(T0) == T0

    def test_uses_the_epoch_as_origin_so_every_process_agrees(self):
        # Two processes started at different times must produce the same bucket
        # for the same sample, or the upsert key stops matching.
        odd = datetime(2026, 8, 22, 10, 37, 42, tzinfo=UTC)
        assert floor_to_window(odd) == datetime(2026, 8, 22, 10, 37, tzinfo=UTC)

    def test_works_for_other_window_sizes(self):
        assert floor_to_window(
            datetime(2026, 8, 22, 10, 37, tzinfo=UTC), timedelta(hours=1)
        ) == datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


class TestAggregate:
    def test_statistics_over_a_known_sample(self):
        agg = make_aggregate()
        for index, value in enumerate([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]):
            agg.add(value, T0 + timedelta(seconds=index))

        assert agg.count == 8
        assert agg.mean == pytest.approx(5.0)
        assert agg.minimum == 2.0
        assert agg.maximum == 9.0
        # Sample standard deviation of that set is exactly sqrt(32/7).
        assert agg.stddev == pytest.approx(math.sqrt(32.0 / 7.0))

    def test_stddev_of_identical_samples_is_zero_not_a_tiny_negative_root(self):
        # Floating point can push the variance slightly below zero when every
        # sample is the same, and sqrt of that is a crash.
        agg = make_aggregate()
        for index in range(50):
            agg.add(21.4, T0 + timedelta(seconds=index))
        assert agg.stddev == 0.0

    def test_a_single_sample_has_no_sample_stddev(self):
        agg = make_aggregate()
        agg.add(21.0, T0)
        assert agg.stddev == 0.0

    def test_last_value_follows_device_time_not_arrival_order(self):
        # Records can be reordered between the broker and here. last_value must
        # mean the newest sample, not the most recently processed one.
        agg = make_aggregate()
        agg.add(1.0, T0 + timedelta(seconds=30))
        agg.add(2.0, T0 + timedelta(seconds=10))
        assert agg.last_value == 1.0

    def test_merge_combines_two_partials_exactly(self):
        left = make_aggregate()
        right = make_aggregate()
        whole = make_aggregate()

        for index, value in enumerate([1.0, 2.0, 3.0]):
            left.add(value, T0 + timedelta(seconds=index))
            whole.add(value, T0 + timedelta(seconds=index))
        for index, value in enumerate([4.0, 5.0, 6.0], start=3):
            right.add(value, T0 + timedelta(seconds=index))
            whole.add(value, T0 + timedelta(seconds=index))

        left.merge(right)
        assert left.count == whole.count
        assert left.mean == pytest.approx(whole.mean)
        assert left.stddev == pytest.approx(whole.stddev)
        assert left.minimum == whole.minimum
        assert left.maximum == whole.maximum
        assert left.last_value == whole.last_value

    def test_merging_an_empty_aggregate_changes_nothing(self):
        agg = make_aggregate()
        agg.add(5.0, T0)
        agg.merge(make_aggregate())
        assert agg.count == 1
        assert agg.mean == 5.0

    def test_empty_aggregate_reports_zero_mean_rather_than_dividing_by_zero(self):
        assert make_aggregate().mean == 0.0


class TestWindowAggregator:
    def test_samples_are_grouped_by_device_metric_and_bucket(self):
        agg = WindowAggregator()
        agg.add(device_id="a", site_id="s", metric="m", value=1.0, ts=T0)
        agg.add(device_id="a", site_id="s", metric="m", value=2.0, ts=T0 + timedelta(seconds=30))
        agg.add(device_id="b", site_id="s", metric="m", value=3.0, ts=T0)
        agg.add(device_id="a", site_id="s", metric="other", value=4.0, ts=T0)

        assert agg.open_windows == 3

    def test_a_window_only_closes_after_the_lateness_allowance(self):
        agg = WindowAggregator(lateness=timedelta(seconds=15))
        agg.add(device_id="a", site_id="s", metric="m", value=1.0, ts=T0)

        # The window ends at T0 + 1m; the allowance runs to T0 + 1m15s.
        assert agg.ready(T0 + timedelta(minutes=1, seconds=10)) == []
        ready = agg.ready(T0 + timedelta(minutes=1, seconds=20))
        assert len(ready) == 1
        assert ready[0].bucket == T0

    def test_closing_a_window_removes_it(self):
        agg = WindowAggregator(lateness=timedelta(seconds=0))
        agg.add(device_id="a", site_id="s", metric="m", value=1.0, ts=T0)
        agg.ready(T0 + timedelta(minutes=2))
        assert agg.open_windows == 0
        assert agg.ready(T0 + timedelta(minutes=3)) == []

    def test_snapshot_does_not_close_anything(self):
        # The partial upsert is what makes a bucket visible while it fills, so
        # it must not consume the window.
        agg = WindowAggregator()
        agg.add(device_id="a", site_id="s", metric="m", value=1.0, ts=T0)
        assert len(agg.snapshot()) == 1
        assert agg.open_windows == 1

    def test_drain_closes_everything(self):
        agg = WindowAggregator()
        for index in range(5):
            agg.add(device_id=f"d{index}", site_id="s", metric="m", value=1.0, ts=T0)
        assert len(agg.drain()) == 5
        assert agg.open_windows == 0

    def test_late_samples_are_counted_not_silently_folded_in(self):
        # Folding a late sample into a window that has already been written
        # would make the live rollup go backwards.
        agg = WindowAggregator(lateness=timedelta(seconds=15))
        now = T0 + timedelta(minutes=5)
        assert agg.note_late(T0, now) is True
        assert agg.late_samples == 1

    def test_an_on_time_sample_is_not_counted_as_late(self):
        agg = WindowAggregator(lateness=timedelta(seconds=15))
        now = T0 + timedelta(seconds=30)
        assert agg.note_late(T0 + timedelta(seconds=20), now) is False
        assert agg.late_samples == 0
