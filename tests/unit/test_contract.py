"""The message contract: models, versioning and the topic grammar.

These tests are the ones that matter most in this repository. Everything else
can be fixed by redeploying a service; a mistake in the contract is baked into
firmware that is already in the field.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from telemetry_common.metrics import CATALOGUE, is_plausible, unit_for
from telemetry_common.models import (
    DeviceStatus,
    LifecycleEvent,
    StatusMessage,
    TelemetryMessage,
    explode,
)
from telemetry_common.topics import (
    TopicError,
    TopicKind,
    parse,
    status_topic,
    subscription_all_telemetry,
    telemetry_topic,
)
from telemetry_common.versioning import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    Version,
    is_newer_minor,
    is_supported,
    require_supported,
)


def make_message(**overrides) -> TelemetryMessage:
    base = {
        "device_id": "esp32-000001",
        "site_id": "site-a",
        "seq": 42,
        "ts": datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
        "fw_version": "1.4.2",
        "uptime_s": 3600,
        "readings": {"temperature_c": 21.5, "humidity_pct": 48.0},
    }
    base.update(overrides)
    return TelemetryMessage(**base)


class TestVersioning:
    def test_current_version_is_supported(self):
        assert is_supported(CURRENT_SCHEMA_VERSION)

    @pytest.mark.parametrize("version", ["1.0.0", "1.5.0", "1.99.3", "1.0.7"])
    def test_any_minor_of_a_supported_major_is_accepted(self, version):
        # The whole point of the policy: minor bumps are additive, so a consumer
        # built for 1.1 must accept 1.0 and 1.99 alike.
        assert is_supported(version)

    @pytest.mark.parametrize("version", ["2.0.0", "0.9.0", "9.0.0"])
    def test_other_majors_are_refused(self, version):
        assert not is_supported(version)
        with pytest.raises(SchemaVersionError):
            require_supported(version)

    @pytest.mark.parametrize("version", ["1.2", "v1.2.3", "", "abc", "1.2.3.4", None])
    def test_malformed_versions_are_refused_rather_than_guessed(self, version):
        assert not is_supported(version)

    def test_newer_minor_is_flagged_but_still_supported(self):
        newer = f"{Version.parse(CURRENT_SCHEMA_VERSION).major}.99.0"
        assert is_supported(newer)
        assert is_newer_minor(newer)

    def test_older_minor_is_not_flagged_as_newer(self):
        assert not is_newer_minor("1.0.0")

    def test_versions_order_naturally(self):
        assert Version.parse("1.2.0") < Version.parse("1.10.0")
        assert Version.parse("1.0.0") < Version.parse("2.0.0")


class TestTelemetryMessage:
    def test_round_trips_through_json(self):
        message = make_message()
        restored = TelemetryMessage.model_validate(json.loads(message.model_dump_json()))
        assert restored == message

    def test_naive_timestamp_is_read_as_utc(self):
        # Hardware without a timezone database sends naive stamps. Rejecting
        # them would drop real data over a formatting detail.
        message = make_message(ts=datetime(2026, 8, 22, 10, 0))
        assert message.ts.tzinfo is UTC

    def test_non_utc_timestamp_is_converted(self):
        offset = timezone(timedelta(hours=5))
        message = make_message(ts=datetime(2026, 8, 22, 15, 0, tzinfo=offset))
        assert message.ts == datetime(2026, 8, 22, 10, 0, tzinfo=UTC)

    def test_unknown_fields_are_dropped_not_rejected(self):
        # This is the forwards-compatibility rule in action: a payload from a
        # newer minor carries fields this build has never heard of.
        payload = make_message().model_dump(mode="json")
        payload["a_field_from_the_future"] = {"nested": True}
        message = TelemetryMessage.model_validate(payload)
        assert not hasattr(message, "a_field_from_the_future")
        assert message.readings["temperature_c"] == 21.5

    @pytest.mark.parametrize("device_id", ["ESP32-1", "ab", "with space", "under_score", ""])
    def test_bad_device_ids_are_refused(self, device_id):
        with pytest.raises(ValidationError):
            make_message(device_id=device_id)

    def test_empty_readings_are_refused(self):
        # A message with no readings carries no information and would create a
        # row in no table. Better to reject it at the edge.
        with pytest.raises(ValidationError):
            make_message(readings={})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_readings_are_refused(self, value):
        # NaN survives JSON round trips in some encoders and then poisons every
        # average downstream.
        with pytest.raises(ValidationError):
            make_message(readings={"temperature_c": value})

    def test_negative_sequence_is_refused(self):
        with pytest.raises(ValidationError):
            make_message(seq=-1)

    def test_implausible_metrics_are_reported_not_raised(self):
        # A stuck sensor reporting 900 degC is a real message from a real
        # device. It belongs in the dead-letter topic with a reason, not thrown
        # away by the parser.
        message = make_message(readings={"temperature_c": 900.0, "humidity_pct": 50.0})
        assert message.implausible_metrics() == ["temperature_c"]

    def test_plausible_metrics_report_nothing(self):
        assert make_message().implausible_metrics() == []

    def test_unknown_metrics_are_allowed_through(self):
        # An open readings map is what lets a new sensor ship without a
        # coordinated platform deploy.
        message = make_message(readings={"a_new_sensor": 1.0})
        assert message.implausible_metrics() == []


class TestExplode:
    def test_one_row_per_reading(self):
        rows = explode(make_message())
        assert len(rows) == 2
        assert {row.metric for row in rows} == {"temperature_c", "humidity_pct"}

    def test_envelope_fields_are_carried_onto_every_row(self):
        message = make_message(status=DeviceStatus.DEGRADED)
        for row in explode(message):
            assert row.device_id == message.device_id
            assert row.site_id == message.site_id
            assert row.seq == message.seq
            assert row.fw_version == message.fw_version
            assert row.device_status is DeviceStatus.DEGRADED

    def test_rows_are_ordered_by_metric_for_determinism(self):
        rows = explode(make_message(readings={"z": 1.0, "a": 2.0, "m": 3.0}))
        assert [row.metric for row in rows] == ["a", "m", "z"]

    def test_ingest_lag_is_measured_from_the_device_timestamp(self):
        ts = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)
        ingest = ts + timedelta(milliseconds=250)
        row = explode(make_message(ts=ts), ingest_ts=ingest)[0]
        assert row.ingest_lag_ms == pytest.approx(250.0)

    def test_clock_skew_produces_a_negative_lag_rather_than_an_error(self):
        # A device whose clock runs fast produces a negative lag. Storing it is
        # what makes the skew visible instead of silently clamped away.
        ts = datetime(2026, 8, 22, 10, 0, 30, tzinfo=UTC)
        ingest = datetime(2026, 8, 22, 10, 0, 0, tzinfo=UTC)
        row = explode(make_message(ts=ts), ingest_ts=ingest)[0]
        assert row.ingest_lag_ms == pytest.approx(-30_000.0)


class TestTopics:
    def test_round_trip(self):
        topic = telemetry_topic("esp32-000042", "site-b", "acme")
        parts = parse(topic)
        assert parts.device_id == "esp32-000042"
        assert parts.site_id == "site-b"
        assert parts.tenant == "acme"
        assert parts.kind is TopicKind.TELEMETRY

    def test_status_topic_parses_as_status(self):
        assert parse(status_topic("esp32-000001", "site-a")).kind is TopicKind.STATUS

    @pytest.mark.parametrize(
        "topic",
        [
            "dt/acme/site-a/esp32-000001",
            "dt/acme/site-a/esp32-000001/telemetry/extra",
            "xx/acme/site-a/esp32-000001/telemetry",
            "dt/acme/site-a/esp32-000001/unknown",
            "",
            "dt/acme/site-a/BAD_ID/telemetry",
        ],
    )
    def test_malformed_topics_raise(self, topic):
        with pytest.raises(TopicError):
            parse(topic)

    def test_wildcard_subscription_is_not_a_concrete_topic(self):
        # The subscription is what the bridge subscribes with. It must never be
        # mistaken for a topic a device could publish to.
        with pytest.raises(TopicError):
            parse(subscription_all_telemetry("acme"))

    def test_device_id_is_a_whole_segment(self):
        # The identity check downstream compares the topic segment to the
        # payload claim, so a device id must never be able to span segments.
        with pytest.raises(TopicError):
            telemetry_topic("esp32/000001", "site-a")


class TestStatusMessage:
    def test_lifecycle_round_trip(self):
        message = StatusMessage(
            device_id="esp32-000001",
            site_id="site-a",
            ts=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
            event=LifecycleEvent.ONLINE,
            fw_version="1.4.2",
        )
        restored = StatusMessage.model_validate(json.loads(message.model_dump_json()))
        assert restored.event is LifecycleEvent.ONLINE

    def test_reason_is_length_capped(self):
        with pytest.raises(ValidationError):
            StatusMessage(
                device_id="esp32-000001",
                site_id="site-a",
                ts=datetime.now(UTC),
                event=LifecycleEvent.OFFLINE,
                fw_version="1.4.2",
                reason="x" * 300,
            )


class TestMetricCatalogue:
    def test_every_spec_has_a_sane_range(self):
        for spec in CATALOGUE.values():
            assert spec.minimum < spec.maximum, spec.name
            assert spec.unit
            assert spec.description

    def test_plausibility_uses_the_catalogue_range(self):
        assert is_plausible("humidity_pct", 50.0)
        assert not is_plausible("humidity_pct", 150.0)
        assert not is_plausible("humidity_pct", -1.0)

    def test_unknown_metric_has_no_unit_but_does_not_raise(self):
        assert unit_for("not_a_metric") == "unknown"
