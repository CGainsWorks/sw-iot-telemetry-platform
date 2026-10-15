"""Where accepted messages go next.

Two implementations behind one interface. Locally the stream is Redpanda over
the Kafka protocol; on AWS the IoT Core rule writes to Kinesis. Keeping the
sink behind a protocol means the validation and enrichment code above it is
identical in both deployments, and the choice is a configuration flag rather
than a second codebase.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from telemetry_common.logging import get_logger
from telemetry_common.models import DeadLetter, NormalisedReading, StatusMessage

log = get_logger(__name__)


class StreamSink(Protocol):
    """Anything that can accept the three kinds of output ingest produces."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send_readings(self, readings: Sequence[NormalisedReading]) -> None: ...

    async def send_status(self, status: StatusMessage) -> None: ...

    async def send_dead_letter(self, dead_letter: DeadLetter) -> None: ...


class KafkaSink:
    """Produce to Redpanda or Kafka.

    Keyed by device id so every reading from one device lands on one partition
    and therefore stays in order. Ordering per device is what lets the rollup
    consumer treat a sequence gap as a real gap rather than as reordering.
    """

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        telemetry_topic: str,
        status_topic: str,
        dlq_topic: str,
        linger_ms: int = 20,
        compression: str = "lz4",
        acks: str | int = "all",
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._telemetry_topic = telemetry_topic
        self._status_topic = status_topic
        self._dlq_topic = dlq_topic
        self._linger_ms = linger_ms
        self._compression = compression
        self._acks = acks
        # Any rather than AIOKafkaProducer: the client is imported lazily inside
        # start(), so naming the type here would force aiokafka on every importer
        # of this module. The None guard in each send method is the real check.
        self._producer: Any | None = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            linger_ms=self._linger_ms,
            compression_type=self._compression,
            # acks=all costs latency and buys durability. Telemetry that has been
            # acknowledged to a device must not vanish because one broker died,
            # and the benchmark measures what that choice costs.
            acks=self._acks,
            enable_idempotence=True,
            value_serializer=lambda value: json.dumps(value).encode(),
            key_serializer=lambda key: key.encode() if key else None,
        )
        await self._producer.start()
        log.info("kafka sink started", bootstrap=self._bootstrap)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None
            log.info("kafka sink stopped")

    async def send_readings(self, readings: Sequence[NormalisedReading]) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaSink.start() was not awaited")
        for reading in readings:
            await self._producer.send(
                self._telemetry_topic,
                value=reading.model_dump(mode="json"),
                key=reading.device_id,
            )

    async def send_status(self, status: StatusMessage) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaSink.start() was not awaited")
        await self._producer.send(
            self._status_topic,
            value=status.model_dump(mode="json"),
            key=status.device_id,
        )

    async def send_dead_letter(self, dead_letter: DeadLetter) -> None:
        if self._producer is None:
            raise RuntimeError("KafkaSink.start() was not awaited")
        await self._producer.send(self._dlq_topic, value=dead_letter.model_dump(mode="json"))


class KinesisSink:
    """Produce to Amazon Kinesis Data Streams.

    Used when the platform runs on AWS and the broker is IoT Core. Not exercised
    by the local stack, so it is covered by unit tests against a stubbed client
    rather than by the integration test. See docs/architecture.md for which path
    runs where.
    """

    #: Kinesis caps a PutRecords call at 500 records.
    MAX_BATCH = 500

    def __init__(
        self,
        *,
        stream_name: str,
        dlq_stream_name: str,
        client=None,
        region: str | None = None,
    ) -> None:
        self._stream = stream_name
        self._dlq_stream = dlq_stream_name
        self._client: Any | None = client
        self._region = region

    async def start(self) -> None:
        if self._client is None:
            import boto3

            self._client = boto3.client("kinesis", region_name=self._region)
        log.info("kinesis sink started", stream=self._stream)

    async def stop(self) -> None:
        self._client = None

    def _put_records(self, stream: str, records: list[dict]) -> None:
        if self._client is None:
            raise RuntimeError("KinesisSink.start() was not awaited")
        for start in range(0, len(records), self.MAX_BATCH):
            chunk = records[start : start + self.MAX_BATCH]
            response = self._client.put_records(StreamName=stream, Records=chunk)
            failed = response.get("FailedRecordCount", 0)
            if failed:
                # Kinesis reports partial failure in the response body rather
                # than by raising, so a caller that only catches exceptions
                # loses records silently.
                log.error("kinesis partial failure", stream=stream, failed=failed)
                raise RuntimeError(f"{failed} of {len(chunk)} records failed to {stream}")

    async def send_readings(self, readings: Sequence[NormalisedReading]) -> None:
        self._put_records(
            self._stream,
            [
                {
                    "Data": json.dumps(reading.model_dump(mode="json")).encode(),
                    "PartitionKey": reading.device_id,
                }
                for reading in readings
            ],
        )

    async def send_status(self, status: StatusMessage) -> None:
        self._put_records(
            self._stream,
            [
                {
                    "Data": json.dumps(status.model_dump(mode="json")).encode(),
                    "PartitionKey": status.device_id,
                }
            ],
        )

    async def send_dead_letter(self, dead_letter: DeadLetter) -> None:
        self._put_records(
            self._dlq_stream,
            [
                {
                    "Data": json.dumps(dead_letter.model_dump(mode="json")).encode(),
                    "PartitionKey": dead_letter.reason,
                }
            ],
        )
