"""Entry point for the alerting service."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import asyncpg
from prometheus_client import start_http_server

from telemetry_common.logging import configure_logging, get_logger
from telemetry_common.settings import KafkaSettings, ObservabilitySettings, PostgresSettings

from .engine import EngineConfig, run_engine
from .notifiers import LogNotifier, Notifier, StreamNotifier, WebhookNotifier
from .rules import RuleError, load_rules

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-alerting",
        description="Evaluate the alert rule catalogue against the live rollups.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path(os.environ.get("ALERT_RULES_PATH", "config/alert_rules.yaml")),
    )
    parser.add_argument("--interval", type=float, default=15.0, help="Seconds between cycles")
    parser.add_argument(
        "--reload-interval",
        type=float,
        default=60.0,
        help="Seconds between rule file reloads. 0 disables reloading.",
    )
    parser.add_argument("--webhook-url", default=os.environ.get("ALERT_WEBHOOK_URL"))
    parser.add_argument("--metrics-port", type=int, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the rule catalogue and exit. Used by CI and by pre-commit.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    postgres = PostgresSettings()
    kafka = KafkaSettings()

    rules = load_rules(args.rules)

    pool = await asyncpg.create_pool(
        postgres.dsn, min_size=1, max_size=postgres.max_pool, command_timeout=30.0
    )

    stream = StreamNotifier(bootstrap_servers=kafka.bootstrap_servers, topic=kafka.alerts_topic)
    await stream.start()

    notifiers: dict[str, Notifier] = {
        "log": LogNotifier(),
        "webhook": WebhookNotifier(args.webhook_url),
        "stream": stream,
    }

    try:
        stats = await run_engine(
            pool=pool,
            rules=rules,
            notifiers=notifiers,
            config=EngineConfig(interval_s=args.interval, reload_interval_s=args.reload_interval),
            reload_rules=lambda: load_rules(args.rules),
        )
        log.info("alerting finished", **stats.snapshot())
    finally:
        await pool.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    obs = ObservabilitySettings(service_name="alerting")
    configure_logging(service="alerting", level=obs.log_level, json_output=obs.log_json)

    if args.check:
        try:
            rules = load_rules(args.rules)
        except RuleError as exc:
            print(f"invalid rule catalogue: {exc}")
            return 1
        enabled = sum(1 for rule in rules if rule.enabled)
        print(f"{len(rules)} rules loaded, {enabled} enabled, from {args.rules}")
        for rule in rules:
            state = "on " if rule.enabled else "off"
            print(
                f"  [{state}] {rule.rule_id:<24} {rule.severity.value:<8} {rule.describe_condition()}"
            )
        return 0

    start_http_server(args.metrics_port or obs.metrics_port)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
