"""Notification channels.

Two are implemented, ``log`` and ``webhook``. Both record their outcome in the
``notifications`` table so a delivery failure is visible rather than being a
missing line in a log somewhere.

Email, SNS and PagerDuty are not implemented. They are listed in
docs/alert_catalogue.md as unimplemented rather than stubbed out here, because a
notifier that silently succeeds without sending anything is exactly the failure
this table exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from telemetry_common.logging import get_logger

log = get_logger(__name__)


@dataclass
class Notification:
    """What gets sent, independent of how."""

    alert_id: str
    rule_id: str
    rule_name: str
    severity: str
    device_id: str
    site_id: str
    metric: str | None
    value: float | None
    threshold: float | None
    message: str
    state: str
    fired_at: datetime

    def as_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "device_id": self.device_id,
            "site_id": self.site_id,
            "metric": self.metric,
            "value": self.value,
            "threshold": self.threshold,
            "message": self.message,
            "state": self.state,
            "fired_at": self.fired_at.isoformat(),
        }


@dataclass
class DeliveryResult:
    channel: str
    status: str  # sent | failed | suppressed
    detail: str | None = None


class Notifier(Protocol):
    name: str

    async def send(self, notification: Notification) -> DeliveryResult: ...

    async def close(self) -> None: ...


class LogNotifier:
    """Writes the alert to the service log. Always available, never fails."""

    name = "log"

    async def send(self, notification: Notification) -> DeliveryResult:
        emit = log.error if notification.severity == "critical" else log.warning
        emit(
            "alert",
            state=notification.state,
            rule_id=notification.rule_id,
            severity=notification.severity,
            device_id=notification.device_id,
            site_id=notification.site_id,
            metric=notification.metric,
            value=notification.value,
            message=notification.message,
        )
        return DeliveryResult(channel=self.name, status="sent")

    async def close(self) -> None:
        return None


class WebhookNotifier:
    """POSTs the alert as JSON to a configured URL.

    When no URL is configured the result is ``suppressed`` rather than ``sent``.
    The distinction matters: it means "nobody was told, and here is why" instead
    of a false record of successful delivery.
    """

    name = "webhook"

    def __init__(self, url: str | None, *, timeout_s: float = 5.0) -> None:
        self._url = (url or "").strip()
        self._timeout_s = timeout_s
        self._client = None

    async def _ensure_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._timeout_s)
        return self._client

    async def send(self, notification: Notification) -> DeliveryResult:
        if not self._url:
            return DeliveryResult(
                channel=self.name,
                status="suppressed",
                detail="ALERT_WEBHOOK_URL is not set",
            )

        try:
            client = await self._ensure_client()
            response = await client.post(
                self._url,
                content=json.dumps(notification.as_dict()),
                headers={"content-type": "application/json"},
            )
            if response.status_code >= 400:
                return DeliveryResult(
                    channel=self.name,
                    status="failed",
                    detail=f"HTTP {response.status_code}: {response.text[:200]}",
                )
            return DeliveryResult(
                channel=self.name, status="sent", detail=f"HTTP {response.status_code}"
            )
        except Exception as exc:
            return DeliveryResult(channel=self.name, status="failed", detail=str(exc)[:200])

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class StreamNotifier:
    """Publishes the alert to the ``alerts.fired`` topic.

    This is what puts alerts on the dashboard's WebSocket without the dashboard
    polling for them, and it gives anything else that cares a place to subscribe.
    """

    name = "stream"

    def __init__(self, *, bootstrap_servers: str, topic: str) -> None:
        self._bootstrap = bootstrap_servers
        self._topic = topic
        # Any rather than AIOKafkaProducer, imported lazily in start().
        self._producer: Any | None = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            value_serializer=lambda value: json.dumps(value).encode(),
            key_serializer=lambda key: key.encode() if key else None,
            acks="all",
        )
        await self._producer.start()
        log.info("alert stream notifier started", topic=self._topic)

    async def send(self, notification: Notification) -> DeliveryResult:
        if self._producer is None:
            return DeliveryResult(
                channel=self.name, status="suppressed", detail="producer not started"
            )
        try:
            await self._producer.send_and_wait(
                self._topic, value=notification.as_dict(), key=notification.device_id
            )
            return DeliveryResult(channel=self.name, status="sent")
        except Exception as exc:
            return DeliveryResult(channel=self.name, status="failed", detail=str(exc)[:200])

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def utcnow() -> datetime:
    return datetime.now(UTC)
