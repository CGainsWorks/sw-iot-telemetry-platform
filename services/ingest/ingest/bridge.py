"""The MQTT to stream bridge.

Subscribes to the fleet's telemetry and status topics with its own client
certificate, runs each message through :mod:`ingest.validation` and forwards
the result to a :class:`~ingest.sinks.StreamSink`.

On AWS this process does not exist: an IoT Core rule does the same routing in
managed infrastructure, using the rule SQL in ``services/ingest/iot_rules``. The
validation module is shared between the two, so what is enforced does not depend
on which one is running.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiomqtt
from prometheus_client import Counter, Gauge, Histogram

from telemetry_common.logging import get_logger
from telemetry_common.topics import subscription_all_status, subscription_all_telemetry

from .sinks import StreamSink
from .validation import Accepted, AcceptedStatus, Rejected, handle

log = get_logger(__name__)

MESSAGES = Counter(
    "ingest_messages_total", "Messages received from the broker", ["kind", "outcome"]
)
REJECTIONS = Counter("ingest_rejected_total", "Messages dead-lettered", ["reason"])
READINGS = Counter("ingest_readings_total", "Normalised readings forwarded to the stream")
SCHEMA_AHEAD = Counter(
    "ingest_schema_ahead_total",
    "Accepted messages whose schema minor is ahead of this build",
)
INGEST_LAG = Histogram(
    "ingest_lag_seconds",
    "Device timestamp to platform receipt",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
HANDLE_TIME = Histogram(
    "ingest_handle_seconds",
    "Time spent validating and forwarding one message",
    buckets=(0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05, 0.1),
)
CONNECTED = Gauge("ingest_broker_connected", "1 when the bridge holds a broker connection")


@dataclass
class BridgeConfig:
    host: str = "mosquitto"
    port: int = 8883
    tenant: str = "acme"
    certs_dir: Path = Path("/certs")
    identity: str = "ingest-bridge"
    qos: int = 1
    verify_hostname: bool = True
    reject_implausible: bool = True
    #: Seconds between reconnect attempts, doubling to this ceiling.
    max_backoff_s: float = 30.0


@dataclass
class BridgeStats:
    accepted: int = 0
    rejected: int = 0
    status: int = 0
    readings: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, float | int]:
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "status": self.status,
            "readings": self.readings,
            "elapsed_s": round(elapsed, 2),
            "accept_rate_hz": round(self.accepted / elapsed, 2),
        }


def _as_bytes(payload: object) -> bytes:
    """Normalise an MQTT payload to bytes.

    paho types a payload as str | bytes | bytearray | int | float | None, and
    bytes() refuses most of those. A device could in principle publish an empty
    payload or a bare number, and neither should take the bridge down.
    """
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    if payload is None:
        return b""
    return str(payload).encode("utf-8")


def _tls_params(certs_dir: Path, identity: str) -> aiomqtt.TLSParameters:
    ca = certs_dir / "ca" / "ca.crt"
    cert = certs_dir / "services" / f"{identity}.crt"
    key = certs_dir / "services" / f"{identity}.key"
    for path in (ca, cert, key):
        if not path.exists():
            raise FileNotFoundError(f"missing {path}. Run: scripts/pki.sh service {identity}")
    return aiomqtt.TLSParameters(
        ca_certs=str(ca),
        certfile=str(cert),
        keyfile=str(key),
        cert_reqs=ssl.CERT_REQUIRED,
    )


class Bridge:
    def __init__(self, config: BridgeConfig, sink: StreamSink) -> None:
        self.config = config
        self.sink = sink
        self.stats = BridgeStats()
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def _dispatch(self, topic: str, payload: bytes) -> None:
        started = time.perf_counter()
        result = handle(topic, payload, reject_implausible=self.config.reject_implausible)

        if isinstance(result, Accepted):
            await self.sink.send_readings(result.readings)
            self.stats.accepted += 1
            self.stats.readings += len(result.readings)
            MESSAGES.labels(kind="telemetry", outcome="accepted").inc()
            READINGS.inc(len(result.readings))
            if result.schema_ahead:
                SCHEMA_AHEAD.inc()
            if result.readings:
                # Clock skew can make this negative. Clamping keeps the histogram
                # meaningful while the raw skew stays visible in the stored rows.
                INGEST_LAG.observe(max(result.readings[0].ingest_lag_ms / 1000.0, 0.0))

        elif isinstance(result, AcceptedStatus):
            await self.sink.send_status(result.message)
            self.stats.status += 1
            MESSAGES.labels(kind="status", outcome="accepted").inc()

        elif isinstance(result, Rejected):
            await self.sink.send_dead_letter(result.dead_letter)
            self.stats.rejected += 1
            MESSAGES.labels(kind="unknown", outcome="rejected").inc()
            REJECTIONS.labels(reason=result.reason.value).inc()
            log.warning(
                "message rejected",
                reason=result.reason.value,
                detail=result.detail[:200],
                topic=topic,
            )

        HANDLE_TIME.observe(time.perf_counter() - started)

    async def run(self) -> BridgeStats:
        cfg = self.config
        tls = _tls_params(cfg.certs_dir, cfg.identity)
        backoff = 1.0

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=cfg.host,
                    port=cfg.port,
                    identifier=cfg.identity,
                    tls_params=tls,
                    tls_insecure=not cfg.verify_hostname,
                    keepalive=30,
                ) as client:
                    CONNECTED.set(1)
                    backoff = 1.0
                    await client.subscribe(subscription_all_telemetry(cfg.tenant), qos=cfg.qos)
                    await client.subscribe(subscription_all_status(cfg.tenant), qos=cfg.qos)
                    log.info(
                        "bridge connected",
                        broker=f"{cfg.host}:{cfg.port}",
                        identity=cfg.identity,
                    )

                    stopper = asyncio.create_task(self._stop.wait(), name="bridge-stop")
                    try:
                        async for message in client.messages:
                            await self._dispatch(str(message.topic), _as_bytes(message.payload))
                            if self._stop.is_set():
                                break
                    finally:
                        stopper.cancel()

                    CONNECTED.set(0)
                    if self._stop.is_set():
                        return self.stats

            except asyncio.CancelledError:
                CONNECTED.set(0)
                raise
            except Exception as exc:
                CONNECTED.set(0)
                log.error(
                    "bridge connection failed, retrying",
                    error=str(exc),
                    backoff_s=round(backoff, 1),
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, cfg.max_backoff_s)

        return self.stats


async def run_bridge(config: BridgeConfig, sink: StreamSink) -> BridgeStats:
    """Run the bridge until interrupted, then shut the sink down cleanly."""
    bridge = Bridge(config, sink)
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bridge.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: bridge.request_stop())

    await sink.start()
    try:
        return await bridge.run()
    finally:
        # Flush whatever is still buffered before the process goes away. Dropping
        # it here would lose messages the broker has already acknowledged.
        await sink.stop()
        log.info("bridge stopped", **bridge.stats.snapshot())
