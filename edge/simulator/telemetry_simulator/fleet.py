"""Run a fleet of simulated devices against a real MQTT broker.

One asyncio task and one TLS connection per device, because that is what real
hardware does. Sharing a connection would be cheaper but it would hide exactly
the things worth measuring: per-device handshake cost, broker connection limits
and the behaviour of the ACL when many identities are active at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import signal
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiomqtt

from telemetry_common.logging import get_logger
from telemetry_common.models import LifecycleEvent
from telemetry_common.topics import status_topic, telemetry_topic

from .device import SimulatedDevice
from .faults import ALL_FAULTS, FaultKind, plan_for
from .profiles import FleetMix, site_for

log = get_logger(__name__)


@dataclass
class FleetConfig:
    host: str = "localhost"
    port: int = 8883
    tenant: str = "acme"
    certs_dir: Path = Path("certs")
    device_count: int = 10
    device_prefix: str = "esp32"
    duration_s: float | None = None
    seed: int = 1
    fault_fraction: float = 0.15
    fault_kinds: tuple[FaultKind, ...] = ALL_FAULTS
    qos: int = 1
    #: Seconds to spread initial connections over, so 200 devices do not all
    #: complete a TLS handshake in the same millisecond.
    connect_stagger_s: float = 5.0
    #: Multiplier on simulated physical time. 60 advances a battery discharge or
    #: a developing bearing fault an hour per real minute. Message timestamps
    #: stay on the real clock regardless, because a timestamp hours in the future
    #: is exactly what ingest refuses.
    time_scale: float = 1.0
    warm_start_hours: float = 0.0
    verify_hostname: bool = True
    report_interval_s: float = 10.0


@dataclass
class FleetStats:
    published: int = 0
    failed: int = 0
    suppressed_offline: int = 0
    connected: int = 0
    reconnects: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, float | int]:
        elapsed = max(time.monotonic() - self.started_at, 1e-9)
        return {
            "published": self.published,
            "failed": self.failed,
            "suppressed_offline": self.suppressed_offline,
            "connected": self.connected,
            "reconnects": self.reconnects,
            "elapsed_s": round(elapsed, 2),
            "publish_rate_hz": round(self.published / elapsed, 2),
        }


def device_id_for(index: int, prefix: str) -> str:
    return f"{prefix}-{index:06d}"


def _tls_params(certs_dir: Path, device_id: str) -> aiomqtt.TLSParameters:
    """Build the mutual-TLS parameters for one device.

    Each device presents its own certificate. The common name in that
    certificate becomes the MQTT username at the broker, which is what binds the
    connection to an identity the ACL can enforce.
    """
    device_dir = certs_dir / "devices" / device_id
    ca = certs_dir / "ca" / "ca.crt"
    cert = device_dir / f"{device_id}.crt"
    key = device_dir / f"{device_id}.key"

    for path in (ca, cert, key):
        if not path.exists():
            raise FileNotFoundError(
                f"missing {path}. Run: scripts/pki.sh fleet <count> to provision the fleet"
            )

    return aiomqtt.TLSParameters(
        ca_certs=str(ca),
        certfile=str(cert),
        keyfile=str(key),
        cert_reqs=ssl.CERT_REQUIRED,
    )


class FleetRunner:
    """Owns the device tasks and shuts all of them down together."""

    def __init__(self, config: FleetConfig) -> None:
        self.config = config
        self.stats = FleetStats()
        self._stop = asyncio.Event()
        self._mix = FleetMix()

    def request_stop(self) -> None:
        self._stop.set()

    def _build_devices(self) -> list[SimulatedDevice]:
        cfg = self.config
        epoch = datetime.now(UTC)
        rng = random.Random(cfg.seed)
        devices: list[SimulatedDevice] = []
        for index in range(1, cfg.device_count + 1):
            profile = self._mix.assign(index - 1)
            devices.append(
                SimulatedDevice(
                    device_id=device_id_for(index, cfg.device_prefix),
                    site_id=site_for(index - 1),
                    profile=profile,
                    fault_plan=plan_for(
                        index,
                        fraction=cfg.fault_fraction,
                        kinds=cfg.fault_kinds,
                        seed=cfg.seed,
                    ),
                    seed=cfg.seed * 100_003 + index,
                    epoch=epoch,
                    warm_start_s=rng.uniform(0.0, cfg.warm_start_hours * 3600.0),
                )
            )
        return devices

    async def _run_device(self, device: SimulatedDevice, start_delay_s: float) -> None:
        cfg = self.config
        await asyncio.sleep(start_delay_s)

        tls = _tls_params(cfg.certs_dir, device.device_id)
        telemetry = telemetry_topic(device.device_id, device.site_id, cfg.tenant)
        status = status_topic(device.device_id, device.site_id, cfg.tenant)

        will = aiomqtt.Will(
            topic=status,
            payload=json.dumps(device.will_payload()).encode(),
            qos=1,
            retain=True,
        )

        backoff = 1.0
        run_started = time.monotonic()

        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=cfg.host,
                    port=cfg.port,
                    identifier=device.device_id,
                    tls_params=tls,
                    tls_insecure=not cfg.verify_hostname,
                    will=will,
                    keepalive=30,
                ) as client:
                    self.stats.connected += 1
                    backoff = 1.0

                    # Birth message. Retained, so a dashboard that starts later
                    # still learns the device is up without waiting for telemetry.
                    await client.publish(
                        status,
                        json.dumps(
                            device.lifecycle(LifecycleEvent.ONLINE).model_dump(mode="json")
                        ).encode(),
                        qos=1,
                        retain=True,
                    )

                    while not self._stop.is_set():
                        elapsed_wall = time.monotonic() - run_started
                        if cfg.duration_s is not None and elapsed_wall >= cfg.duration_s:
                            break

                        # Real elapsed time drives the timestamp; scaled time
                        # drives the physics. See SimulatedDevice.sample.
                        message = device.sample(
                            elapsed_wall, sim_elapsed_s=elapsed_wall * cfg.time_scale
                        )
                        if message is None:
                            self.stats.suppressed_offline += 1
                        else:
                            await client.publish(
                                telemetry,
                                json.dumps(message.model_dump(mode="json")).encode(),
                                qos=cfg.qos,
                            )
                            self.stats.published += 1

                        interval = device.next_interval_s() / max(cfg.time_scale, 1e-9)
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(self._stop.wait(), timeout=interval)

                    # Graceful goodbye. The broker discards the will once this
                    # arrives, so a clean shutdown does not raise offline alerts.
                    await client.publish(
                        status,
                        json.dumps(
                            device.lifecycle(LifecycleEvent.OFFLINE, reason="shutdown").model_dump(
                                mode="json"
                            )
                        ).encode(),
                        qos=1,
                        retain=True,
                    )
                    self.stats.connected -= 1
                    return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats.failed += 1
                self.stats.reconnects += 1
                log.warning(
                    "device connection failed, retrying",
                    device_id=device.device_id,
                    error=str(exc),
                    backoff_s=round(backoff, 1),
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _report(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.report_interval_s)
                return
            except TimeoutError:
                log.info("fleet", **self.stats.snapshot())

    async def run(self) -> FleetStats:
        cfg = self.config
        devices = self._build_devices()
        faulty = sum(1 for d in devices if d.fault_plan.kinds)
        log.info(
            "starting fleet",
            devices=len(devices),
            faulty=faulty,
            broker=f"{cfg.host}:{cfg.port}",
            duration_s=cfg.duration_s,
            time_scale=cfg.time_scale,
        )

        stagger = cfg.connect_stagger_s / max(len(devices), 1)
        tasks = [
            asyncio.create_task(
                self._run_device(device, index * stagger), name=f"device-{device.device_id}"
            )
            for index, device in enumerate(devices)
        ]
        reporter = asyncio.create_task(self._report(), name="fleet-reporter")

        if cfg.duration_s is not None:
            # Bound to a local so the narrowing survives into the closure below.
            deadline_s = cfg.duration_s + 5.0

            # A hard stop so the simulator always terminates on its own. Anything
            # started in the background has to be able to end without a human.
            async def _deadline() -> None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=deadline_s)
                except TimeoutError:
                    log.info("duration reached, stopping fleet")
                    self.request_stop()

            tasks.append(asyncio.create_task(_deadline(), name="fleet-deadline"))

        try:
            await asyncio.gather(*tasks)
        finally:
            self.request_stop()
            reporter.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, reporter, return_exceptions=True)

        log.info("fleet stopped", **self.stats.snapshot())
        return self.stats


async def run_fleet(config: FleetConfig) -> FleetStats:
    """Run a fleet, stopping cleanly on SIGINT, SIGTERM or the configured duration."""
    runner = FleetRunner(config)
    loop = asyncio.get_running_loop()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, runner.request_stop)
        except NotImplementedError:
            # Windows event loops do not support add_signal_handler. The
            # KeyboardInterrupt path in the CLI covers it there.
            signal.signal(sig, lambda *_: runner.request_stop())

    return await runner.run()
