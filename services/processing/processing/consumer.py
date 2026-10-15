"""The stream processor.

Consumes the three topics ingest produces, writes raw readings to TimescaleDB,
maintains the live one-minute rollups and keeps the device registry current.

Offsets are committed manually, after the batch has been written. That ordering
is the whole delivery guarantee: a crash between write and commit replays a
batch that is already stored, and the unique index on ``telemetry`` swallows it.
A crash the other way round, with auto-commit, would lose it.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiokafka import AIOKafkaConsumer
from prometheus_client import Counter, Gauge, Histogram

from telemetry_common.logging import get_logger
from telemetry_common.models import DeadLetter, NormalisedReading, StatusMessage

from .windows import WindowAggregator
from .writer import TelemetryWriter

log = get_logger(__name__)

CONSUMED = Counter("processing_messages_total", "Messages consumed", ["topic", "outcome"])
ROWS_WRITTEN = Counter("processing_rows_written_total", "Rows written", ["table"])
BATCH_SIZE = Histogram(
    "processing_batch_rows",
    "Rows per write batch",
    buckets=(1, 10, 50, 100, 500, 1000, 5000, 10000),
)
WRITE_TIME = Histogram(
    "processing_write_seconds",
    "Time to write one batch",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
)
END_TO_END_LAG = Histogram(
    "processing_end_to_end_lag_seconds",
    "Device sample time to durable storage",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)
OPEN_WINDOWS = Gauge("processing_open_windows", "Rollup windows currently held in memory")
LATE_SAMPLES = Counter("processing_late_samples_total", "Samples that missed their live window")


@dataclass
class ProcessorConfig:
    bootstrap_servers: str = "redpanda:9092"
    telemetry_topic: str = "telemetry.normalised"
    status_topic: str = "device.status"
    dlq_topic: str = "telemetry.deadletter"
    consumer_group: str = "processing"
    #: Upper bound on rows held before a write. Larger amortises the round trip;
    #: too large and a restart replays more work than it saves.
    max_batch: int = 2000
    #: Milliseconds to wait for a batch to fill before writing what is there.
    #: This is the dominant term in end-to-end latency at low message rates.
    batch_timeout_ms: int = 250
    #: How often partially filled rollup windows are pushed to the live table.
    rollup_flush_interval_s: float = 5.0
    auto_offset_reset: str = "earliest"


@dataclass
class ProcessorStats:
    readings: int = 0
    status: int = 0
    dead_letters: int = 0
    malformed: int = 0
    batches: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, float | int]:
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        return {
            "readings": self.readings,
            "status": self.status,
            "dead_letters": self.dead_letters,
            "malformed": self.malformed,
            "batches": self.batches,
            "elapsed_s": round(elapsed, 2),
            "rows_per_s": round(self.readings / elapsed, 2),
        }


class Processor:
    def __init__(self, config: ProcessorConfig, writer: TelemetryWriter) -> None:
        self.config = config
        self.writer = writer
        self.stats = ProcessorStats()
        self.aggregator = WindowAggregator()
        self._stop = asyncio.Event()
        self._consumer: AIOKafkaConsumer | None = None
        self._last_rollup_flush = time.monotonic()

    def request_stop(self) -> None:
        self._stop.set()

    # -- decoding ----------------------------------------------------------

    def _decode(self, topic: str, raw: bytes) -> object | None:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Only reachable if something other than ingest writes to these
            # topics. Counted rather than raised so one bad record cannot wedge
            # the consumer group on a poison message.
            self.stats.malformed += 1
            CONSUMED.labels(topic=topic, outcome="malformed").inc()
            log.error("undecodable record", topic=topic, error=str(exc))
            return None

        try:
            if topic == self.config.telemetry_topic:
                return NormalisedReading.model_validate(document)
            if topic == self.config.status_topic:
                return StatusMessage.model_validate(document)
            if topic == self.config.dlq_topic:
                return DeadLetter.model_validate(document)
        except Exception as exc:
            self.stats.malformed += 1
            CONSUMED.labels(topic=topic, outcome="malformed").inc()
            log.error("unparseable record", topic=topic, error=str(exc))
            return None

        log.warning("record from an unexpected topic", topic=topic)
        return None

    # -- write path --------------------------------------------------------

    async def _write_batch(
        self,
        readings: list[NormalisedReading],
        status: list[StatusMessage],
        dead_letters: list[DeadLetter],
    ) -> None:
        started = time.perf_counter()

        if readings:
            written = await self.writer.write_readings(readings)
            await self.writer.upsert_devices_from_readings(readings)
            self.stats.readings += written
            ROWS_WRITTEN.labels(table="telemetry").inc(written)
            BATCH_SIZE.observe(written)

            now = datetime.now(UTC)
            for reading in readings:
                # A sample whose window has already been written out cannot be
                # folded into the live rollup without making it go backwards.
                # It is counted and skipped here; the continuous aggregate still
                # picks it up from the raw row, which is why the live path is
                # allowed to be approximate.
                if self.aggregator.note_late(reading.ts, now):
                    LATE_SAMPLES.inc()
                else:
                    self.aggregator.add(
                        device_id=reading.device_id,
                        site_id=reading.site_id,
                        metric=reading.metric,
                        value=reading.value,
                        ts=reading.ts,
                    )
                END_TO_END_LAG.observe(max((now - reading.ts).total_seconds(), 0.0))

        if status:
            written = await self.writer.write_status(status)
            self.stats.status += written
            ROWS_WRITTEN.labels(table="device_events").inc(written)

        if dead_letters:
            written = await self.writer.write_dead_letters(dead_letters)
            self.stats.dead_letters += written
            ROWS_WRITTEN.labels(table="dead_letters").inc(written)

        WRITE_TIME.observe(time.perf_counter() - started)
        self.stats.batches += 1

    async def _flush_rollups(self, *, final: bool = False) -> None:
        """Push rollup windows to the live table.

        Closed windows are written and dropped. Open ones are written too, so a
        bucket is visible while it is still filling, and rewritten on the next
        flush with a fuller count.
        """
        now = datetime.now(UTC)
        closed = self.aggregator.drain() if final else self.aggregator.ready(now)
        pending = closed + ([] if final else self.aggregator.snapshot())

        if pending:
            await self.writer.upsert_live_rollups(pending, updated_at=now)
            ROWS_WRITTEN.labels(table="telemetry_1m_live").inc(len(pending))

        OPEN_WINDOWS.set(self.aggregator.open_windows)
        self._last_rollup_flush = time.monotonic()

    # -- main loop ---------------------------------------------------------

    async def run(self) -> ProcessorStats:
        cfg = self.config
        self._consumer = AIOKafkaConsumer(
            cfg.telemetry_topic,
            cfg.status_topic,
            cfg.dlq_topic,
            bootstrap_servers=cfg.bootstrap_servers,
            group_id=cfg.consumer_group,
            enable_auto_commit=False,
            auto_offset_reset=cfg.auto_offset_reset,
            max_poll_records=cfg.max_batch,
        )
        await self._consumer.start()
        log.info(
            "processor started",
            bootstrap=cfg.bootstrap_servers,
            group=cfg.consumer_group,
            topics=[cfg.telemetry_topic, cfg.status_topic, cfg.dlq_topic],
        )

        try:
            while not self._stop.is_set():
                batches = await self._consumer.getmany(
                    timeout_ms=cfg.batch_timeout_ms, max_records=cfg.max_batch
                )

                readings: list[NormalisedReading] = []
                status: list[StatusMessage] = []
                dead_letters: list[DeadLetter] = []

                for partition, records in batches.items():
                    for record in records:
                        decoded = self._decode(partition.topic, record.value)
                        if decoded is None:
                            continue
                        CONSUMED.labels(topic=partition.topic, outcome="ok").inc()
                        if isinstance(decoded, NormalisedReading):
                            readings.append(decoded)
                        elif isinstance(decoded, StatusMessage):
                            status.append(decoded)
                        elif isinstance(decoded, DeadLetter):
                            dead_letters.append(decoded)

                if readings or status or dead_letters:
                    await self._write_batch(readings, status, dead_letters)
                    # Commit only now. Everything in this batch is durable.
                    await self._consumer.commit()

                if time.monotonic() - self._last_rollup_flush >= cfg.rollup_flush_interval_s:
                    await self._flush_rollups()

        finally:
            # Windows still held in memory are written before the process exits,
            # otherwise the last minute of the live view disappears on every
            # deploy.
            try:
                await self._flush_rollups(final=True)
            except Exception as exc:
                log.error("final rollup flush failed", error=str(exc))
            await self._consumer.stop()
            self._consumer = None
            log.info("processor stopped", **self.stats.snapshot())

        return self.stats


async def run_processor(config: ProcessorConfig, writer: TelemetryWriter) -> ProcessorStats:
    processor = Processor(config, writer)
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, processor.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: processor.request_stop())

    await writer.start()
    try:
        return await processor.run()
    finally:
        await writer.stop()
