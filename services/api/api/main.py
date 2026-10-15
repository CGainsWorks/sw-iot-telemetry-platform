"""Uvicorn entry point for the query API."""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telemetry-api",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    uvicorn.run(
        "api.app:app",
        host=args.host,
        port=args.port,
        # Each worker runs its own Kafka consumer and its own connection pool, so
        # more than one is a real cost. Scale with replicas instead, which is
        # what the Kubernetes manifests do.
        workers=args.workers if not args.reload else 1,
        reload=args.reload,
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
