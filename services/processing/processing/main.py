"""Entry point for the stream processor."""

from __future__ import annotations

import argparse
import asyncio

from prometheus_client import start_http_server

from telemetry_common.logging import configure_logging, get_logger
from telemetry_common.settings import KafkaSettings, ObservabilitySettings, PostgresSettings

from .consumer import ProcessorConfig, run_processor
from .writer import TelemetryWriter

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry-processing",
        description="Consume the telemetry stream, write TimescaleDB, maintain live rollups.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--max-batch", type=int, default=2000)
    parser.add_argument("--batch-timeout-ms", type=int, default=250)
    parser.add_argument("--rollup-flush-interval", type=float, default=5.0)
    parser.add_argument("--metrics-port", type=int, default=None)
    parser.add_argument("--consumer-group", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    kafka = KafkaSettings()
    postgres = PostgresSettings()
    obs = ObservabilitySettings(service_name="processing")

    configure_logging(service="processing", level=obs.log_level, json_output=obs.log_json)
    start_http_server(args.metrics_port or obs.metrics_port)

    config = ProcessorConfig(
        bootstrap_servers=kafka.bootstrap_servers,
        telemetry_topic=kafka.telemetry_topic,
        status_topic=kafka.status_topic,
        dlq_topic=kafka.dlq_topic,
        consumer_group=args.consumer_group or f"{kafka.consumer_group}-processing",
        max_batch=args.max_batch,
        batch_timeout_ms=args.batch_timeout_ms,
        rollup_flush_interval_s=args.rollup_flush_interval,
    )

    writer = TelemetryWriter(postgres.dsn, min_size=postgres.min_pool, max_size=postgres.max_pool)

    stats = asyncio.run(run_processor(config, writer))
    log.info("processing finished", **stats.snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
