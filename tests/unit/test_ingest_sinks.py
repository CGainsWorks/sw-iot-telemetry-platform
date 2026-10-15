"""Stream sinks.

The Kinesis sink is the AWS half of ingest and the local stack never exercises
it, so it is covered here against a stubbed client. The behaviour worth pinning
down is the one that is easy to get wrong: Kinesis reports a partial batch
failure in the response body rather than by raising, so a caller that only
catches exceptions loses records and never knows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ingest.sinks import KinesisSink
from telemetry_common.models import (
    DeadLetter,
    LifecycleEvent,
    StatusMessage,
    TelemetryMessage,
    explode,
)


class FakeKinesis:
    """Records calls and returns whatever failure count it is told to."""

    def __init__(self, failed_per_call: list[int] | None = None) -> None:
        self.calls: list[dict] = []
        self._failures = list(failed_per_call or [])

    def put_records(self, *, StreamName: str, Records: list[dict]) -> dict:
        self.calls.append({"stream": StreamName, "records": Records})
        failed = self._failures.pop(0) if self._failures else 0
        return {"FailedRecordCount": failed, "Records": []}


def make_readings(count: int = 3):
    message = TelemetryMessage(
        device_id="esp32-000001",
        site_id="site-a",
        seq=1,
        ts=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
        fw_version="1.4.2",
        uptime_s=10,
        readings={f"metric_{i}": float(i) for i in range(count)},
    )
    return explode(message)


async def make_sink(client: FakeKinesis) -> KinesisSink:
    sink = KinesisSink(
        stream_name="telemetry-normalised", dlq_stream_name="telemetry-deadletter", client=client
    )
    await sink.start()
    return sink


class TestKinesisSink:
    async def test_readings_are_partitioned_by_device(self):
        # Partition key decides the shard, and a device's readings must stay in
        # order, so they all have to land on the same one.
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_readings(make_readings(3))

        records = client.calls[0]["records"]
        assert len(records) == 3
        assert {record["PartitionKey"] for record in records} == {"esp32-000001"}

    async def test_records_carry_the_serialised_reading(self):
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_readings(make_readings(1))

        payload = json.loads(client.calls[0]["records"][0]["Data"])
        assert payload["device_id"] == "esp32-000001"
        assert payload["metric"] == "metric_0"

    async def test_a_large_batch_is_split_to_the_api_limit(self):
        # PutRecords caps at 500 records per call and rejects anything larger.
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_readings(make_readings(1200))

        assert [len(call["records"]) for call in client.calls] == [500, 500, 200]

    async def test_a_partial_failure_raises_rather_than_passing_silently(self):
        # Kinesis returns FailedRecordCount in a 200 response. Ignoring it is
        # how records go missing without a single error in the logs.
        client = FakeKinesis(failed_per_call=[7])
        sink = await make_sink(client)

        with pytest.raises(RuntimeError, match="7 of 3 records failed"):
            await sink.send_readings(make_readings(3))

    async def test_a_failure_in_a_later_chunk_still_raises(self):
        client = FakeKinesis(failed_per_call=[0, 3])
        sink = await make_sink(client)

        with pytest.raises(RuntimeError):
            await sink.send_readings(make_readings(600))

    async def test_dead_letters_go_to_their_own_stream(self):
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_dead_letter(
            DeadLetter(
                reason="identity_mismatch",
                detail="payload claimed another device",
                topic="dt/acme/site-a/esp32-000001/telemetry",
                received_at=datetime.now(UTC),
                raw="{}",
            )
        )

        assert client.calls[0]["stream"] == "telemetry-deadletter"
        assert client.calls[0]["records"][0]["PartitionKey"] == "identity_mismatch"

    async def test_status_messages_go_to_the_telemetry_stream(self):
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_status(
            StatusMessage(
                device_id="esp32-000001",
                site_id="site-a",
                ts=datetime.now(UTC),
                event=LifecycleEvent.ONLINE,
                fw_version="1.4.2",
            )
        )

        assert client.calls[0]["stream"] == "telemetry-normalised"

    async def test_using_the_sink_before_start_is_an_error(self):
        sink = KinesisSink(stream_name="a", dlq_stream_name="b", client=None)
        with pytest.raises(RuntimeError, match="start"):
            await sink.send_readings(make_readings(1))

    async def test_an_empty_batch_makes_no_api_call(self):
        client = FakeKinesis()
        sink = await make_sink(client)

        await sink.send_readings([])

        assert client.calls == []
