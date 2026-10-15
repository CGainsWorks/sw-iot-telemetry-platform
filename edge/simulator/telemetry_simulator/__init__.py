"""Synthetic device fleet for the IoT telemetry platform."""

from .device import SimulatedDevice
from .fleet import FleetConfig, FleetStats, run_fleet
from .profiles import PROFILES, DeviceProfile

__all__ = [
    "PROFILES",
    "DeviceProfile",
    "FleetConfig",
    "FleetStats",
    "SimulatedDevice",
    "run_fleet",
]
