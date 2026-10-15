"""Device profiles: what a given class of hardware reports and how often.

A fleet is never homogeneous. Modelling three classes rather than one is what
makes the query API, the rollups and the dashboard deal with sparse metrics from
the start, instead of discovering the problem when the first real device type is
added.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DeviceProfile:
    """One class of device."""

    name: str
    #: Metric names this class reports.
    metrics: tuple[str, ...]
    #: Nominal seconds between telemetry publishes.
    interval_s: float
    #: Jitter as a fraction of the interval. Real devices are never in lockstep,
    #: and a fleet that is produces a thundering herd the platform never sees in
    #: production.
    jitter_fraction: float
    #: Firmware version this class ships with.
    fw_version: str
    #: Battery powered devices report battery_v and eventually go flat.
    battery_powered: bool
    description: str


ENVIRONMENT_NODE = DeviceProfile(
    name="environment",
    metrics=("temperature_c", "humidity_pct", "pressure_hpa", "battery_v", "rssi_dbm"),
    interval_s=10.0,
    jitter_fraction=0.15,
    fw_version="1.4.2",
    battery_powered=True,
    description="Battery ESP32 with a BME280. The bulk of the fleet.",
)

MACHINE_MONITOR = DeviceProfile(
    name="machine",
    metrics=("vibration_rms_g", "temperature_c", "rssi_dbm"),
    interval_s=2.0,
    jitter_fraction=0.05,
    fw_version="1.4.2",
    battery_powered=False,
    description="Mains-powered vibration monitor bolted to a motor. Fast cadence.",
)

AIR_QUALITY_NODE = DeviceProfile(
    name="air_quality",
    metrics=("co2_ppm", "temperature_c", "humidity_pct", "rssi_dbm"),
    interval_s=30.0,
    jitter_fraction=0.2,
    # Deliberately behind. A real fleet always has stragglers, and this one lets
    # the platform prove it accepts an older minor version rather than dropping it.
    fw_version="1.3.0",
    battery_powered=False,
    description="Mains-powered CO2 sensor on an older firmware line.",
)

PROFILES: dict[str, DeviceProfile] = {
    profile.name: profile for profile in (ENVIRONMENT_NODE, MACHINE_MONITOR, AIR_QUALITY_NODE)
}


@dataclass(frozen=True)
class FleetMix:
    """How many of each profile make up a fleet, as relative weights."""

    weights: dict[str, int] = field(
        default_factory=lambda: {"environment": 6, "machine": 3, "air_quality": 1}
    )

    def assign(self, index: int) -> DeviceProfile:
        """Deterministically pick a profile for device number ``index``.

        Deterministic rather than random so that device ``esp32-000004`` is the
        same class of device on every run, which keeps benchmark comparisons and
        alert fixtures stable.
        """
        expanded: list[str] = []
        for name, weight in self.weights.items():
            expanded.extend([name] * weight)
        return PROFILES[expanded[index % len(expanded)]]


SITES: tuple[str, ...] = ("site-a", "site-b", "site-c")


def site_for(index: int) -> str:
    """Spread devices across sites so site-scoped queries have something to group."""
    return SITES[index % len(SITES)]
