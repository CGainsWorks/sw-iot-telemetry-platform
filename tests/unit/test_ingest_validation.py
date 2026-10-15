"""The ingest accept-or-reject decision.

This is the security boundary of the platform, so the identity tests here carry
the most weight: they are what stops one compromised device writing telemetry
attributed to its neighbours.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from ingest.validation import (
    MAX_CLOCK_AHEAD,
    Accepted,
    AcceptedStatus,
    Rejected,
    RejectReason,
    handle,
)
from telemetry_common.topics import status_topic, telemetry_topic

DEVICE = "esp32-000001"
SITE = "site-a"
NOW = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


def payload(**overrides) -> str:
    base = {
        "schema_version": "1.1.0",
        "device_id": DEVICE,
        "site_id": SITE,
        "seq": 1,
        "ts": NOW.isoformat(),
        "fw_version": "1.4.2",
        "uptime_s": 100,
        "status": "ok",
        "readings": {"temperature_c": 21.0, "humidity_pct": 45.0},
    }
    base.update(overrides)
    return json.dumps(base)


def topic(device: str = DEVICE, site: str = SITE) -> str:
    return telemetry_topic(device, site)


def handle_now(topic_str: str, body: str):
    return handle(topic_str, body, received_at=NOW)


class TestHappyPath:
    def test_a_good_message_is_accepted_and_exploded(self):
        result = handle_now(topic(), payload())
        assert isinstance(result, Accepted)
        assert len(result.readings) == 2
        assert result.parts.device_id == DEVICE

    def test_accepts_bytes_as_well_as_str(self):
        assert isinstance(handle_now(topic(), payload().encode()), Accepted)

    def test_ingest_timestamp_is_stamped_onto_every_row(self):
        result = handle_now(topic(), payload())
        assert isinstance(result, Accepted)
        assert all(row.ingest_ts == NOW for row in result.readings)

    def test_older_minor_version_is_accepted(self):
        # An air-quality node still on 1.3.0 is a normal part of any fleet.
        result = handle_now(topic(), payload(schema_version="1.3.0"))
        assert isinstance(result, Accepted)

    def test_newer_minor_is_accepted_and_flagged(self):
        result = handle_now(topic(), payload(schema_version="1.99.0"))
        assert isinstance(result, Accepted)
        assert result.schema_ahead is True

    def test_status_message_is_accepted_on_the_status_topic(self):
        body = json.dumps(
            {
                "schema_version": "1.1.0",
                "device_id": DEVICE,
                "site_id": SITE,
                "ts": NOW.isoformat(),
                "event": "online",
                "fw_version": "1.4.2",
            }
        )
        assert isinstance(handle_now(status_topic(DEVICE, SITE), body), AcceptedStatus)


class TestIdentityBinding:
    """The topic is authenticated. The payload is a claim. They must agree."""

    def test_payload_claiming_another_device_is_rejected(self):
        result = handle_now(topic(DEVICE), payload(device_id="esp32-000002"))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.IDENTITY_MISMATCH
        # Both identities appear in the detail, so the dead letter names the
        # attacker and the victim.
        assert "esp32-000002" in result.detail
        assert DEVICE in result.detail

    def test_payload_claiming_another_site_is_rejected(self):
        result = handle_now(topic(DEVICE, "site-a"), payload(site_id="site-b"))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.SITE_MISMATCH

    def test_the_rejected_payload_is_kept_for_investigation(self):
        result = handle_now(topic(), payload(device_id="esp32-000002"))
        assert isinstance(result, Rejected)
        assert "esp32-000002" in result.dead_letter.raw
        assert result.dead_letter.topic == topic()


class TestSchemaVersion:
    def test_unsupported_major_is_rejected_before_validation(self):
        # A 9.0.0 payload may parse perfectly and still mean something entirely
        # different. It must never reach the model.
        result = handle_now(topic(), payload(schema_version="9.0.0"))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.SCHEMA_UNSUPPORTED

    @pytest.mark.parametrize("version", ["", "abc", "1.2", None, 1.1])
    def test_malformed_version_is_rejected(self, version):
        result = handle_now(topic(), payload(schema_version=version))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.SCHEMA_UNSUPPORTED

    def test_missing_version_is_rejected(self):
        body = json.loads(payload())
        del body["schema_version"]
        result = handle_now(topic(), json.dumps(body))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.SCHEMA_UNSUPPORTED


class TestMalformedInput:
    def test_invalid_json_is_rejected(self):
        result = handle_now(topic(), "{not json")
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.MALFORMED_JSON

    @pytest.mark.parametrize("body", ["[1, 2, 3]", '"a string"', "42", "null"])
    def test_non_object_json_is_rejected(self, body):
        result = handle_now(topic(), body)
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.MALFORMED_JSON

    def test_malformed_topic_is_rejected_before_the_payload_is_read(self):
        result = handle_now("garbage", payload())
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.MALFORMED_TOPIC

    def test_command_topics_are_not_consumed_by_ingest(self):
        result = handle_now("cmd/acme/site-a/esp32-000001/req", payload())
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.MALFORMED_TOPIC

    def test_structurally_invalid_payload_is_rejected_with_the_field_named(self):
        result = handle_now(topic(), payload(seq=-5))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.SCHEMA_INVALID
        assert "seq" in result.detail

    def test_undecodable_bytes_do_not_raise(self):
        result = handle_now(topic(), b"\xff\xfe\x00garbage")
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.MALFORMED_JSON


class TestPlausibility:
    def test_out_of_range_reading_is_rejected(self):
        result = handle_now(topic(), payload(readings={"temperature_c": 999.0}))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.IMPLAUSIBLE_READING
        assert "temperature_c" in result.detail

    def test_plausibility_can_be_turned_off(self):
        # Some deployments would rather store a stuck sensor's nonsense than
        # lose the fact that it reported at all.
        result = handle(
            topic(),
            payload(readings={"temperature_c": 999.0}),
            received_at=NOW,
            reject_implausible=False,
        )
        assert isinstance(result, Accepted)

    def test_unknown_metrics_are_not_range_checked(self):
        result = handle_now(topic(), payload(readings={"a_new_sensor": 1e6}))
        assert isinstance(result, Accepted)


class TestClockHandling:
    def test_a_timestamp_far_in_the_future_is_rejected(self):
        future = (NOW + MAX_CLOCK_AHEAD + timedelta(minutes=1)).isoformat()
        result = handle_now(topic(), payload(ts=future))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.TIMESTAMP_OUT_OF_RANGE

    def test_small_forward_skew_is_tolerated(self):
        # Hardware with no RTC is routinely a minute or two out. Rejecting that
        # would throw away most of a real fleet's data.
        near = (NOW + timedelta(minutes=2)).isoformat()
        assert isinstance(handle_now(topic(), payload(ts=near)), Accepted)

    def test_a_buffered_backlog_from_yesterday_is_accepted(self):
        # A device that buffered through an outage is doing the right thing.
        yesterday = (NOW - timedelta(days=1)).isoformat()
        assert isinstance(handle_now(topic(), payload(ts=yesterday)), Accepted)

    def test_an_absurdly_old_timestamp_is_rejected(self):
        ancient = (NOW - timedelta(days=30)).isoformat()
        result = handle_now(topic(), payload(ts=ancient))
        assert isinstance(result, Rejected)
        assert result.reason is RejectReason.TIMESTAMP_OUT_OF_RANGE


class TestDeadLetterEnvelope:
    def test_oversized_payloads_are_truncated(self):
        # A dead-letter table is not a place to store megabytes of a
        # misconfigured device's output.
        huge = payload(readings={"temperature_c": 999.0}, fw_version="1.4.2")
        body = json.loads(huge)
        body["padding"] = "x" * 20_000
        result = handle_now(topic(), json.dumps(body))
        assert isinstance(result, Rejected)
        assert len(result.dead_letter.raw) <= 8192

    def test_every_reject_reason_is_recorded_verbatim(self):
        result = handle_now(topic(), "{not json")
        assert isinstance(result, Rejected)
        assert result.dead_letter.reason == RejectReason.MALFORMED_JSON.value
        assert result.dead_letter.received_at == NOW
