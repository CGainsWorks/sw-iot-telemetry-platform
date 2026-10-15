#!/usr/bin/env python3
"""Export TimescaleDB rollups to Parquet for the analytical layer.

The analytical copy exists because TimescaleDB and BigQuery are good at
different things. TimescaleDB answers "what is this device doing now" in
milliseconds and deliberately throws away history: raw samples last 30 days,
minute rollups 180. BigQuery answers "how has the fleet behaved over three
years" and does not care that the question takes eight seconds.

Pointing analytical dashboards at TimescaleDB instead would put a table scan
across every chunk in the way of the queries the live dashboard depends on,
which is how a reporting request takes an operational system down.

Parquet is the interchange format because both BigQuery and DuckDB load it
natively, it is columnar so a query touching two of nine columns reads two, and
it carries its own schema so a column rename is a load error rather than a
silently misaligned column.

    python analytics/export/export.py --days 7 --out data/export
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# One query per BigQuery table. Column names and order match the JSON schemas in
# analytics/schemas exactly, because a load job matches by position when the
# Parquet has no matching names and by name when it does; keeping them identical
# removes the question.
EXPORTS: dict[str, str] = {
    "telemetry_hourly": """
        SELECT
            bucket,
            device_id,
            site_id,
            metric,
            avg_value,
            min_value,
            max_value,
            sample_count::bigint AS sample_count,
            now() AS exported_at
        FROM telemetry_1h
        WHERE bucket >= $1 AND bucket < $2
        ORDER BY bucket, device_id, metric
    """,
    "device_dim": """
        SELECT
            CURRENT_DATE AS snapshot_date,
            device_id,
            site_id,
            profile,
            fw_version,
            schema_version,
            first_seen,
            last_seen,
            certificate_expires_at
        FROM devices
        ORDER BY device_id
    """,
    "alert_history": """
        SELECT
            alert_id::text AS alert_id,
            rule_id,
            device_id,
            site_id,
            metric,
            severity,
            value,
            threshold,
            fired_at,
            resolved_at,
            notification_count::bigint AS notification_count
        FROM alerts
        WHERE fired_at >= $1 AND fired_at < $2
        ORDER BY fired_at
    """,
}

# device_dim is a full snapshot rather than a window, so it takes no bounds.
UNBOUNDED = {"device_dim"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dsn",
        default="postgresql://telemetry:telemetry@localhost:5432/telemetry",
        help="TimescaleDB connection string",
    )
    parser.add_argument("--days", type=int, default=7, help="How many days back to export")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "data" / "export",
        help="Directory to write Parquet files into",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=None,
        help=f"Subset to export. Default is all: {', '.join(EXPORTS)}",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit zero even if a table exported no rows",
    )
    return parser


def export(dsn: str, days: int, out: Path, tables: list[str] | None, allow_empty: bool) -> int:
    try:
        import psycopg
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        print(
            f"missing dependency: {exc.name}\n"
            "Install the analytics extras:  pip install -r analytics/requirements.txt",
            file=sys.stderr,
        )
        return 2

    selected = tables or list(EXPORTS)
    unknown = set(selected) - set(EXPORTS)
    if unknown:
        print(f"unknown table(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    out.mkdir(parents=True, exist_ok=True)

    empty: list[str] = []

    with psycopg.connect(dsn) as conn:
        for name in selected:
            sql = EXPORTS[name]
            with conn.cursor() as cur:
                if name in UNBOUNDED:
                    cur.execute(sql.replace("$1", "%s").replace("$2", "%s"))
                else:
                    cur.execute(sql.replace("$1", "%s").replace("$2", "%s"), (start, end))

                columns = [description.name for description in cur.description]
                rows = cur.fetchall()

            if not rows:
                empty.append(name)
                print(f"{name}: 0 rows, skipped")
                continue

            # Column-major, because that is what Arrow wants and what Parquet
            # stores. Transposing here avoids Arrow inferring a type per row.
            table = pa.table({column: [row[i] for row in rows] for i, column in enumerate(columns)})

            # snappy rather than the gzip the archive uses: this file is read
            # once by a load job, so decompression speed beats size.
            path = out / f"{name}.parquet"
            pq.write_table(table, path, compression="snappy")
            print(f"{name}: {len(rows):,} rows -> {path.relative_to(REPO_ROOT).as_posix()}")

    if empty and not allow_empty:
        print(
            f"\nno rows exported for: {', '.join(empty)}\n"
            "An empty export usually means the window is wrong or the stack has "
            "not been running long enough. Pass --allow-empty to accept it.",
            file=sys.stderr,
        )
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return export(args.dsn, args.days, args.out, args.tables, args.allow_empty)


if __name__ == "__main__":
    raise SystemExit(main())
