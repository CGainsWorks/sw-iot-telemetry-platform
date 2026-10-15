#!/usr/bin/env python3
"""Throughput and latency benchmark for the whole pipeline.

Every quantitative claim in the README comes out of this script. Run it and you
get the same table, on your hardware.

What is measured
----------------

**Throughput.** Messages published per second, and the readings per second that
actually land in TimescaleDB. The second number is the one that matters: a
broker will happily accept more than the rest of the stack can absorb, and the
gap between the two is where data goes missing.

**Device-to-ingest lag.** ``ingest_ts - ts``, straight out of the stored rows.
Exact, because both stamps are recorded, and it covers the MQTT hop and the
validation step.

**End-to-end latency.** Publish to queryable, measured with probe messages. A
probe is published with a marker value, then the database is polled until the
row appears. Polling bounds the resolution to the poll interval, 20 ms by
default, which is small against the hundreds of milliseconds the batching
introduces. It is measured this way rather than by adding a ``stored_at`` column
because that column would cost eight bytes on every row of the largest table in
the system, forever, to answer a question that is only asked during a benchmark.

**Loss.** Published minus stored. Should be zero at QoS 1; anything else is a
finding, not a rounding error.

Usage
-----

    docker compose up -d --wait
    bash scripts/export_certs.sh
    python bench/benchmark.py --devices 50 --rate 20 --duration 60

The load generator uses the ``bench`` service certificate, which the broker ACL
grants fleet-wide publish rights. Real devices cannot do this, which is the
point of the ACL; the benchmark is explicitly a privileged client.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import ssl
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiomqtt
import asyncpg

from telemetry_common.runtime import use_compatible_event_loop

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CERTS = REPO_ROOT / "certs"
RESULTS_DIR = REPO_ROOT / "bench" / "results"

#: Metric the probes use. Real devices never report it, so a probe row cannot be
#: confused with fleet traffic.
PROBE_METRIC = "bench_probe_v"


@dataclass
class BenchConfig:
    host: str = "localhost"
    port: int = 8883
    tenant: str = "acme"
    certs_dir: Path = DEFAULT_CERTS
    identity: str = "bench"
    dsn: str = "postgresql://telemetry:telemetry@localhost:5432/telemetry"

    devices: int = 50
    #: Messages per second per device.
    rate_hz: float = 20.0
    duration_s: float = 60.0
    warmup_s: float = 5.0
    #: How long to wait after the load stops for the pipeline to drain.
    drain_s: float = 20.0
    qos: int = 1
    readings_per_message: int = 5

    probe_interval_s: float = 0.5
    probe_poll_s: float = 0.02
    probe_timeout_s: float = 30.0

    #: Publisher connections. Fewer connections than devices is realistic for a
    #: load generator and avoids the benchmark measuring its own TLS handshakes.
    connections: int = 10

    label: str = "default"


@dataclass
class Percentiles:
    p50: float
    p95: float
    p99: float
    minimum: float
    maximum: float
    mean: float
    count: int

    @classmethod
    def of(cls, values: list[float]) -> Percentiles | None:
        if not values:
            return None
        ordered = sorted(values)

        def pick(fraction: float) -> float:
            # Nearest-rank. Exact and obvious, which matters more here than the
            # interpolation a statistics library would do.
            index = min(len(ordered) - 1, max(0, int(round(fraction * len(ordered))) - 1))
            return ordered[index]

        return cls(
            p50=round(pick(0.50), 2),
            p95=round(pick(0.95), 2),
            p99=round(pick(0.99), 2),
            minimum=round(ordered[0], 2),
            maximum=round(ordered[-1], 2),
            mean=round(statistics.fmean(ordered), 2),
            count=len(ordered),
        )


@dataclass
class BenchResult:
    label: str
    started_at: str
    config: dict
    duration_s: float
    messages_published: int
    messages_failed: int
    readings_published: int
    readings_stored: int
    publish_rate_hz: float
    stored_rate_hz: float
    loss_fraction: float
    device_to_ingest_ms: dict | None
    end_to_end_ms: dict | None
    dead_letters: int
    notes: list[str] = field(default_factory=list)


def tls_params(certs_dir: Path, identity: str) -> aiomqtt.TLSParameters:
    ca = certs_dir / "ca" / "ca.crt"
    cert = certs_dir / "services" / f"{identity}.crt"
    key = certs_dir / "services" / f"{identity}.key"
    for path in (ca, cert, key):
        if not path.exists():
            raise SystemExit(
                f"missing {path}\n"
                "Run the stack and export the certificates first:\n"
                "  docker compose up -d --wait\n"
                "  bash scripts/export_certs.sh"
            )
    return aiomqtt.TLSParameters(
        ca_certs=str(ca), certfile=str(cert), keyfile=str(key), cert_reqs=ssl.CERT_REQUIRED
    )


def build_payload(
    device_id: str,
    site_id: str,
    seq: int,
    readings: int,
    *,
    ts: datetime,
    probe: bool = False,
) -> bytes:
    values = {
        "temperature_c": 20.0 + (seq % 100) / 100.0,
        "humidity_pct": 45.0 + (seq % 50) / 10.0,
        "pressure_hpa": 1013.0 + (seq % 20) / 10.0,
        "battery_v": 3.9,
        "rssi_dbm": -60.0,
    }
    selected = dict(list(values.items())[:readings])
    if probe:
        selected = {PROBE_METRIC: float(seq)}

    return json.dumps(
        {
            "schema_version": "1.1.0",
            "device_id": device_id,
            "site_id": site_id,
            "seq": seq,
            "ts": ts.isoformat(),
            "fw_version": "1.4.2",
            "uptime_s": seq,
            "status": "ok",
            "readings": selected,
        }
    ).encode()


class Publisher:
    """One MQTT connection driving a slice of the synthetic fleet."""

    def __init__(self, config: BenchConfig, device_ids: list[str], index: int) -> None:
        self.config = config
        self.device_ids = device_ids
        self.index = index
        self.published = 0
        self.failed = 0
        # Sequence numbers bound the measurement window instead of timestamps.
        # Each connection owns a disjoint slice of the fleet and its seq counter
        # only increases, so [first, last] names exactly the messages counted,
        # with no clock and no boundary to get wrong.
        self.first_counted_seq: int | None = None
        self.last_counted_seq: int | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, counting: asyncio.Event) -> None:
        cfg = self.config
        tls = tls_params(cfg.certs_dir, cfg.identity)
        # Total offered rate for this connection's share of the fleet.
        interval = 1.0 / (cfg.rate_hz * len(self.device_ids))

        async with aiomqtt.Client(
            hostname=cfg.host,
            port=cfg.port,
            identifier=f"{cfg.identity}-{self.index}",
            tls_params=tls,
            keepalive=60,
        ) as client:
            seq = 0
            next_at = time.perf_counter()
            while not self._stop.is_set():
                device_id = self.device_ids[seq % len(self.device_ids)]
                site_id = f"site-{'abc'[seq % 3]}"
                topic = f"dt/{cfg.tenant}/{site_id}/{device_id}/telemetry"

                # The counting decision is taken before the timestamp is stamped,
                # and window_start is recorded before counting is set. That
                # ordering guarantees every counted message carries a ts inside
                # the measurement window, so "published" and the rows the window
                # query returns are exactly comparable. Deciding afterwards
                # instead lets a message stamped just before the window open be
                # counted as published while its row falls outside the query,
                # which shows up as loss that never happened.
                counted = counting.is_set()
                stamped = datetime.now(UTC)

                try:
                    await client.publish(
                        topic,
                        build_payload(
                            device_id, site_id, seq, cfg.readings_per_message, ts=stamped
                        ),
                        qos=cfg.qos,
                    )
                    if counted:
                        self.published += 1
                        if self.first_counted_seq is None:
                            self.first_counted_seq = seq
                        self.last_counted_seq = seq
                except Exception:
                    self.failed += 1

                seq += 1
                next_at += interval
                delay = next_at - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # Behind the offered rate. Reset the schedule rather than
                    # accumulating debt and then bursting to catch up, which
                    # would measure the burst instead of the steady state.
                    next_at = time.perf_counter()


class Probe:
    """Measures publish-to-queryable latency on a sample of messages."""

    def __init__(self, config: BenchConfig, pool: asyncpg.Pool) -> None:
        self.config = config
        self.pool = pool
        self.latencies_ms: list[float] = []
        self.timeouts = 0
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        cfg = self.config
        tls = tls_params(cfg.certs_dir, cfg.identity)
        device_id = "bench-probe"
        site_id = "site-a"
        topic = f"dt/{cfg.tenant}/{site_id}/{device_id}/telemetry"

        async with aiomqtt.Client(
            hostname=cfg.host,
            port=cfg.port,
            identifier=f"{cfg.identity}-probe",
            tls_params=tls,
            keepalive=60,
        ) as client:
            seq = 1
            while not self._stop.is_set():
                await client.publish(
                    topic,
                    build_payload(device_id, site_id, seq, 1, ts=datetime.now(UTC), probe=True),
                    qos=cfg.qos,
                )

                latency = await self._await_row(seq)
                if latency is None:
                    self.timeouts += 1
                else:
                    self.latencies_ms.append(latency)

                seq += 1
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=cfg.probe_interval_s)

    async def _await_row(self, seq: int) -> float | None:
        cfg = self.config
        started = time.perf_counter()
        deadline = started + cfg.probe_timeout_s

        while time.perf_counter() < deadline:
            if self._stop.is_set():
                return None
            found = await self.pool.fetchval(
                "SELECT 1 FROM telemetry WHERE metric = $1 AND value = $2 "
                "AND ts > now() - INTERVAL '5 minutes' LIMIT 1",
                PROBE_METRIC,
                float(seq),
            )
            if found:
                return (time.perf_counter() - started) * 1000.0
            await asyncio.sleep(cfg.probe_poll_s)
        return None


async def run_benchmark(config: BenchConfig) -> BenchResult:
    pool = await asyncpg.create_pool(config.dsn, min_size=2, max_size=6)
    notes: list[str] = []

    try:
        # Sequence ranges repeat between runs, so rows left behind by a previous
        # run would be counted by this one. Synthetic load data, safe to drop.
        await _purge_bench_rows(pool)

        device_ids = [f"bench-{i:06d}" for i in range(1, config.devices + 1)]
        connections = max(1, min(config.connections, config.devices))
        slices = [device_ids[i::connections] for i in range(connections)]

        publishers = [Publisher(config, slice_, i) for i, slice_ in enumerate(slices) if slice_]
        probe = Probe(config, pool)
        counting = asyncio.Event()

        print(
            f"warming up for {config.warmup_s:.0f}s "
            f"({config.devices} devices, {config.rate_hz:g} Hz each, "
            f"{connections} connections)",
            flush=True,
        )

        tasks = [asyncio.create_task(p.run(counting), name=f"pub-{p.index}") for p in publishers]
        await asyncio.sleep(config.warmup_s)

        # The measurement window opens here. Everything before it was warm-up:
        # TLS handshakes, connection pool growth and JIT-free but still cold
        # Python code paths.
        window_start = datetime.now(UTC)
        counting.set()
        probe_task = asyncio.create_task(probe.run(), name="probe")

        print(f"measuring for {config.duration_s:.0f}s", flush=True)
        started = time.perf_counter()
        await asyncio.sleep(config.duration_s)
        measured_s = time.perf_counter() - started

        for publisher in publishers:
            publisher.stop()
        probe.stop()

        # window_end is taken after the publishers have actually finished, not
        # when they were asked to stop. A publisher mid-iteration when stop was
        # requested still stamps and sends one more message, and that message
        # has to fall inside the window it was counted in.
        await asyncio.gather(*tasks, return_exceptions=True)
        window_end = datetime.now(UTC)

        probe_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await probe_task

        print(f"draining for {config.drain_s:.0f}s", flush=True)
        await asyncio.sleep(config.drain_s)

        published = sum(p.published for p in publishers)
        failed = sum(p.failed for p in publishers)
        readings_published = published * config.readings_per_message

        # Count exactly the messages that were counted as published: this
        # connection's devices, within the sequence range it recorded. Bounding
        # by timestamp instead leaves a message stamped either side of the window
        # edge to be counted in one place and not the other, which shows up as
        # loss or as more rows stored than were ever sent.
        stored = 0
        lags: list[float] = []
        for publisher in publishers:
            if publisher.first_counted_seq is None:
                continue
            rows = await pool.fetch(
                """
                SELECT EXTRACT(EPOCH FROM (ingest_ts - ts)) * 1000.0 AS lag_ms
                FROM telemetry
                WHERE device_id = ANY($1::text[])
                  AND metric <> $2
                  AND seq BETWEEN $3 AND $4
                  AND ts > now() - INTERVAL '2 hours'
                """,
                publisher.device_ids,
                PROBE_METRIC,
                publisher.first_counted_seq,
                publisher.last_counted_seq,
            )
            stored += len(rows)
            lags.extend(float(row["lag_ms"]) for row in rows)

        # Only the load generator's own rejections. The simulated fleet is
        # running alongside and deliberately injects faults, so counting every
        # dead letter would attribute the fleet's rejections to the benchmark.
        dead_letters = await pool.fetchval(
            "SELECT count(*) FROM dead_letters "
            "WHERE received_at >= $1 AND received_at < $2 AND topic LIKE '%%/bench-%%'",
            window_start,
            window_end + timedelta(seconds=config.drain_s),
        )

        if failed:
            notes.append(f"{failed} publishes failed at the broker")
        if probe.timeouts:
            notes.append(
                f"{probe.timeouts} probes never became queryable within "
                f"{config.probe_timeout_s:.0f}s"
            )
        if dead_letters:
            notes.append(
                f"{dead_letters} load-generator messages were rejected by ingest and "
                "are counted as loss"
            )
        loss = (
            0.0
            if readings_published == 0
            else max(0.0, (readings_published - (stored or 0)) / readings_published)
        )
        if loss > 0.01:
            notes.append(
                f"{loss:.1%} of published readings did not reach storage. "
                "The stack could not keep up with the offered rate."
            )

        return BenchResult(
            label=config.label,
            started_at=window_start.isoformat(),
            config={
                **{k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(config).items()},
                "connections_used": connections,
            },
            duration_s=round(measured_s, 2),
            messages_published=published,
            messages_failed=failed,
            readings_published=readings_published,
            readings_stored=stored or 0,
            publish_rate_hz=round(published / measured_s, 1),
            stored_rate_hz=round((stored or 0) / measured_s, 1),
            loss_fraction=round(loss, 5),
            device_to_ingest_ms=asdict(p) if (p := Percentiles.of(lags)) else None,
            end_to_end_ms=asdict(q) if (q := Percentiles.of(probe.latencies_ms)) else None,
            dead_letters=dead_letters or 0,
            notes=notes,
        )

    finally:
        # Load-generator rows would otherwise sit alongside the fleet's data and
        # fill the dashboard with devices that do not exist.
        with contextlib.suppress(Exception):
            await _purge_bench_rows(pool)
        await pool.close()


async def _purge_bench_rows(pool: asyncpg.Pool) -> None:
    """Remove everything the load generator has ever written."""
    await pool.execute("DELETE FROM telemetry WHERE device_id LIKE 'bench-%'")
    await pool.execute("DELETE FROM telemetry_1m_live WHERE device_id LIKE 'bench-%'")
    await pool.execute("DELETE FROM alerts WHERE device_id LIKE 'bench-%'")
    await pool.execute("DELETE FROM device_events WHERE device_id LIKE 'bench-%'")
    await pool.execute("DELETE FROM devices WHERE device_id LIKE 'bench-%'")


def render_markdown(result: BenchResult) -> str:
    lines = [
        f"### Benchmark: {result.label}",
        "",
        f"Run at {result.started_at} for {result.duration_s:.0f}s, "
        f"{result.config['devices']} devices at {result.config['rate_hz']:g} Hz each.",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Messages published | {result.messages_published:,} |",
        f"| Readings published | {result.readings_published:,} |",
        f"| Readings stored | {result.readings_stored:,} |",
        f"| Publish rate | {result.publish_rate_hz:,.1f} msg/s |",
        f"| Storage rate | {result.stored_rate_hz:,.1f} readings/s |",
        f"| Loss | {result.loss_fraction:.3%} |",
        f"| Dead letters | {result.dead_letters} |",
    ]

    if result.device_to_ingest_ms:
        lag = result.device_to_ingest_ms
        lines.append(
            f"| Device to ingest | p50 {lag['p50']:.0f} ms, p95 {lag['p95']:.0f} ms, "
            f"p99 {lag['p99']:.0f} ms |"
        )
    if result.end_to_end_ms:
        e2e = result.end_to_end_ms
        lines.append(
            f"| Publish to queryable | p50 {e2e['p50']:.0f} ms, p95 {e2e['p95']:.0f} ms, "
            f"p99 {e2e['p99']:.0f} ms |"
        )

    if result.notes:
        lines.extend(["", "Notes:", ""])
        lines.extend(f"- {note}" for note in result.notes)

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "8883")))
    parser.add_argument("--certs-dir", type=Path, default=DEFAULT_CERTS)
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "PG_DSN", "postgresql://telemetry:telemetry@localhost:5432/telemetry"
        ),
    )
    parser.add_argument("--devices", type=int, default=50)
    parser.add_argument("--rate", type=float, default=20.0, help="Messages per second per device")
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--drain", type=float, default=20.0)
    parser.add_argument("--connections", type=int, default=10)
    parser.add_argument("--readings", type=int, default=5, choices=range(1, 6))
    parser.add_argument("--qos", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--probe-interval", type=float, default=0.5)
    parser.add_argument("--label", default="default")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write the JSON result. Defaults to bench/results/<label>.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Must happen before asyncio.run: aiomqtt needs a loop with socket readers.
    use_compatible_event_loop()

    config = BenchConfig(
        host=args.host,
        port=args.port,
        certs_dir=args.certs_dir,
        dsn=args.dsn,
        devices=args.devices,
        rate_hz=args.rate,
        duration_s=args.duration,
        warmup_s=args.warmup,
        drain_s=args.drain,
        connections=args.connections,
        readings_per_message=args.readings,
        qos=args.qos,
        probe_interval_s=args.probe_interval,
        label=args.label,
    )

    result = asyncio.run(run_benchmark(config))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or (RESULTS_DIR / f"{config.label}.json")
    out.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8", newline="")

    markdown = render_markdown(result)
    (out.with_suffix(".md")).write_text(markdown, encoding="utf-8", newline="")

    print()
    print(markdown)
    print(f"written to {out.relative_to(REPO_ROOT).as_posix()}")

    # A run that stored nothing is a failed benchmark, not a benchmark showing
    # zero. Exiting non-zero stops a broken stack being published as a result.
    if result.readings_stored == 0:
        print("ERROR: nothing reached storage", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
