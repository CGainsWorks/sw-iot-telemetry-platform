#!/usr/bin/env python3
"""Run the analytical queries locally against DuckDB.

This is what makes the BigQuery layer testable. The queries are written once
against the macro vocabulary in ``analytics/dialect.py`` and rendered into
either dialect; here they are rendered for DuckDB and run over the Parquet the
exporter produced. A syntax error, a column that does not exist or a join that
returns nothing is caught in the repository rather than discovered against a
billed warehouse.

    python analytics/export/export.py --days 7 --out data/export
    python analytics/local/run_queries.py --data data/export

With ``--fixtures`` it generates its own data instead, so CI can run every query
without the stack being up.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "analytics"))

from dialect import Dialect, load, query_names, render  # noqa: E402

TABLES = ("telemetry_hourly", "device_dim", "alert_history")


def register_parquet(connection, data_dir: Path) -> list[str]:
    """Expose each Parquet file as a view named after its table."""
    registered: list[str] = []
    for table in TABLES:
        path = data_dir / f"{table}.parquet"
        if not path.exists():
            continue
        connection.execute(
            f"CREATE OR REPLACE VIEW {table} AS SELECT * FROM read_parquet(?)",
            [str(path)],
        )
        registered.append(table)
    return registered


def build_fixtures(connection, *, seed: int = 7) -> None:
    """Generate a small synthetic warehouse in memory.

    Deliberately shaped to exercise the queries rather than to look realistic:
    it contains a site with a drifting sensor so the outlier query returns
    something, a device with gaps so the completeness query does, and a mix of
    firmware versions so the rollout query does. A fixture that returns no rows
    tests only that the SQL parses.
    """
    rng = random.Random(seed)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)

    sites = {"site-a": 4, "site-b": 4, "site-c": 3}
    metrics = ("temperature_c", "humidity_pct", "battery_v")

    telemetry: list[tuple] = []
    devices: list[tuple] = []
    alerts: list[tuple] = []

    device_number = 0
    for site, count in sites.items():
        for _ in range(count):
            device_number += 1
            device_id = f"esp32-{device_number:06d}"
            # One device per site reads consistently high: the drifting sensor
            # the outlier query is meant to find.
            drift = 4.5 if device_number % 4 == 0 else 0.0
            # A quarter of the fleet is on the older firmware line.
            fw = "1.3.0" if device_number % 4 == 1 else "1.4.2"
            schema = "1.0.0" if fw == "1.3.0" else "1.1.0"

            devices.append(
                (
                    now.date(),
                    device_id,
                    site,
                    "environment",
                    fw,
                    schema,
                    now - timedelta(days=200),
                    now - timedelta(minutes=rng.randint(0, 30)),
                    now + timedelta(days=rng.choice([12, 200, 300])),
                )
            )

            for hours_ago in range(24 * 20):
                bucket = now - timedelta(hours=hours_ago)
                # Every fifth device loses a stretch of hours, so the
                # completeness query has something to report.
                if device_number % 5 == 0 and 40 < hours_ago < 70:
                    continue
                for metric in metrics:
                    base = {"temperature_c": 19.0, "humidity_pct": 52.0, "battery_v": 3.9}[metric]
                    offset = drift if metric == "temperature_c" else 0.0
                    value = base + offset + rng.gauss(0, 0.4)
                    telemetry.append(
                        (
                            bucket,
                            device_id,
                            site,
                            metric,
                            round(value, 3),
                            round(value - 0.5, 3),
                            round(value + 0.5, 3),
                            360,
                            now,
                        )
                    )

    for index in range(60):
        fired = now - timedelta(hours=rng.randint(1, 24 * 25))
        # A spread of durations, including some very short ones so the
        # short-lived share in the reliability query is non-zero.
        duration = rng.choice([45, 90, 400, 1800, 7200, 20000])
        rule, severity = rng.choice(
            [
                ("high-temperature", "warning"),
                ("low-battery", "warning"),
                ("device-offline", "warning"),
                ("critical-temperature", "critical"),
            ]
        )
        alerts.append(
            (
                f"alert-{index:04d}",
                rule,
                f"esp32-{rng.randint(1, device_number):06d}",
                rng.choice(list(sites)),
                "temperature_c" if "temperature" in rule else None,
                severity,
                42.0,
                35.0,
                fired,
                fired + timedelta(seconds=duration),
                rng.randint(1, 3),
            )
        )

    connection.execute(
        """
        CREATE OR REPLACE TABLE telemetry_hourly (
            bucket TIMESTAMPTZ, device_id VARCHAR, site_id VARCHAR, metric VARCHAR,
            avg_value DOUBLE, min_value DOUBLE, max_value DOUBLE,
            sample_count BIGINT, exported_at TIMESTAMPTZ
        )
        """
    )
    connection.executemany(
        "INSERT INTO telemetry_hourly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", telemetry
    )

    connection.execute(
        """
        CREATE OR REPLACE TABLE device_dim (
            snapshot_date DATE, device_id VARCHAR, site_id VARCHAR, profile VARCHAR,
            fw_version VARCHAR, schema_version VARCHAR,
            first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ, certificate_expires_at TIMESTAMPTZ
        )
        """
    )
    connection.executemany("INSERT INTO device_dim VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", devices)

    connection.execute(
        """
        CREATE OR REPLACE TABLE alert_history (
            alert_id VARCHAR, rule_id VARCHAR, device_id VARCHAR, site_id VARCHAR,
            metric VARCHAR, severity VARCHAR, value DOUBLE, threshold DOUBLE,
            fired_at TIMESTAMPTZ, resolved_at TIMESTAMPTZ, notification_count BIGINT
        )
        """
    )
    connection.executemany(
        "INSERT INTO alert_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", alerts
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=REPO_ROOT / "data" / "export",
        help="Directory holding the exported Parquet files",
    )
    parser.add_argument(
        "--fixtures",
        action="store_true",
        help="Generate synthetic data instead of reading Parquet. Used by CI.",
    )
    parser.add_argument("--query", default=None, help="Run one query rather than all of them")
    parser.add_argument("--limit", type=int, default=8, help="Rows to print per query")
    parser.add_argument(
        "--require-rows",
        action="store_true",
        help="Fail if any query returns nothing. A query that returns no rows has "
        "only been proven to parse.",
    )
    parser.add_argument(
        "--print-sql", action="store_true", help="Print the rendered SQL before running it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import duckdb
    except ImportError:
        print(
            "duckdb is not installed.\n" "  pip install -r analytics/requirements.txt",
            file=sys.stderr,
        )
        return 2

    connection = duckdb.connect()

    if args.fixtures:
        build_fixtures(connection)
        print("using generated fixtures\n")
    else:
        registered = register_parquet(connection, args.data)
        if not registered:
            print(
                f"no Parquet files found in {args.data}\n"
                "Export first:  python analytics/export/export.py --days 7\n"
                "Or run against generated data:  --fixtures",
                file=sys.stderr,
            )
            return 2
        print(f"reading {args.data}: {', '.join(registered)}\n")

    names = [args.query] if args.query else query_names()
    failures: list[str] = []
    empty: list[str] = []

    for name in names:
        sql = render(load(name), Dialect.DUCKDB)
        if args.print_sql:
            print(sql)

        try:
            result = connection.execute(sql)
            rows = result.fetchall()
            columns = [description[0] for description in result.description]
        except Exception as exc:
            failures.append(name)
            print(f"FAIL  {name}\n      {exc}\n")
            continue

        print(f"OK    {name}  ({len(rows):,} rows)")
        if not rows:
            empty.append(name)
        else:
            width = max(len(column) for column in columns)
            print("      " + " | ".join(column.ljust(width) for column in columns))
            for row in rows[: args.limit]:
                print("      " + " | ".join(str(value).ljust(width)[:width] for value in row))
            if len(rows) > args.limit:
                print(f"      ... {len(rows) - args.limit:,} more")
        print()

    if failures:
        print(f"{len(failures)} query(ies) failed: {', '.join(failures)}", file=sys.stderr)
        return 1

    if empty and args.require_rows:
        print(
            f"{len(empty)} query(ies) returned no rows: {', '.join(empty)}\n"
            "A query returning nothing has only been shown to parse.",
            file=sys.stderr,
        )
        return 1

    print(f"{len(names)} query(ies) ran successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
