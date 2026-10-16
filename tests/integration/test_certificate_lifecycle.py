"""Certificate provisioning, rotation and revocation against the running stack.

The procedure in docs/provisioning.md, executed rather than described:

1. provision a brand new device into a fleet that is already running and see
   its first message stored and its certificate recorded in the registry;
2. rotate it and see both the outgoing and the replacement certificate
   accepted during the overlap window;
3. revoke the outgoing certificate, reload the broker and see that one
   refused while the replacement keeps working. Before the reload the revoked
   certificate is still accepted, which is asserted too, because it is the one
   place this broker behaves differently from IoT Core.

This is the local stand-in for the AWS IoT Core sequence in the same document:
create-keys-and-certificate, attach, then update-certificate --new-status
INACTIVE. The broker is Mosquitto with mutual TLS and a CRL, so the outcome is
the same and the mechanism differs; docs/architecture.md lists the differences.

The tests in this module share one device and run in order.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from .stack import (
    MQTT_HOST,
    MQTT_PORT,
    REPO_ROOT,
    certificate_serial,
    docker_available,
    fetch_device,
    pki,
    port_open,
    register_certificates,
    reload_broker,
    unique_suffix,
)

CERTS = Path(os.environ.get("CERTS_DIR", REPO_ROOT / "certs"))
PG_DSN = os.environ.get("PG_DSN", "postgresql://telemetry:telemetry@localhost:5432/telemetry")
SITE = "site-l"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            port_open("localhost", 5432)
            and port_open(MQTT_HOST, MQTT_PORT)
            and (CERTS / "ca" / "ca.crt").exists()
            and docker_available()
        ),
        reason="Compose stack is not running, or certs have not been exported.",
    ),
]

DEVICE_ID = f"lifecycle-{unique_suffix()}"


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("lifecycle")


@pytest.fixture
async def pool():
    import asyncpg

    created = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2)
    try:
        yield created
    finally:
        await created.close()


def tls_for(directory: Path):
    import aiomqtt

    return aiomqtt.TLSParameters(
        ca_certs=str(CERTS / "ca" / "ca.crt"),
        certfile=str(directory / f"{DEVICE_ID}.crt"),
        keyfile=str(directory / f"{DEVICE_ID}.key"),
        cert_reqs=ssl.CERT_REQUIRED,
    )


def reading(marker: float) -> bytes:
    return json.dumps(
        {
            "schema_version": "1.1.0",
            "device_id": DEVICE_ID,
            "site_id": SITE,
            "seq": int(time.time()) % 1_000_000,
            "ts": datetime.now(UTC).isoformat(),
            "fw_version": "1.4.2",
            "uptime_s": 1,
            "status": "ok",
            "readings": {"rssi_dbm": marker},
        }
    ).encode()


async def publish(directory: Path, marker: float, client_suffix: str) -> None:
    """Connect with the certificate in ``directory`` and publish one reading."""
    import aiomqtt

    async with aiomqtt.Client(
        hostname=MQTT_HOST,
        port=MQTT_PORT,
        identifier=f"{DEVICE_ID}-{client_suffix}",
        tls_params=tls_for(directory),
        timeout=10,
    ) as client:
        await client.publish(f"dt/acme/{SITE}/{DEVICE_ID}/telemetry", reading(marker), qos=1)


async def stored(pool, marker: float, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = await pool.fetchval(
            "SELECT count(*) FROM telemetry WHERE device_id = $1 AND metric = 'rssi_dbm' AND value = $2",
            DEVICE_ID,
            marker,
        )
        if found:
            return True
        await asyncio.sleep(1)
    return False


REFUSALS: tuple[type[BaseException], ...] = (ssl.SSLError, OSError, TimeoutError)


class TestCertificateLifecycle:
    async def test_a_new_device_is_provisioned_into_the_running_fleet(self, workdir, pool):
        pki("device", DEVICE_ID)
        directory = fetch_device(DEVICE_ID, workdir / "issued")
        assert (directory / f"{DEVICE_ID}.crt").exists()

        await publish(directory, -71.1, "first")
        assert await stored(
            pool, -71.1
        ), "the newly provisioned device's first reading was not stored"

        # The registry learns about the certificate from the PKI, not from the
        # device, so the certificate-expiring rule covers it from day one.
        register_certificates()
        row = await pool.fetchrow(
            "SELECT certificate_cn, certificate_expires_at FROM devices WHERE device_id = $1",
            DEVICE_ID,
        )
        assert row is not None
        assert row["certificate_cn"] == DEVICE_ID
        assert row["certificate_expires_at"] > datetime.now(UTC)

    async def test_rotation_keeps_both_certificates_valid_during_the_overlap(self, workdir, pool):
        old_serial = certificate_serial(DEVICE_ID)
        pki("rotate", DEVICE_ID)
        directory = fetch_device(DEVICE_ID, workdir / "rotated")

        new_serial = certificate_serial(DEVICE_ID)
        assert new_serial != old_serial
        assert certificate_serial(DEVICE_ID, previous=True) == old_serial

        # The device may not have taken delivery of the new one yet, so the
        # outgoing certificate must still work.
        await publish(directory / "previous", -71.2, "outgoing")
        await publish(directory, -71.3, "replacement")
        assert await stored(
            pool, -71.2
        ), "the outgoing certificate stopped working before revocation"
        assert await stored(pool, -71.3), "the replacement certificate was not accepted"

    async def test_a_revoked_certificate_is_refused_and_its_replacement_is_not(self, workdir, pool):
        directory = workdir / "rotated" / DEVICE_ID
        pki("revoke", DEVICE_ID, "--previous")

        # Mosquitto reads the CRL when it loads its configuration, so until the
        # broker is reloaded a revoked certificate still gets in. Asserted
        # rather than glossed over: it is the operational gap between this
        # broker and IoT Core, where INACTIVE takes effect at once and
        # docs/provisioning.md tells the operator to reload.
        await publish(directory / "previous", -71.6, "before-reload")
        assert await stored(pool, -71.6), "expected the CRL to take effect only on reload"

        reload_broker()

        import aiomqtt

        with pytest.raises((aiomqtt.MqttError, *REFUSALS)):
            await publish(directory / "previous", -71.4, "revoked")
        assert not await stored(pool, -71.4, timeout_s=10), "a revoked certificate published"

        await publish(directory, -71.5, "after-revocation")
        assert await stored(
            pool, -71.5
        ), "revoking the outgoing certificate locked out the replacement"
