"""Live updates: Kafka to WebSocket.

The dashboard could poll the query API every second, and for a handful of
devices that would work. It stops working as soon as the fleet grows, because
every client then runs the same aggregate query against TimescaleDB on a timer
whether anything changed or not.

Instead one consumer per API process reads the telemetry stream and fans the
records out to whichever WebSocket clients are subscribed. The consumer group id
is unique per process and the offset starts at the tail: this is a live feed, not
a replay, and a browser that connects has no interest in the backlog.

Each client gets its own bounded queue. A slow client fills its queue and starts
dropping its own messages; it cannot slow the consumer down or affect anybody
else. Dropping frames for a client that cannot keep up is the correct behaviour
for a live view, and the drop count is reported so it is not silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Counter, Gauge

from telemetry_common.logging import get_logger

log = get_logger(__name__)

SUBSCRIBERS = Gauge("api_live_subscribers", "Connected WebSocket clients")
FANOUT = Counter("api_live_messages_total", "Messages fanned out to clients")
DROPPED = Counter("api_live_dropped_total", "Messages dropped for slow clients")

#: Frames buffered per client before the oldest are discarded. About a second of
#: a busy fleet, which is as much history as a live view is worth.
CLIENT_QUEUE_SIZE = 256


# eq=False keeps the default identity-based __hash__. A dataclass with eq=True
# sets __hash__ to None, which makes instances unhashable and unusable in the
# subscriber set. Identity is the right comparison anyway: two clients watching
# the same device are still two clients.
@dataclass(eq=False)
class Subscription:
    """One connected client and the filter it asked for."""

    queue: asyncio.Queue[str] = field(
        default_factory=lambda: asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    )
    device_id: str | None = None
    site_id: str | None = None
    metric: str | None = None
    dropped: int = 0

    def wants(self, record: dict) -> bool:
        if self.device_id and record.get("device_id") != self.device_id:
            return False
        if self.site_id and record.get("site_id") != self.site_id:
            return False
        if self.metric and record.get("metric") != self.metric:
            return False
        return True

    def offer(self, payload: str) -> None:
        """Enqueue without ever blocking the fan-out loop."""
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Make room by discarding the oldest frame. A live view wants the
            # newest data, so dropping from the front is right.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            DROPPED.inc()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(payload)


class LiveFeed:
    """Owns the Kafka consumer and the set of subscribers."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        telemetry_topic: str,
        alerts_topic: str,
        group_prefix: str = "api-live",
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._telemetry_topic = telemetry_topic
        self._alerts_topic = alerts_topic
        self._group_id = f"{group_prefix}-{uuid.uuid4().hex[:12]}"
        self._subscribers: set[Subscription] = set()
        self._task: asyncio.Task | None = None
        # Any rather than AIOKafkaConsumer, which is imported lazily in _run().
        self._consumer: Any | None = None
        self._stop = asyncio.Event()
        self.connected = False

    # -- subscriptions -----------------------------------------------------

    def subscribe(
        self,
        *,
        device_id: str | None = None,
        site_id: str | None = None,
        metric: str | None = None,
    ) -> Subscription:
        subscription = Subscription(device_id=device_id, site_id=site_id, metric=metric)
        self._subscribers.add(subscription)
        SUBSCRIBERS.set(len(self._subscribers))
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)
        SUBSCRIBERS.set(len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="live-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
            self._consumer = None
        self.connected = False
        log.info("live feed stopped")

    async def _run(self) -> None:
        from aiokafka import AIOKafkaConsumer

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consumer = AIOKafkaConsumer(
                    self._telemetry_topic,
                    self._alerts_topic,
                    bootstrap_servers=self._bootstrap,
                    group_id=self._group_id,
                    # A live view starts at the tail. Replaying the retained log
                    # into a browser on every page load would be pointless and
                    # would take minutes to catch up on a busy stream.
                    auto_offset_reset="latest",
                    enable_auto_commit=False,
                )
                await self._consumer.start()
                self.connected = True
                backoff = 1.0
                log.info("live feed connected", group=self._group_id)

                async for record in self._consumer:
                    self._fanout(record.topic, record.value)
                    if self._stop.is_set():
                        break

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.error("live feed failed, retrying", error=str(exc), backoff_s=backoff)
                with contextlib.suppress(Exception):
                    if self._consumer is not None:
                        await self._consumer.stop()
                self._consumer = None
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, 30.0)

    def _fanout(self, topic: str, raw: bytes) -> None:
        if not self._subscribers:
            return
        try:
            record = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        kind = "alert" if topic == self._alerts_topic else "reading"
        envelope = json.dumps({"type": kind, "data": record}, separators=(",", ":"))

        for subscription in self._subscribers:
            # Alerts always go through: a filter on metric is about the chart,
            # and suppressing an alert because the user is looking at a
            # different chart would be actively harmful.
            if kind == "alert" or subscription.wants(record):
                subscription.offer(envelope)
                FANOUT.inc()
