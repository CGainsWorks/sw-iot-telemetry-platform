"""HTTP and WebSocket endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import Counter as CountBy
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

from telemetry_common.logging import get_logger
from telemetry_common.metrics import CATALOGUE
from telemetry_common.versioning import CURRENT_SCHEMA_VERSION

from .db import MAX_ROWS, Database
from .live import LiveFeed
from .resolution import Resolution, RetentionError, choose
from .schemas import (
    AlertListResponse,
    AlertRecord,
    DeadLetterResponse,
    DeadLetterSummary,
    DeviceListResponse,
    DeviceSummary,
    HealthResponse,
    MetricInfo,
    PlatformStats,
    ReadyResponse,
    SiteRollup,
    TelemetryPoint,
    TelemetryResponse,
)

log = get_logger(__name__)

router = APIRouter()

#: Widest window a single request may ask for. Prevents an accidental "all of
#: time" query from pinning the database.
MAX_WINDOW = timedelta(days=400)


def _db(request: Request) -> Database:
    return request.app.state.db


def _feed(request: Request) -> LiveFeed:
    return request.app.state.live


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive query parameter as UTC rather than as local time.

    A dashboard sends RFC 3339 with an offset. A human with curl usually does
    not, and silently reading their input as server-local time would return the
    wrong window without any error.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/healthz", response_model=HealthResponse, tags=["health"])
async def healthz() -> HealthResponse:
    """Liveness. Deliberately does not touch the database.

    A liveness probe that checks a dependency restarts a perfectly good process
    when that dependency blips, which turns a brief outage into a longer one.
    """
    return HealthResponse(status="ok", version=CURRENT_SCHEMA_VERSION)


@router.get("/readyz", response_model=ReadyResponse, tags=["health"])
async def readyz(request: Request) -> ReadyResponse:
    """Readiness. Checks the database, because without it nothing can be served."""
    try:
        await _db(request).ping()
    except Exception as exc:
        return ReadyResponse(ready=False, database="unreachable", detail=str(exc)[:200])
    return ReadyResponse(ready=True, database="ok")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@router.get("/api/v1/metrics", response_model=list[MetricInfo], tags=["catalogue"])
async def list_metrics() -> list[MetricInfo]:
    return [
        MetricInfo(
            name=spec.name,
            unit=spec.unit,
            minimum=spec.minimum,
            maximum=spec.maximum,
            description=spec.description,
        )
        for spec in CATALOGUE.values()
    ]


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


@router.get("/api/v1/devices", response_model=DeviceListResponse, tags=["devices"])
async def list_devices(
    request: Request,
    site_id: str | None = Query(default=None),
    health: str | None = Query(default=None),
) -> DeviceListResponse:
    rows = await _db(request).list_devices(site_id=site_id, health=health)
    devices = [DeviceSummary(**dict(row)) for row in rows]
    return DeviceListResponse(
        devices=devices,
        total=len(devices),
        by_health=dict(CountBy(d.health for d in devices)),
    )


@router.get("/api/v1/devices/{device_id}", tags=["devices"])
async def get_device(request: Request, device_id: str) -> dict:
    database = _db(request)
    row = await database.get_device(device_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}")
    return {
        "device": DeviceSummary(**dict(row)).model_dump(mode="json"),
        "metrics": await database.device_metrics(device_id),
    }


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


@router.get("/api/v1/telemetry", response_model=TelemetryResponse, tags=["telemetry"])
async def query_telemetry(
    request: Request,
    start: datetime | None = Query(default=None, description="Window start, RFC 3339"),
    end: datetime | None = Query(default=None, description="Window end, RFC 3339"),
    minutes: int | None = Query(
        default=None, ge=1, le=525_600, description="Shorthand for the last N minutes"
    ),
    device_id: str | None = Query(default=None),
    site_id: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    resolution: Resolution = Query(default=Resolution.AUTO),
    limit: int = Query(default=MAX_ROWS, ge=1, le=MAX_ROWS),
) -> TelemetryResponse:
    """Query telemetry, letting the server pick the cheapest adequate source.

    The chosen source and the reasoning behind it come back in the response, so
    a surprising result can be explained without reading the server logs.
    """
    now = _utcnow()

    if minutes is not None:
        window_end = now
        window_start = now - timedelta(minutes=minutes)
    else:
        window_end = _as_utc(end) if end else now
        window_start = _as_utc(start) if start else window_end - timedelta(hours=1)

    if window_end <= window_start:
        raise HTTPException(status_code=400, detail="end must be after start")
    if window_end - window_start > MAX_WINDOW:
        raise HTTPException(
            status_code=400,
            detail=f"window is longer than the {MAX_WINDOW.days} day maximum",
        )

    try:
        plan = choose(start=window_start, end=window_end, requested=resolution, now=now)
    except RetentionError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = await _db(request).query_telemetry(
        plan,
        start=window_start,
        end=window_end,
        device_id=device_id,
        site_id=site_id,
        metric=metric,
        limit=limit,
    )

    points = [TelemetryPoint(**dict(row)) for row in rows]
    return TelemetryResponse(
        points=points,
        count=len(points),
        source=plan.source.value,
        resolution=plan.resolution.value,
        reason=plan.reason,
        truncated=len(points) >= min(limit, MAX_ROWS),
    )


@router.get("/api/v1/telemetry/latest", tags=["telemetry"])
async def latest(
    request: Request,
    site_id: str | None = Query(default=None),
    metric: str | None = Query(default=None),
) -> dict:
    rows = await _db(request).latest_readings(site_id=site_id, metric=metric)
    return {"readings": [dict(row) for row in rows], "count": len(rows)}


@router.get("/api/v1/sites/rollup", response_model=list[SiteRollup], tags=["telemetry"])
async def site_rollup(
    request: Request, hours: int = Query(default=1, ge=1, le=48)
) -> list[SiteRollup]:
    rows = await _db(request).site_rollup(hours=hours)
    return [SiteRollup(**dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/api/v1/alerts", response_model=AlertListResponse, tags=["alerts"])
async def list_alerts(
    request: Request,
    state: str | None = Query(default="firing", pattern="^(firing|resolved|all)$"),
    severity: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> AlertListResponse:
    rows = await _db(request).list_alerts(
        state=None if state == "all" else state,
        severity=severity,
        device_id=device_id,
        limit=limit,
    )
    alerts = [AlertRecord(**dict(row)) for row in rows]
    return AlertListResponse(
        alerts=alerts,
        total=len(alerts),
        by_severity=dict(CountBy(a.severity for a in alerts)),
    )


@router.get("/api/v1/alerts/rules", tags=["alerts"])
async def list_rules(request: Request) -> dict:
    rows = await _db(request).list_rules()
    return {"rules": [dict(row) for row in rows], "total": len(rows)}


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


@router.get("/api/v1/dead-letters", response_model=DeadLetterResponse, tags=["operations"])
async def dead_letters(
    request: Request, hours: int = Query(default=24, ge=1, le=168)
) -> DeadLetterResponse:
    rows = await _db(request).dead_letter_summary(hours=hours)
    summary = [DeadLetterSummary(**dict(row)) for row in rows]
    return DeadLetterResponse(
        summary=summary,
        total=sum(item.count for item in summary),
        window_hours=hours,
    )


@router.get("/api/v1/stats", response_model=PlatformStats, tags=["operations"])
async def stats(request: Request) -> PlatformStats:
    return PlatformStats(**await _db(request).platform_stats())


# ---------------------------------------------------------------------------
# Live feed
# ---------------------------------------------------------------------------


@router.websocket("/api/v1/stream")
async def stream(
    websocket: WebSocket,
    device_id: str | None = Query(default=None),
    site_id: str | None = Query(default=None),
    metric: str | None = Query(default=None),
) -> None:
    """Push readings and alerts as they arrive.

    Filters are applied server side so a dashboard watching one device does not
    receive, decode and discard the whole fleet's traffic.
    """
    await websocket.accept()
    feed: LiveFeed = websocket.app.state.live
    subscription = feed.subscribe(device_id=device_id, site_id=site_id, metric=metric)

    await websocket.send_text(
        json.dumps(
            {
                "type": "hello",
                "data": {
                    "filters": {"device_id": device_id, "site_id": site_id, "metric": metric},
                    "feed_connected": feed.connected,
                },
            }
        )
    )

    async def pump() -> None:
        while True:
            payload = await subscription.queue.get()
            await websocket.send_text(payload)

    async def drain_client() -> None:
        # A WebSocket only notices the peer has gone when it tries to read, so
        # this coroutine exists purely to detect the disconnect. Anything the
        # client sends is ignored: the feed is one directional.
        while True:
            await websocket.receive_text()

    pump_task = asyncio.create_task(pump(), name="ws-pump")
    drain_task = asyncio.create_task(drain_client(), name="ws-drain")

    try:
        done, pending = await asyncio.wait(
            {pump_task, drain_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in (pump_task, drain_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        feed.unsubscribe(subscription)
        if subscription.dropped:
            log.info("client dropped frames", dropped=subscription.dropped)
