#!/usr/bin/env python3
"""Record issued device certificates in the device registry.

This closes the link between provisioning and the platform. Without it the
`certificate_cn` and `certificate_expires_at` columns on `devices` stay null
forever, which means:

* the `certificate-expiring` alert rule can never fire, because it has nothing
  to evaluate. An alert rule that cannot fire is worse than no rule, because it
  looks like reassurance;
* `device_health.certificate_days_remaining` is always null;
* the fleet health query reports zero certificates expiring soon regardless of
  the truth.

That was the actual state of this repository until the analytical export
produced an all-null column and a query failed on it. The gap was invisible from
every other direction.

Run after the migrations, with the certificate directory available:

    python scripts/register_certificates.py --certs-dir certs

Idempotent: re-running after a rotation updates the recorded expiry. On AWS the
equivalent is reading `aws iot list-certificates` and the thing attributes;
`certificate_cn` exists separately from `device_id` precisely so a mismatch
introduced by a bad provisioning run is visible rather than assumed away.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SUBJECT_CN_RE = re.compile(r"CN\s*=\s*([^,/\n]+)")


def read_certificate(path: Path) -> tuple[str, datetime] | None:
    """Return the common name and expiry of a certificate, or None if unreadable.

    Shells out to openssl rather than taking a dependency on `cryptography`.
    The tools image already has the openssl binary because the PKI scripts need
    it, and adding a C-extension dependency to parse two fields would be a poor
    trade.
    """
    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(path),
                "-noout",
                "-subject",
                "-enddate",
                "-nameopt",
                "compat",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  cannot read {path.name}: {exc}", file=sys.stderr)
        return None

    common_name: str | None = None
    expires: datetime | None = None

    for line in result.stdout.splitlines():
        if line.startswith("subject="):
            match = _SUBJECT_CN_RE.search(line)
            if match:
                common_name = match.group(1).strip()
        elif line.startswith("notAfter="):
            raw = line.split("=", 1)[1].strip()
            # openssl prints "Aug 22 08:20:33 2027 GMT".
            expires = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)

    if common_name is None or expires is None:
        print(f"  {path.name}: no common name or expiry found", file=sys.stderr)
        return None

    return common_name, expires


def discover(certs_dir: Path) -> list[tuple[str, Path]]:
    """Find each device's current certificate.

    Only the top-level file per device directory. A rotation leaves the outgoing
    certificate under previous/, and recording that one would report an expiry
    the device has already moved off.
    """
    devices_dir = certs_dir / "devices"
    if not devices_dir.is_dir():
        return []

    found: list[tuple[str, Path]] = []
    for device_dir in sorted(devices_dir.iterdir()):
        if not device_dir.is_dir():
            continue
        certificate = device_dir / f"{device_dir.name}.crt"
        if certificate.is_file():
            found.append((device_dir.name, certificate))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--certs-dir", type=Path, default=ROOT / "certs")
    parser.add_argument("--dsn", default=None, help="Defaults to the PG_* environment settings")
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Exit zero when no certificates are found, rather than failing",
    )
    args = parser.parse_args(argv)

    import psycopg

    dsn = args.dsn
    if dsn is None:
        from telemetry_common.settings import PostgresSettings

        dsn = PostgresSettings().dsn

    certificates = discover(args.certs_dir)
    if not certificates:
        message = f"no device certificates under {args.certs_dir}/devices"
        if args.allow_missing:
            print(message)
            return 0
        print(message, file=sys.stderr)
        return 1

    now = datetime.now(UTC)
    recorded = 0
    mismatched: list[str] = []
    expiries: list[datetime] = []

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for device_id, path in certificates:
                parsed = read_certificate(path)
                if parsed is None:
                    continue
                common_name, expires = parsed
                expiries.append(expires)

                if common_name != device_id:
                    # Not fatal, but it means the broker will authenticate this
                    # connection as something other than the directory suggests,
                    # and the platform will dead-letter everything it sends.
                    mismatched.append(f"{device_id} holds a certificate for {common_name}")

                # The device row may not exist yet: certificates are issued
                # before a device has ever published. ON CONFLICT covers both.
                # COALESCE on site_id keeps whatever the stream has already
                # learned rather than overwriting it with a placeholder.
                cur.execute(
                    """
                    INSERT INTO devices (
                        device_id, site_id, certificate_cn,
                        certificate_expires_at, provisioned_at
                    )
                    VALUES (%s, 'unknown', %s, %s, %s)
                    ON CONFLICT (device_id) DO UPDATE SET
                        certificate_cn         = EXCLUDED.certificate_cn,
                        certificate_expires_at = EXCLUDED.certificate_expires_at,
                        -- Keep the first provisioning time. A re-run after a
                        -- rotation updates the expiry, not the date the device
                        -- was first brought into service.
                        provisioned_at         = COALESCE(
                            devices.provisioned_at, EXCLUDED.provisioned_at
                        )
                    """,
                    (device_id, common_name, expires, now),
                )
                recorded += 1
        conn.commit()

    print(f"recorded {recorded} certificate(s) from {args.certs_dir}/devices")

    if expiries:
        soonest = min(expiries)
        print(f"earliest expiry: {soonest:%Y-%m-%d} ({(soonest - now).days} days)")

    if mismatched:
        print("\nWARNING: certificate common name does not match the device id:", file=sys.stderr)
        for line in mismatched:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nThe broker authenticates by common name, so these devices will be "
            "dead-lettered for identity mismatch.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
