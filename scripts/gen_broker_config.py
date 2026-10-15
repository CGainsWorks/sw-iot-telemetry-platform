#!/usr/bin/env python3
"""Generate the Mosquitto ACL file from the topic grammar.

The broker ACL and the topic helpers in ``telemetry_common.topics`` have to
agree exactly or a device is either locked out or over-privileged. Generating
one from the other removes the chance of them drifting. Run with ``--check`` in
CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from telemetry_common.topics import DEFAULT_TENANT, mosquitto_acl_patterns

ROOT = Path(__file__).resolve().parents[1]
ACL_PATH = ROOT / "config" / "mosquitto" / "acl"

# Platform-side identities. Each is a client certificate issued by scripts/pki.sh,
# so these names are certificate common names, not passwords.
SERVICE_GRANTS: dict[str, list[str]] = {
    "ingest-bridge": [
        "topic read dt/{tenant}/+/+/telemetry",
        "topic read dt/{tenant}/+/+/status",
        "topic read cmd/{tenant}/+/+/res",
    ],
    "command-dispatcher": [
        "topic write cmd/{tenant}/+/+/req",
        "topic read cmd/{tenant}/+/+/res",
    ],
    "bench": [
        "topic write dt/{tenant}/+/+/telemetry",
        "topic write dt/{tenant}/+/+/status",
        "topic read dt/{tenant}/+/+/telemetry",
    ],
    # Used only by the container healthcheck. Granted the broker's own statistics
    # topics and nothing else, so a successful check proves the full mutual-TLS
    # path works without handing the checker any fleet data.
    "broker-health": [
        "topic read $SYS/#",
    ],
}

HEADER = """# GENERATED FILE. Do not edit by hand.
# Regenerate with: python scripts/gen_broker_config.py
#
# Mosquitto is configured with use_identity_as_username, so %u below is the
# common name of the client certificate presented during the TLS handshake.
# A device therefore cannot publish under any identity but its own, which is the
# same guarantee AWS IoT Core gives through ${{iot:Connection.Thing.ThingName}}
# policy variables.
#
# Anonymous access is disabled in mosquitto.conf, so there is no unauthenticated
# path into these topics.
"""


def render(tenant: str = DEFAULT_TENANT) -> str:
    lines: list[str] = [HEADER.format()]

    lines.append("# --- Devices ------------------------------------------------------------")
    lines.append("# Pattern rules apply to every authenticated client and confine each one to")
    lines.append("# the topic space named after its own certificate.")
    lines.append("")
    lines.extend(mosquitto_acl_patterns(tenant))
    lines.append("")

    lines.append("# --- Platform services --------------------------------------------------")
    lines.append("# Fleet-wide grants, one block per service certificate.")
    for identity, grants in SERVICE_GRANTS.items():
        lines.append("")
        lines.append(f"user {identity}")
        for grant in grants:
            lines.append(grant.format(tenant=tenant))

    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    args = parser.parse_args()

    rendered = render(args.tenant)

    if args.check:
        current = ACL_PATH.read_text(encoding="utf-8") if ACL_PATH.exists() else ""
        if current != rendered:
            print(f"stale: {ACL_PATH.relative_to(ROOT).as_posix()}", file=sys.stderr)
            print("run: python scripts/gen_broker_config.py", file=sys.stderr)
            return 1
        print("broker ACL is up to date")
        return 0

    ACL_PATH.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the output LF on Windows too. The broker reads this
    # file inside a Linux container, where CRLF would corrupt every ACL line.
    ACL_PATH.write_text(rendered, encoding="utf-8", newline="")
    print(f"wrote {ACL_PATH.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
