#!/usr/bin/env python3
"""Generate the published JSON Schema files from the Pydantic models.

Run with ``--check`` in CI. If the checked-in schemas differ from what the
models produce the build fails, which is the only reliable way to stop the
published contract drifting away from the code that enforces it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from telemetry_common.metrics import CATALOGUE
from telemetry_common.models import StatusMessage, TelemetryMessage
from telemetry_common.versioning import CURRENT_VERSION

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "services" / "common" / "telemetry_common" / "schemas"

BASE_URI = "https://schemas.cgainsworks.dev/iot-telemetry"


def _build(model: type[BaseModel], name: str) -> dict:
    schema = model.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"{BASE_URI}/{name}/v{CURRENT_VERSION.major}.json"
    schema["title"] = name
    schema["x-schema-version"] = str(CURRENT_VERSION)
    return schema


def _metric_catalogue() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"{BASE_URI}/metrics/catalogue.json",
        "title": "metric_catalogue",
        "description": (
            "Units and plausible ranges for every metric the fleet may report. "
            "Adding an entry is a catalogue change, not a schema version bump."
        ),
        "metrics": {
            spec.name: {
                "unit": spec.unit,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "description": spec.description,
            }
            for spec in CATALOGUE.values()
        },
    }


def targets() -> dict[Path, dict]:
    major = CURRENT_VERSION.major
    return {
        SCHEMA_DIR / "telemetry" / f"v{major}.json": _build(TelemetryMessage, "telemetry"),
        SCHEMA_DIR / "status" / f"v{major}.json": _build(StatusMessage, "status"),
        SCHEMA_DIR / "metric_catalogue.json": _metric_catalogue(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if files are stale")
    args = parser.parse_args()

    stale: list[Path] = []
    for path, schema in targets().items():
        rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="" stops Python translating to CRLF on Windows. The
            # repository is LF everywhere, and a generator that emits CRLF
            # would make the --check run in CI fail on a clean tree.
            path.write_text(rendered, encoding="utf-8", newline="")
            print(f"wrote {path.relative_to(ROOT).as_posix()}")

    if stale:
        for path in stale:
            print(f"stale: {path.relative_to(ROOT).as_posix()}", file=sys.stderr)
        print("\nrun: python scripts/gen_schemas.py", file=sys.stderr)
        return 1

    if args.check:
        print("schemas are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
