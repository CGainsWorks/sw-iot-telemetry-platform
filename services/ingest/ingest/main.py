"""Entry point for the ingest bridge."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from prometheus_client import start_http_server

from telemetry_common.logging import configure_logging, get_logger
from telemetry_common.runtime import use_compatible_event_loop
from telemetry_common.settings import KafkaSettings, MqttSettings, ObservabilitySettings

from .bridge import BridgeConfig, run_bridge
from .sinks import KafkaSink, KinesisSink, StreamSink

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest-bridge",
        description="Bridge MQTT telemetry into the processing stream.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--sink",
        choices=("kafka", "kinesis"),
        default="kafka",
        help="kafka for the local and Kubernetes stack, kinesis when AWS IoT Core "
        "rules are not doing the routing",
    )
    parser.add_argument("--certs-dir", type=Path, default=None)
    parser.add_argument("--identity", default="ingest-bridge")
    parser.add_argument("--kinesis-stream", default="telemetry-normalised")
    parser.add_argument("--kinesis-dlq-stream", default="telemetry-deadletter")
    parser.add_argument("--aws-region", default=None)
    parser.add_argument(
        "--allow-implausible",
        action="store_true",
        help="Store readings outside the catalogue range instead of dead-lettering them",
    )
    parser.add_argument("--metrics-port", type=int, default=None)
    return parser


def build_sink(args: argparse.Namespace, kafka: KafkaSettings) -> StreamSink:
    if args.sink == "kinesis":
        return KinesisSink(
            stream_name=args.kinesis_stream,
            dlq_stream_name=args.kinesis_dlq_stream,
            region=args.aws_region,
        )
    return KafkaSink(
        bootstrap_servers=kafka.bootstrap_servers,
        telemetry_topic=kafka.telemetry_topic,
        status_topic=kafka.status_topic,
        dlq_topic=kafka.dlq_topic,
        linger_ms=kafka.linger_ms,
        compression=kafka.compression,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    mqtt = MqttSettings()
    kafka = KafkaSettings()
    obs = ObservabilitySettings(service_name="ingest")

    configure_logging(service="ingest", level=obs.log_level, json_output=obs.log_json)
    start_http_server(args.metrics_port or obs.metrics_port)
    use_compatible_event_loop()

    config = BridgeConfig(
        host=mqtt.host,
        port=mqtt.port,
        tenant=mqtt.tenant,
        certs_dir=args.certs_dir or Path(mqtt.ca_cert).parent.parent,
        identity=args.identity,
        qos=mqtt.qos,
        verify_hostname=mqtt.verify_hostname,
        reject_implausible=not args.allow_implausible,
    )

    stats = asyncio.run(run_bridge(config, build_sink(args, kafka)))
    log.info("ingest finished", **stats.snapshot())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
