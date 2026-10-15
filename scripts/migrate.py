#!/usr/bin/env python3
"""Apply the SQL migrations in db/migrations, in order, exactly once.

Deliberately small. A migration framework would bring a dependency, a DSL and a
set of conventions to learn, and this platform has one database and a handful of
files. What it does need, and what a naive ``psql -f *.sql`` loop does not give,
is three things:

* a record of what has already run, so restarting the stack is safe;
* a checksum on each applied file, so an edit to a migration that has already
  run is an error rather than a silent divergence between environments;
* the ability to run a file outside a transaction, because
  ``CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)`` refuses to run
  inside one.

A file opts out of the transaction with ``-- migrate:no-transaction`` on any of
its first few lines.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "db" / "migrations"

NO_TRANSACTION_MARKER = "-- migrate:no-transaction"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    checksum    TEXT        NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INTEGER     NOT NULL
);
"""


def checksum(text: str) -> str:
    # Normalise line endings first. The repository is checked out with LF via
    # .gitattributes, but a Windows tool can still rewrite a file to CRLF, and a
    # checksum that changes with the line ending would fail every run afterwards.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def discover() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")
    return files


def wants_transaction(text: str) -> bool:
    head = "\n".join(text.splitlines()[:5])
    return NO_TRANSACTION_MARKER not in head


def wait_for_database(dsn: str, timeout_s: float) -> None:
    """Block until the server accepts a connection, or give up.

    Compose starts the migration container as soon as the database container is
    healthy, but "healthy" and "accepting queries on this database" are not the
    same instant on a first boot.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3):
                return
        except psycopg.Error as exc:
            last_error = exc
            time.sleep(1.0)
    raise SystemExit(f"database not reachable within {timeout_s:.0f}s: {last_error}")


def applied_versions(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {row[0]: row[1] for row in cur.fetchall()}


def apply_one(dsn: str, path: Path, version: str, digest: str) -> None:
    text = path.read_text(encoding="utf-8")
    started = time.monotonic()

    if wants_transaction(text):
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(text)
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum, duration_ms) "
                    "VALUES (%s, %s, %s)",
                    (version, digest, int((time.monotonic() - started) * 1000)),
                )
            conn.commit()
        return

    # Outside a transaction. If a statement fails partway the file is not
    # recorded as applied, so a rerun repeats it. Every statement in a
    # no-transaction migration is therefore written to be idempotent.
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(text)
            cur.execute(
                "INSERT INTO schema_migrations (version, checksum, duration_ms) "
                "VALUES (%s, %s, %s)",
                (version, digest, int((time.monotonic() - started) * 1000)),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN. Defaults to the PG_* environment settings.",
    )
    parser.add_argument("--wait", type=float, default=60.0, help="Seconds to wait for the server")
    parser.add_argument(
        "--status", action="store_true", help="List migrations and their state, apply nothing"
    )
    args = parser.parse_args()

    dsn = args.dsn
    if dsn is None:
        from telemetry_common.settings import PostgresSettings

        dsn = PostgresSettings().dsn

    wait_for_database(dsn, args.wait)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
        conn.commit()
        already = applied_versions(conn)

    pending: list[tuple[Path, str, str]] = []
    for path in discover():
        version = path.stem
        digest = checksum(path.read_text(encoding="utf-8"))
        recorded = already.get(version)
        if recorded is None:
            pending.append((path, version, digest))
        elif recorded != digest:
            print(
                f"ERROR: {version} has already been applied but its contents have changed.\n"
                f"  recorded checksum {recorded}\n"
                f"  current checksum  {digest}\n"
                "Migrations are immutable once applied. Add a new file instead.",
                file=sys.stderr,
            )
            return 1

    if args.status:
        for path in discover():
            state = "applied" if path.stem in already else "pending"
            print(f"{state:>8}  {path.name}")
        return 0

    if not pending:
        print(f"database is up to date ({len(already)} migrations applied)")
        return 0

    for path, version, digest in pending:
        print(f"applying {path.name} ...", flush=True)
        apply_one(dsn, path, version, digest)
        print(f"applied  {path.name}")

    print(f"{len(pending)} migration(s) applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
