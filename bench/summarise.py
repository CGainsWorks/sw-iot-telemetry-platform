#!/usr/bin/env python3
"""Condense benchmark result files into the committed summary.

    python bench/summarise.py laptop-50dev-10hz laptop-50dev-20hz laptop-100dev-25hz

Full results land in ``bench/results/`` and stay out of the repository: they are
large, they carry run-local detail and most runs are exploratory. The handful
of runs the README quotes are condensed into ``bench/summary.json``, which is
committed, so every figure in the README and in docs/benchmarks.md can be traced
to a file in the tree.

The summary carries only fields copied from the result files. Nothing is
recomputed or rounded beyond what the benchmark itself wrote and the hardware
description comes from the command line rather than being guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "bench" / "results"
SUMMARY = REPO_ROOT / "bench" / "summary.json"

DEFAULT_MACHINE = (
    "AMD Ryzen 7 5700X (8 cores, 16 threads), 48 GB RAM, Samsung 970 EVO Plus NVMe; "
    "Windows 11 Pro, Docker Desktop with the WSL2 backend; load generator and the whole "
    "stack on the same machine"
)

CONFIG_FIELDS = (
    "devices",
    "rate_hz",
    "duration_s",
    "warmup_s",
    "drain_s",
    "qos",
    "readings_per_message",
    "connections_used",
)
RESULT_FIELDS = (
    "started_at",
    "duration_s",
    "messages_published",
    "messages_failed",
    "readings_published",
    "readings_stored",
    "publish_rate_hz",
    "stored_rate_hz",
    "loss_fraction",
    "dead_letters",
    "device_to_ingest_ms",
    "end_to_end_ms",
    "notes",
)


def condense(result: dict) -> dict:
    config = result["config"]
    return {
        "label": result["label"],
        "config": {k: config[k] for k in CONFIG_FIELDS if k in config},
        **{k: result[k] for k in RESULT_FIELDS if k in result},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("labels", nargs="+", help="result labels under bench/results/")
    parser.add_argument("--machine", default=DEFAULT_MACHINE, help="hardware the runs were made on")
    parser.add_argument("--out", type=Path, default=SUMMARY)
    args = parser.parse_args(argv)

    runs = []
    for label in args.labels:
        path = RESULTS_DIR / f"{label}.json"
        if not path.exists():
            print(f"no result file for {label!r} at bench/results/{path.name}", file=sys.stderr)
            return 1
        runs.append(condense(json.loads(path.read_text(encoding="utf-8"))))

    summary = {
        "description": (
            "Condensed from bench/results/<label>.json, written by bench/benchmark.py "
            "against the full Compose stack. Regenerate with bench/summarise.py."
        ),
        "machine": args.machine,
        "runs": runs,
    }
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {args.out.resolve().relative_to(REPO_ROOT).as_posix()} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
