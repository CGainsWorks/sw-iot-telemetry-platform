"""Helpers for driving the running Compose stack from the integration tests.

Certificate work happens inside the stack's own `pki` container, against the
CA in the shared volume, exactly as an operator would run it. Nothing here
holds a copy of the CA key; the tests only ever receive device certificates and
keys, streamed out of the volume as a tar archive.
"""

from __future__ import annotations

import io
import os
import shlex
import socket
import subprocess
import tarfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))

#: Account the services run as. New device material is handed to it so that the
#: volume stays in the state provision_fleet.sh leaves it in.
APP_UID = 10001


def compose(
    *args: str, timeout: float = 180, check: bool = True, capture: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        cwd=REPO_ROOT,
        capture_output=capture,
        timeout=timeout,
        check=check,
    )


def in_pki(script: str, timeout: float = 120) -> bytes:
    """Run a shell snippet in a throwaway `pki` container and return stdout."""
    result = compose("run", "--rm", "--no-deps", "-T", "pki", script, timeout=timeout, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"pki command failed ({result.returncode}): {script}\n"
            f"{result.stderr.decode(errors='replace')}"
        )
    return result.stdout


def pki(*args: str) -> None:
    """scripts/pki.sh inside the stack, then hand the device tree back to the app account."""
    command = shlex.join(["bash", "/app/scripts/pki.sh", *args])
    in_pki(f"{command} >&2 && chown -R {APP_UID}:{APP_UID} /certs/devices")


def fetch_device(device_id: str, destination: Path) -> Path:
    """Copy one device's certificate directory out of the volume."""
    archive = in_pki(f"tar -C /certs/devices -cf - {shlex.quote(device_id)}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    return destination / device_id


def certificate_serial(device_id: str, previous: bool = False) -> str:
    """Serial of a device certificate as openssl prints it, upper-case hex."""
    sub = "previous/" if previous else ""
    path = f"/certs/devices/{device_id}/{sub}{device_id}.crt"
    out = in_pki(f"openssl x509 -noout -serial -in {shlex.quote(path)}").decode()
    return out.strip().split("=", 1)[1].upper()


def register_certificates() -> None:
    """Re-run the job that records issued certificates in the device registry."""
    compose("run", "--rm", "--no-deps", "certs-register", timeout=120)


def reload_broker(timeout_s: float = 30.0) -> None:
    """Make Mosquitto re-read its configuration, certificates and CRL.

    SIGHUP rather than a restart, so connected clients, the simulated fleet and
    the ingest bridge among them, keep their sessions.
    """
    compose("kill", "-s", "SIGHUP", "mosquitto")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if port_open(MQTT_HOST, MQTT_PORT):
            return
        time.sleep(0.5)
    raise TimeoutError("broker did not come back after SIGHUP")


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def unique_suffix() -> str:
    """Short, grammar-safe and different on every run, so reruns never collide."""
    return format(int(time.time() * 1000) % 0xFFFFFFF, "x")
