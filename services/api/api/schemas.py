"""Response models for the query API.

Explicit models rather than returning raw rows, so the OpenAPI document the
dashboard is generated against is accurate and a column rename in the database
cannot silently change the public contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    status: str
    service: str = "telemetry-api"
    version: str


class ReadyResponse(BaseModel):
    ready: bool
    database: str
    detail: str | None = None


class MetricInfo(BaseModel):
    name: str
    unit: str
    minimum: float
    maximum: float
    description: str


class DeviceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: str
    site_id: str
    profile: str | None = None
    fw_version: str | None = None
    schema_version: str | None = None
    last_seen: datetime | None = None
    last_status: str | None = None
    last_lifecycle_event: str | None = None
    health: str
    seconds_since_last_seen: float | None = None
    certificate_expires_at: datetime | None = None
    certificate_days_remaining: float | None = None


class DeviceListResponse(BaseModel):
    devices: list[DeviceSummary]
    total: int
    #: Count per health state, so a dashboard can render the summary tiles
    #: without a second request or a client-side reduce over a paged list.
    by_health: dict[str, int]


class TelemetryPoint(BaseModel):
    ts: datetime
    device_id: str
    site_id: str
    metric: str
    value: float
    #: Present only when the answer came from a rollup.
    min_value: float | None = None
    max_value: float | None = None
    sample_count: int | None = None


class TelemetryResponse(BaseModel):
    points: list[TelemetryPoint]
    count: int
    #: Which table answered, at what resolution, and why. Returned rather than
    #: hidden so a slow or surprising result can be explained without reading
    #: the server logs.
    source: str
    resolution: str
    reason: str
    truncated: bool = Field(
        default=False,
        description="True when the row limit was hit and the window is not fully covered",
    )


class AlertRecord(BaseModel):
    alert_id: str
    rule_id: str
    rule_name: str | None = None
    device_id: str
    site_id: str
    metric: str | None = None
    severity: str
    state: str
    value: float | None = None
    threshold: float | None = None
    message: str
    fired_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    notification_count: int = 0


class AlertListResponse(BaseModel):
    alerts: list[AlertRecord]
    total: int
    by_severity: dict[str, int]


class DeadLetterSummary(BaseModel):
    reason: str
    count: int
    latest: datetime
    example_detail: str


class DeadLetterResponse(BaseModel):
    summary: list[DeadLetterSummary]
    total: int
    window_hours: int


class PlatformStats(BaseModel):
    devices_total: int
    devices_healthy: int
    devices_offline: int
    readings_last_hour: int
    readings_last_24h: int
    active_alerts: int
    dead_letters_last_hour: int
    metrics_tracked: int
    oldest_reading: datetime | None = None
    newest_reading: datetime | None = None


class SiteRollup(BaseModel):
    site_id: str
    metric: str
    avg_value: float
    min_value: float
    max_value: float
    device_count: int
    sample_count: int
