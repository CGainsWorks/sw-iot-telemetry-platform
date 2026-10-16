"""The ESP32 firmware, emulated, publishing into the running platform.

The firmware under edge/firmware is built with ESP-IDF for the esp32 target and
booted in Espressif's QEMU fork (qemu-system-xtensa), with an OpenCores
Ethernet NIC on QEMU user-mode networking. It is provisioned exactly as a board
would be, with an NVS image holding a certificate issued by the stack's own PKI
and it connects to the same mutual-TLS broker as the simulated fleet.

What this proves that nothing else in the repository does: the firmware's TLS
handshake, MQTT session, topic grammar and JSON are accepted by the real
platform, end to end, into TimescaleDB, with nothing dead-lettered; it obeys a
command sent down the command topic; and it survives a certificate rotation and
is locked out by a revocation.

What it does not prove: anything about the radio, the BME280, the ADC, power or
timing on silicon. Those are not emulated. The environmental readings are
simulated in this build and the device says so in its boot message, which is
asserted below.

Needs the QEMU build (`make firmware`, or the CI artifact) in
edge/firmware/build-qemu. Skipped without it locally; in CI,
REQUIRE_EMULATED_DEVICE=1 turns a missing build into a failure so the job cannot
pass by not running this.

The tests share one device and run in order.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import subprocess
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from .stack import (
    MQTT_HOST,
    MQTT_PORT,
    REPO_ROOT,
    certificate_serial,
    compose,
    docker_available,
    pki,
    port_open,
    register_certificates,
    reload_broker,
    unique_suffix,
)

CERTS = Path(os.environ.get("CERTS_DIR", REPO_ROOT / "certs"))
PG_DSN = os.environ.get("PG_DSN", "postgresql://telemetry:telemetry@localhost:5432/telemetry")
QEMU_BUILD = REPO_ROOT / "edge" / "firmware" / "build-qemu"
REQUIRED = os.environ.get("REQUIRE_EMULATED_DEVICE") == "1"

SITE = "site-q"
DEVICE_ID = f"esp32-qemu-{unique_suffix()}"
FIRMWARE_VERSION = "2.0.0"

# Generous: QEMU boots, takes an address by DHCP, syncs SNTP and completes an
# RSA handshake under emulation before the first message and CI runners vary.
BOOT_TIMEOUT_S = 240.0
PIPELINE_TIMEOUT_S = 90.0


def _unavailable() -> str | None:
    if not (QEMU_BUILD / "flash_args").exists():
        return f"no QEMU firmware build at {QEMU_BUILD.relative_to(REPO_ROOT).as_posix()}; run make firmware"
    if not (port_open("localhost", 5432) and port_open(MQTT_HOST, MQTT_PORT)):
        return "Compose stack is not running"
    if not (CERTS / "ca" / "ca.crt").exists():
        return "certificates have not been exported"
    if not docker_available():
        return "docker is not available"
    return None


_REASON = _unavailable()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_REASON is not None and not REQUIRED, reason=_REASON or ""),
]


class EmulatedDevice:
    """One QEMU instance in a container on the Compose network."""

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.containers: list[str] = []

    @property
    def current(self) -> str:
        return self.containers[-1]

    def boot(self, variant: str = "current") -> None:
        name = f"iot-telemetry-{self.device_id}-{len(self.containers)}"
        compose(
            "--profile",
            "emulated-device",
            "run",
            "-d",
            "--no-deps",
            "--name",
            name,
            "-e",
            f"DEVICE_ID={self.device_id}",
            "-e",
            f"SITE_ID={SITE}",
            "-e",
            f"CERT_VARIANT={variant}",
            "esp32-qemu",
            timeout=300,
        )
        self.containers.append(name)

    def serial_log(self) -> str:
        result = subprocess.run(
            ["docker", "logs", self.current], capture_output=True, timeout=30, check=False
        )
        return (result.stdout + result.stderr).decode(errors="replace")

    def wait_for(self, pattern: str, timeout_s: float = BOOT_TIMEOUT_S) -> re.Match[str] | None:
        deadline = time.monotonic() + timeout_s
        regex = re.compile(pattern)
        while time.monotonic() < deadline:
            match = regex.search(self.serial_log())
            if match:
                return match
            time.sleep(2)
        return None

    def power_off(self) -> None:
        subprocess.run(
            ["docker", "rm", "-f", self.current], capture_output=True, timeout=60, check=False
        )

    def remove_all(self) -> None:
        for name in self.containers:
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False
            )


@pytest.fixture(scope="module")
def device() -> Iterator[EmulatedDevice]:
    if _REASON is not None:
        pytest.fail(f"REQUIRE_EMULATED_DEVICE is set but the emulated device cannot run: {_REASON}")

    # Provisioning, as for a new board: issue its certificate from the
    # platform's CA, record it in the registry, then flash and boot.
    pki("device", DEVICE_ID)
    register_certificates()

    emulated = EmulatedDevice(DEVICE_ID)
    emulated.boot()
    try:
        yield emulated
    finally:
        log = emulated.serial_log() if emulated.containers else ""
        emulated.remove_all()
        # Left in the test output, because when this fails in CI the UART is
        # the only account of what the device thought was happening.
        print(f"\n--- serial console of {DEVICE_ID} (last boot) ---\n{log[-6000:]}")


@pytest.fixture
async def pool():
    import asyncpg

    created = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield created
    finally:
        await created.close()


async def poll(predicate, timeout_s: float = PIPELINE_TIMEOUT_S, interval_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval_s)
    return last


async def samples_since(pool, since: datetime) -> int:
    return await pool.fetchval(
        "SELECT count(DISTINCT ts) FROM telemetry WHERE device_id = $1 AND ts >= $2",
        DEVICE_ID,
        since,
    )


class TestEmulatedDevice:
    def test_it_boots_with_the_identity_it_was_provisioned_with(self, device):
        match = device.wait_for(
            rf"identity {DEVICE_ID} at acme/{SITE}, certificate serial ([0-9A-F]+)"
        )
        assert match, "the firmware never reported its provisioned identity"
        assert match.group(1) == certificate_serial(DEVICE_ID)

        assert device.wait_for(r"clock set from 10\.0\.2\.2"), "SNTP never set the clock"
        assert device.wait_for(
            rf"connected to broker as {DEVICE_ID}"
        ), "no MQTT session over mutual TLS"

    async def test_its_telemetry_is_stored_and_nothing_is_dead_lettered(self, device, pool):
        async def enough():
            rows = await pool.fetch(
                """
                SELECT metric, count(*) AS n, min(fw_version) AS fw, min(device_status) AS status
                FROM telemetry WHERE device_id = $1 GROUP BY metric
                """,
                DEVICE_ID,
            )
            by_metric = {r["metric"]: r for r in rows}
            if all(
                by_metric.get(m) and by_metric[m]["n"] >= 3
                for m in ("temperature_c", "humidity_pct", "pressure_hpa")
            ):
                return by_metric
            return None

        by_metric = await poll(enough)
        assert (
            by_metric
        ), "fewer than three samples of each environmental metric reached TimescaleDB"
        # No Wi-Fi and no ADC under QEMU, so exactly the three environmental
        # metrics: anything else would mean the build is not what it claims.
        assert set(by_metric) == {"temperature_c", "humidity_pct", "pressure_hpa"}
        for row in by_metric.values():
            assert row["fw"] == FIRMWARE_VERSION
            assert row["status"] == "ok"

        rejected = await pool.fetch(
            "SELECT reason, detail FROM dead_letters WHERE topic LIKE $1", f"%/{DEVICE_ID}/%"
        )
        assert not rejected, f"the platform rejected messages from the firmware: {rejected}"

        registry = await pool.fetchrow(
            "SELECT site_id, fw_version, certificate_cn, certificate_expires_at FROM devices WHERE device_id = $1",
            DEVICE_ID,
        )
        assert registry["site_id"] == SITE
        assert registry["fw_version"] == FIRMWARE_VERSION
        assert registry["certificate_cn"] == DEVICE_ID
        assert registry["certificate_expires_at"] > datetime.now(UTC)

    async def test_its_boot_message_says_the_readings_are_simulated(self, device, pool):
        async def boot_event():
            return await pool.fetchrow(
                "SELECT event, reason, fw_version FROM device_events WHERE device_id = $1 AND event = 'boot'",
                DEVICE_ID,
            )

        event = await poll(boot_event)
        assert event, "no boot status message was stored"
        assert event["fw_version"] == FIRMWARE_VERSION
        assert "emulated device" in event["reason"]
        assert "simulated" in event["reason"]

    async def test_it_obeys_a_command_sent_down_the_command_topic(self, device):
        import aiomqtt

        services = CERTS / "services"
        tls = aiomqtt.TLSParameters(
            ca_certs=str(CERTS / "ca" / "ca.crt"),
            certfile=str(services / "command-dispatcher.crt"),
            keyfile=str(services / "command-dispatcher.key"),
            cert_reqs=ssl.CERT_REQUIRED,
        )
        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            identifier=f"command-dispatcher-{DEVICE_ID}",
            tls_params=tls,
            timeout=10,
        ) as client:
            await client.publish(
                f"cmd/acme/{SITE}/{DEVICE_ID}/req",
                json.dumps({"id": "c-1", "action": "identify"}).encode(),
                qos=1,
            )

        assert device.wait_for(
            r"command: identify", timeout_s=60
        ), "the command never reached the firmware"

    async def test_after_rotation_it_reconnects_with_the_new_certificate(self, device, pool):
        old_serial = certificate_serial(DEVICE_ID)
        pki("rotate", DEVICE_ID)
        new_serial = certificate_serial(DEVICE_ID)
        assert new_serial != old_serial
        register_certificates()

        # Delivering the new certificate is a reflash of the NVS partition
        # here; on a fleet it would be an OTA payload or a command. Either way
        # the device restarts with it.
        device.power_off()
        rebooted_at = datetime.now(UTC)
        device.boot("current")

        match = device.wait_for(r"certificate serial ([0-9A-F]+)")
        assert (
            match and match.group(1) == new_serial
        ), "the device did not boot with the rotated certificate"
        assert device.wait_for(
            rf"connected to broker as {DEVICE_ID}"
        ), "rotated certificate refused"

        async def fresh():
            return await samples_since(pool, rebooted_at) >= 2

        assert await poll(fresh), "no telemetry after rotation"

    async def test_a_revoked_certificate_is_refused_at_the_handshake(self, device, pool):
        pki("revoke", DEVICE_ID, "--previous")
        reload_broker()

        device.power_off()
        booted_at = datetime.now(UTC)
        device.boot("previous")

        assert device.wait_for(rf"identity {DEVICE_ID}"), "the device did not boot"
        assert device.wait_for(
            r"transport error"
        ), "expected the broker to refuse the revoked certificate"
        assert "connected to broker" not in device.serial_log()
        assert (
            await samples_since(pool, booted_at) == 0
        ), "a device with a revoked certificate got data in"

        # And the replacement is untouched by the revocation.
        device.power_off()
        device.boot("current")
        assert device.wait_for(
            rf"connected to broker as {DEVICE_ID}"
        ), "revocation locked out the replacement"
