"""All writes into TimescaleDB.

Every statement here is idempotent, because delivery from the stream is at least
once and a consumer restart replays whatever was not committed. Offsets are
committed only after the corresponding write has succeeded, so the failure mode
is duplicate work rather than lost data, and the duplicates are absorbed by the
unique indexes.

Bulk inserts go through ``unnest`` on arrays rather than ``executemany``. One
round trip and one plan for a batch of ten thousand rows instead of ten thousand
of each is the difference between the platform keeping up with the fleet and not.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import asyncpg

from telemetry_common.logging import get_logger
from telemetry_common.models import DeadLetter, NormalisedReading, StatusMessage

from .windows import Aggregate

log = get_logger(__name__)

INSERT_TELEMETRY = """
INSERT INTO telemetry (
    ts, device_id, site_id, metric, value, seq,
    fw_version, schema_version, device_status, ingest_ts
)
SELECT * FROM unnest(
    $1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::float8[], $6::bigint[],
    $7::text[], $8::text[], $9::text[], $10::timestamptz[]
)
ON CONFLICT (device_id, metric, ts) DO NOTHING
"""

UPSERT_LIVE_ROLLUP = """
INSERT INTO telemetry_1m_live (
    bucket, device_id, site_id, metric,
    avg_value, min_value, max_value, last_value, sample_count, sum_squares, updated_at
)
SELECT * FROM unnest(
    $1::timestamptz[], $2::text[], $3::text[], $4::text[],
    $5::float8[], $6::float8[], $7::float8[], $8::float8[], $9::int[], $10::float8[],
    $11::timestamptz[]
)
ON CONFLICT (device_id, metric, bucket) DO UPDATE SET
    avg_value    = EXCLUDED.avg_value,
    min_value    = EXCLUDED.min_value,
    max_value    = EXCLUDED.max_value,
    last_value   = EXCLUDED.last_value,
    sample_count = EXCLUDED.sample_count,
    sum_squares  = EXCLUDED.sum_squares,
    updated_at   = EXCLUDED.updated_at
-- A window is written repeatedly while it fills, and each write carries the
-- totals so far. Guarding on sample_count stops an out-of-order flush from
-- replacing a fuller aggregate with an emptier one.
WHERE telemetry_1m_live.sample_count <= EXCLUDED.sample_count
"""

UPSERT_DEVICE_FROM_TELEMETRY = """
INSERT INTO devices (device_id, site_id, fw_version, schema_version, last_seen, last_status)
SELECT * FROM unnest(
    $1::text[], $2::text[], $3::text[], $4::text[], $5::timestamptz[], $6::text[]
)
ON CONFLICT (device_id) DO UPDATE SET
    site_id        = EXCLUDED.site_id,
    fw_version     = EXCLUDED.fw_version,
    schema_version = EXCLUDED.schema_version,
    last_status    = EXCLUDED.last_status,
    -- Never move last_seen backwards. Replayed history would otherwise make a
    -- live device look stale and raise a false offline alert.
    last_seen      = GREATEST(devices.last_seen, EXCLUDED.last_seen)
WHERE EXCLUDED.last_seen >= COALESCE(devices.last_seen, '-infinity'::timestamptz)
"""

INSERT_DEVICE_EVENT = """
INSERT INTO device_events (ts, device_id, site_id, event, fw_version, reason)
SELECT * FROM unnest(
    $1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[]
)
"""

UPSERT_DEVICE_FROM_STATUS = """
INSERT INTO devices (
    device_id, site_id, fw_version, last_lifecycle_event, last_lifecycle_at
)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (device_id) DO UPDATE SET
    site_id              = EXCLUDED.site_id,
    fw_version           = COALESCE(EXCLUDED.fw_version, devices.fw_version),
    last_lifecycle_event = EXCLUDED.last_lifecycle_event,
    last_lifecycle_at    = EXCLUDED.last_lifecycle_at
