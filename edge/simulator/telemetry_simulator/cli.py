"""Command line entry point for the device fleet simulator."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from telemetry_common.logging import configure_logging, get_logger
from telemetry_common.runtime import use_compatible_event_loop

from .faults import ALL_FAULTS, FaultKind
from .fleet import FleetConfig, device_id_for, run_fleet
from .profiles import PROFILES, FleetMix, site_for

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-simulator",
        description="Publish synthetic sensor telemetry over MQTT with per-device TLS identity.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="localhost", help="MQTT broker hostname")
    parser.add_argument("--port", type=int, default=8883, help="MQTT broker port")
    parser.add_argument("--tenant", default="acme", help="Tenant segment of the topic")
    parser.add_argument(
        "--certs-dir", type=Path, default=Path("certs"), help="Root of the device PKI"
    )
    parser.add_argument("--devices", type=int, default=10, help="Number of devices to simulate")
    parser.add_argument("--prefix", default="esp32", help="Device id prefix")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Seconds to run for. Omit to run until interrupted.",
    )
    parser.add_argument("--seed", type=int, default=1, help="Seed. Fixes the whole run.")
    parser.add_argument(
        "--fault-fraction",
        type=float,
        default=0.15,
        help="Share of the fleet that exhibits a fault",
    )
    parser.add_argument(
        "--faults",
        default="all",
        help="Comma-separated fault kinds, or 'all', or 'none'. "
        f"Choices: {', '.join(f.value for f in ALL_FAULTS)}",
    )
    parser.add_argument("--qos", type=int, default=1, choices=(0, 1, 2), help="Publish QoS")
    parser.add_argument(
        "--time-scale",
        type=float,
        default=1.0,
        help="Simulated physics seconds per real second. 60 ages a battery an hour "
        "a minute. Message timestamps stay on the real clock either way.",
    )
    parser.add_argument(
        "--warm-start-hours",
        type=float,
        default=0.0,
        help="Spread of prior device uptime, so batteries are not all full",
    )
    parser.add_argument(
        "--connect-stagger",
        type=float,
        default=5.0,
        help="Seconds to spread the initial connections over",
    )
    parser.add_argument(
        "--no-verify-hostname",
        action="store_true",
        help="Skip TLS hostname verification. Needed only if the broker certificate "
        "does not carry the name you are connecting to.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print one sample message per device to stdout and exit. No broker needed.",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--log-text", action="store_true", help="Human-readable logs instead of JSON"
    )
    return parser


def parse_faults(raw: str) -> tuple[FaultKind, ...]:
    value = raw.strip().lower()
    if value == "all":
        return ALL_FAULTS
    if value in ("none", ""):
        return ()
    kinds: list[FaultKind] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            kinds.append(FaultKind(token))
        except ValueError as exc:
            valid = ", ".join(f.value for f in ALL_FAULTS)
            raise SystemExit(f"unknown fault kind {token!r}. Valid kinds: {valid}") from exc
    return tuple(kinds)


def dry_run(args: argparse.Namespace) -> int:
    """Emit one message per device without touching a broker.

    Useful for eyeballing the schema, for seeding fixtures and for proving the
    simulator works before any infrastructure exists.
    """
    from .device import SimulatedDevice
    from .faults import plan_for

    mix = FleetMix()
    for index in range(1, args.devices + 1):
        profile = mix.assign(index - 1)
        device = SimulatedDevice(
            device_id=device_id_for(index, args.prefix),
            site_id=site_for(index - 1),
            profile=profile,
            fault_plan=plan_for(
                index,
                fraction=args.fault_fraction,
                kinds=parse_faults(args.faults),
                seed=args.seed,
            ),
            seed=args.seed * 100_003 + index,
        )
        message = device.sample(0.0)
        if message is None:
            continue
        print(json.dumps(message.model_dump(mode="json"), separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(service="simulator", level=args.log_level, json_output=not args.log_text)

    if args.devices < 1:
        raise SystemExit("--devices must be at least 1")
    if not 0.0 <= args.fault_fraction <= 1.0:
        raise SystemExit("--fault-fraction must be between 0 and 1")

    if args.dry_run:
        return dry_run(args)

    # Must happen before asyncio.run: aiomqtt needs a loop with socket readers.
    use_compatible_event_loop()

    config = FleetConfig(
        host=args.host,
        port=args.port,
        tenant=args.tenant,
        certs_dir=args.certs_dir,
        device_count=args.devices,
        device_prefix=args.prefix,
        duration_s=args.duration,
        seed=args.seed,
        fault_fraction=args.fault_fraction,
        fault_kinds=parse_faults(args.faults),
        qos=args.qos,
        time_scale=args.time_scale,
        warm_start_hours=args.warm_start_hours,
        connect_stagger_s=args.connect_stagger,
        verify_hostname=not args.no_verify_hostname,
    )

    try:
        stats = asyncio.run(run_fleet(config))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130

    # A run that published nothing is a failure, not a success. Exiting zero here
    # would let a broken broker or a missing certificate pass CI unnoticed.
    if stats.published == 0:
        log.error("fleet published no messages", **stats.snapshot())
        return 1
    return 0


def describe_profiles() -> str:
    lines = []
    for name, profile in PROFILES.items():
        lines.append(
            f"{name}: every {profile.interval_s}s, metrics={', '.join(profile.metrics)} "
            f"({profile.description})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
