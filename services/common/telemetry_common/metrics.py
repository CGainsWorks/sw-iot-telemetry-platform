"""Catalogue of the metrics the fleet is allowed to report.

The message envelope is deliberately stable while this catalogue moves. A new
sensor type is a catalogue addition, not a schema version bump, because
``readings`` is an open map of metric name to value. What the catalogue buys us
is a single place that knows the unit, the physically plausible range and the
storage precision of every metric, so ingest can reject nonsense without every
service carrying its own copy of the rules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricSpec:
    """Unit and plausible range for one metric."""

    name: str
    unit: str
    minimum: float
    maximum: float
    description: str

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("temperature_c", "degC", -40.0, 125.0, "Ambient air temperature"),
    MetricSpec("humidity_pct", "percent", 0.0, 100.0, "Relative humidity"),
    MetricSpec("pressure_hpa", "hPa", 300.0, 1100.0, "Barometric pressure"),
    MetricSpec("battery_v", "V", 0.0, 5.0, "Battery terminal voltage"),
    MetricSpec("rssi_dbm", "dBm", -120.0, 0.0, "Wi-Fi received signal strength"),
    MetricSpec("vibration_rms_g", "g", 0.0, 16.0, "Broadband vibration, RMS"),
    MetricSpec("co2_ppm", "ppm", 0.0, 40000.0, "Carbon dioxide concentration"),
)

CATALOGUE: dict[str, MetricSpec] = {spec.name: spec for spec in _SPECS}

METRIC_NAMES: tuple[str, ...] = tuple(CATALOGUE)


def spec_for(metric: str) -> MetricSpec | None:
    """Return the specification for ``metric``, or ``None`` if it is unknown."""
    return CATALOGUE.get(metric)


def is_plausible(metric: str, value: float) -> bool:
    """True if ``value`` sits inside the catalogue range for ``metric``.

    Unknown metrics are treated as plausible. Rejecting them here would make the
    platform impossible to extend without a coordinated fleet-wide deploy, which
    is exactly the coupling the open ``readings`` map is meant to avoid.
    """
    spec = CATALOGUE.get(metric)
    if spec is None:
        return True
    return spec.contains(value)


def unit_for(metric: str) -> str:
    spec = CATALOGUE.get(metric)
    return spec.unit if spec else "unknown"