WHERE EXCLUDED.last_lifecycle_at >= COALESCE(devices.last_lifecycle_at, '-infinity'::timestamptz)
"""

INSERT_DEAD_LETTER = """
INSERT INTO dead_letters (received_at, reason, detail, topic, raw)
SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[])
"""


class TelemetryWriter:
    """Owns the connection pool and every write path."""

    def __init__(self, dsn: str, *, min_size: int = 2, max_size: int = 10) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            command_timeout=30.0,
        )
        log.info("database pool ready", min_size=self._min_size, max_size=self._max_size)

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            log.info("database pool closed")

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("TelemetryWriter.start() was not awaited")
        return self._pool

    # -- telemetry ---------------------------------------------------------

    async def write_readings(self, readings: Sequence[NormalisedReading]) -> int:
        if not readings:
            return 0

        columns: list[list] = [[] for _ in range(10)]
        for reading in readings:
            columns[0].append(reading.ts)
            columns[1].append(reading.device_id)
            columns[2].append(reading.site_id)
            columns[3].append(reading.metric)
            columns[4].append(reading.value)
            columns[5].append(reading.seq)
            columns[6].append(reading.fw_version)
            columns[7].append(reading.schema_version)
            columns[8].append(reading.device_status.value)
            columns[9].append(reading.ingest_ts)

        async with self.pool.acquire() as conn:
            await conn.execute(INSERT_TELEMETRY, *columns)
        return len(readings)

    async def upsert_devices_from_readings(self, readings: Sequence[NormalisedReading]) -> int:
        """Refresh the registry from the newest reading seen per device."""
        if not readings:
            return 0

        newest: dict[str, NormalisedReading] = {}
        for reading in readings:
            current = newest.get(reading.device_id)
            if current is None or reading.ts > current.ts:
                newest[reading.device_id] = reading

        rows = list(newest.values())
        async with self.pool.acquire() as conn:
            await conn.execute(
                UPSERT_DEVICE_FROM_TELEMETRY,
                [r.device_id for r in rows],
                [r.site_id for r in rows],
                [r.fw_version for r in rows],
                [r.schema_version for r in rows],
                [r.ts for r in rows],
                [r.device_status.value for r in rows],
            )
        return len(rows)

    # -- rollups -----------------------------------------------------------

    async def upsert_live_rollups(
        self, aggregates: Sequence[Aggregate], *, updated_at: datetime
    ) -> int:
        if not aggregates:
            return 0

        async with self.pool.acquire() as conn:
            await conn.execute(
                UPSERT_LIVE_ROLLUP,
                [a.bucket for a in aggregates],
                [a.device_id for a in aggregates],
                [a.site_id for a in aggregates],
                [a.metric for a in aggregates],
                [a.mean for a in aggregates],
                [a.minimum for a in aggregates],
                [a.maximum for a in aggregates],
                [a.last_value for a in aggregates],
                [a.count for a in aggregates],
                [a.sum_squares for a in aggregates],
                [updated_at] * len(aggregates),
            )
        return len(aggregates)

    # -- lifecycle ---------------------------------------------------------

    async def write_status(self, messages: Sequence[StatusMessage]) -> int:
        if not messages:
            return 0

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    INSERT_DEVICE_EVENT,
                    [m.ts for m in messages],
                    [m.device_id for m in messages],
                    [m.site_id for m in messages],
                    [m.event.value for m in messages],
                    [m.fw_version for m in messages],
                    [m.reason for m in messages],
                )
                # One statement per device rather than a bulk unnest: the guard
                # compares against the existing row, and a bulk upsert with two
                # events for the same device in one batch would fail on
                # "cannot affect row a second time".
                for message in messages:
                    await conn.execute(
                        UPSERT_DEVICE_FROM_STATUS,
                        message.device_id,
                        message.site_id,
                        message.fw_version,
                        message.event.value,
                        message.ts,
                    )
        return len(messages)

    # -- dead letters ------------------------------------------------------

    async def write_dead_letters(self, dead_letters: Sequence[DeadLetter]) -> int:
        if not dead_letters:
            return 0

        async with self.pool.acquire() as conn:
            await conn.execute(
                INSERT_DEAD_LETTER,
                [d.received_at for d in dead_letters],
                [d.reason for d in dead_letters],
                [d.detail for d in dead_letters],
                [d.topic for d in dead_letters],
                [d.raw for d in dead_letters],
            )
        return len(dead_letters)

    # -- provisioning ------------------------------------------------------

    async def register_certificate(
        self,
        *,
        device_id: str,
        site_id: str,
        common_name: str,
        expires_at: datetime,
        provisioned_at: datetime,
        profile: str | None = None,
    ) -> None:
        """Record a certificate against a device.

        Called by the provisioning tooling rather than by the stream, so that
        "which devices have a certificate expiring this month" is a SQL question.
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO devices (
                    device_id, site_id, profile, certificate_cn,
                    certificate_expires_at, provisioned_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (device_id) DO UPDATE SET
                    site_id                = EXCLUDED.site_id,
                    profile                = COALESCE(EXCLUDED.profile, devices.profile),
                    certificate_cn         = EXCLUDED.certificate_cn,
                    certificate_expires_at = EXCLUDED.certificate_expires_at,
                    provisioned_at         = EXCLUDED.provisioned_at
                """,
                device_id,
                site_id,
                profile,
                common_name,
                expires_at,
                provisioned_at,
            )
