"""Wire models for everything that moves between the fleet and the platform.

These classes are the single definition of the message contract. The JSON
Schema files under ``telemetry_common/schemas`` are generated from them by
``scripts/gen_schemas.py`` and checked in CI, so the published schema cannot
drift away from the code that validates against it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .metrics import is_plausible
from .versioning import CURRENT_SCHEMA_VERSION

DEVICE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,63}$"
SITE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,63}$"
SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"

DeviceId = Annotated[
    str,
    Field(
        pattern=DEVICE_ID_PATTERN,
        description="Lower-case device identity, matches the TLS certificate common name",
    ),
]
SiteId = Annotated[
    str, Field(pattern=SITE_ID_PATTERN, description="Physical site the device is installed at")
]
SemVer = Annotated[str, Field(pattern=SEMVER_PATTERN)]


def utcnow() -> datetime:
    """Timezone-aware now, in UTC. Never use naive datetimes on the wire."""
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Coerce to UTC, treating a naive datetime as already-UTC.

    Devices with no timezone database send naive stamps. Rejecting them would
    drop real data over a formatting detail, so they are read as UTC and the
    firmware is expected to publish UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class DeviceStatus(str, Enum):
    """Self-reported health, decided on the device."""

    OK = "ok"
    DEGRADED = "degraded"
    FAULT = "fault"


class LifecycleEvent(str, Enum):
    """Connection lifecycle, published on the status topic."""

    BOOT = "boot"
    ONLINE = "online"
    OFFLINE = "offline"
    OTA_STARTED = "ota_started"
    OTA_COMPLETED = "ota_completed"
    OTA_FAILED = "ota_failed"


class _WireModel(BaseModel):
    """Base for anything that crosses the broker.

    ``extra="ignore"`` is the compatibility policy in code: a payload from a
    newer minor version carries fields this build has never heard of, and the
    contract says drop them and keep the message.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, use_enum_values=False)


class TelemetryMessage(_WireModel):
    """One batch of sensor readings from one device at one instant."""

    schema_version: SemVer = Field(default=CURRENT_SCHEMA_VERSION)
    device_id: DeviceId
    site_id: SiteId
    seq: int = Field(ge=0, description="Monotonic counter, resets to 0 on reboot")
    ts: datetime = Field(description="Sample time on the device, UTC")
    fw_version: SemVer = Field(description="Firmware version running on the device")
    uptime_s: int = Field(ge=0, description="Seconds since last boot")
    status: DeviceStatus = Field(default=DeviceStatus.OK)
    readings: dict[str, float] = Field(min_length=1, description="Metric name to value")

    @field_validator("ts")
    @classmethod
    def _normalise_ts(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("readings")
    @classmethod
    def _finite_readings(cls, value: dict[str, float]) -> dict[str, float]:
        for metric, reading in value.items():
            if reading != reading or reading in (float("inf"), float("-inf")):
                raise ValueError(f"reading {metric!r} is not a finite number")
        return value

    def implausible_metrics(self) -> list[str]:
        """Metric names whose value sits outside the catalogue range.

        Separate from validation on purpose. A stuck sensor reporting 900 degC
        is a real message from a real device and belongs in the dead-letter
        topic with a reason, not silently dropped by a parser.
        """
        return [m for m, v in self.readings.items() if not is_plausible(m, v)]


class StatusMessage(_WireModel):
    """A lifecycle transition. Also used as the MQTT last will and testament."""

    schema_version: SemVer = Field(default=CURRENT_SCHEMA_VERSION)
    device_id: DeviceId
    site_id: SiteId
    ts: datetime
    event: LifecycleEvent
    fw_version: SemVer
    reason: str | None = Field(default=None, max_length=256)

    @field_validator("ts")
    @classmethod
    def _normalise_ts(cls, value: datetime) -> datetime:
        return _as_utc(value)


class NormalisedReading(BaseModel):
    """One metric from one device at one instant, as carried on the stream.

    Ingest explodes a ``TelemetryMessage`` of N readings into N of these. The
    long form is what TimescaleDB hypertables and continuous aggregates want,
    and it means a new metric never requires a schema migration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ts: datetime
    device_id: str
    site_id: str
    metric: str
    value: float
    seq: int
    fw_version: str
    schema_version: str
    device_status: DeviceStatus
    ingest_ts: datetime = Field(description="When the platform first saw the message, UTC")

    @property
    def ingest_lag_ms(self) -> float:
        """Device-to-platform lag. Negative values mean device clock skew."""
        return (self.ingest_ts - self.ts).total_seconds() * 1000.0


class DeadLetter(BaseModel):
    """A message the platform refused, kept with the reason it was refused."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    detail: str
    topic: str
    received_at: datetime
    raw: str = Field(description="Original payload, truncated to 8 KiB")

    @model_validator(mode="after")
    def _truncate(self) -> DeadLetter:
        if len(self.raw) > 8192:
            object.__setattr__(self, "raw", self.raw[:8192])
        return self


def explode(
    message: TelemetryMessage,
    *,
    ingest_ts: datetime | None = None,
) -> list[NormalisedReading]:
    """Turn one telemetry message into one normalised row per reading."""
    stamp = ingest_ts or utcnow()
    return [
        NormalisedReading(
            ts=message.ts,
            device_id=message.device_id,
            site_id=message.site_id,
            metric=metric,
            value=value,
            seq=message.seq,
            fw_version=message.fw_version,
            schema_version=message.schema_version,
            device_status=message.status,
            ingest_ts=stamp,
        )
        for metric, value in sorted(message.readings.items())
    ]


def to_wire(model: BaseModel) -> dict[str, Any]:
    """Serialise to a JSON-safe dict with RFC 3339 timestamps."""
    return model.model_dump(mode="json")
