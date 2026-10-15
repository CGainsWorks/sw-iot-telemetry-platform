"""One simulated device.

Deliberately free of any MQTT dependency. The device produces messages and
knows nothing about how they are shipped, which means the signal behaviour,
the fault behaviour and the schema handling can all be unit tested without a
broker running.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from telemetry_common.models import DeviceStatus, LifecycleEvent, StatusMessage, TelemetryMessage
from telemetry_common.versioning import CURRENT_SCHEMA_VERSION

from .faults import FaultKind, FaultPlan, spike_value
from .profiles import DeviceProfile
from .signals import (
    BatteryDischarge,
    Co2Occupancy,
    DiurnalTemperature,
    HumidityFromTemperature,
    MachineVibration,
    Pressure,
    WifiRssi,
)

#: Stamped into messages when the schema_drift fault is active. A major this
#: build does not support, so the platform must dead-letter it rather than
#: guess at the contents.
UNSUPPORTED_SCHEMA_VERSION = "9.0.0"


@dataclass
class SimulatedDevice:
    """A single node in the synthetic fleet."""

    device_id: str
    site_id: str
    profile: DeviceProfile
    fault_plan: FaultPlan = field(default_factory=FaultPlan)
    seed: int = 0
    #: Wall-clock instant the run started, used to build message timestamps.
    epoch: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Seconds of simulated life the device already had before the run began, so
    #: a fleet does not consist entirely of freshly booted devices with full
    #: batteries and identical diurnal phase.
    warm_start_s: float = 0.0

    seq: int = 0
    _rng: random.Random = field(init=False)
    _temperature: DiurnalTemperature = field(init=False)
    _humidity: HumidityFromTemperature = field(init=False)
    _pressure: Pressure = field(init=False)
    _battery: BatteryDischarge = field(init=False)
    _rssi: WifiRssi = field(init=False)
    _vibration: MachineVibration = field(init=False)
    _co2: Co2Occupancy = field(init=False)
    _last_elapsed_s: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

        # Per-device offsets so no two devices produce the same curve. Sites also
        # differ, which gives site-level rollups something real to distinguish.
        site_offset = {"site-a": 0.0, "site-b": 2.5, "site-c": -1.8}.get(self.site_id, 0.0)

        self._temperature = DiurnalTemperature(
            mean_c=19.0 + site_offset + self._rng.uniform(-1.5, 1.5),
            amplitude_c=self._rng.uniform(3.0, 6.0),
        )
        self._humidity = HumidityFromTemperature(
            reference_pct=self._rng.uniform(45.0, 58.0),
        )
        self._pressure = Pressure()
        self._rssi = WifiRssi(mean_dbm=self._rng.uniform(-75.0, -48.0))

        battery_life_days = 12.0 if FaultKind.DRAIN in self.fault_plan.kinds else 45.0
        self._battery = BatteryDischarge(life_s=battery_life_days * 86_400.0)

        bearing = FaultKind.BEARING in self.fault_plan.kinds
        self._vibration = MachineVibration(
            duty_period_s=self._rng.uniform(1200.0, 2400.0),
            duty_fraction=self._rng.uniform(0.45, 0.75),
            fault_doubling_s=self._rng.uniform(600.0, 1500.0) if bearing else None,
            fault_start_s=self.fault_plan.onset_s if bearing else 0.0,
        )
        self._co2 = Co2Occupancy()

    # -- timing ------------------------------------------------------------

    def next_interval_s(self) -> float:
        """Seconds until the next publish, with per-device jitter."""
        jitter = self.profile.jitter_fraction
        return self.profile.interval_s * self._rng.uniform(1.0 - jitter, 1.0 + jitter)

    @property
    def uptime_s(self) -> int:
        return int(self.warm_start_s + self._last_elapsed_s)

    # -- payloads ----------------------------------------------------------

    def _readings(self, sim_time_s: float, dt_s: float) -> dict[str, float]:
        rng = self._rng
        temperature = self._temperature.sample(rng, sim_time_s, dt_s)

        values: dict[str, float] = {}
        for metric in self.profile.metrics:
            if metric == "temperature_c":
                values[metric] = round(temperature, 2)
            elif metric == "humidity_pct":
                values[metric] = round(self._humidity.sample(rng, temperature), 2)
            elif metric == "pressure_hpa":
                values[metric] = round(self._pressure.sample(rng, dt_s), 2)
            elif metric == "battery_v":
                values[metric] = round(self._battery.sample(rng, sim_time_s, temperature), 3)
            elif metric == "rssi_dbm":
                values[metric] = round(self._rssi.sample(rng, dt_s), 1)
            elif metric == "vibration_rms_g":
                values[metric] = round(self._vibration.sample(rng, sim_time_s), 4)
            elif metric == "co2_ppm":
                values[metric] = round(self._co2.sample(rng, dt_s), 1)

        # A stuck sensor freezes its first metric at whatever it last read. The
        # value stays perfectly plausible, which is the whole difficulty: only a
        # flatline rule catches it.
        if self.fault_plan.active(FaultKind.STUCK, self._last_elapsed_s) and values:
            frozen = self.profile.metrics[0]
            if frozen in values:
                values[frozen] = getattr(self, "_stuck_value", values[frozen])
                self._stuck_value = values[frozen]

        if self.fault_plan.should_spike(rng, self._last_elapsed_s) and values:
            metric = rng.choice(list(values))
            values[metric] = round(spike_value(rng, metric, values[metric]), 3)

        return values

    def _status(self) -> DeviceStatus:
        elapsed = self._last_elapsed_s
        if self.fault_plan.active(FaultKind.STUCK, elapsed):
            return DeviceStatus.FAULT
        if self.fault_plan.active(FaultKind.DRAIN, elapsed):
            return DeviceStatus.DEGRADED
        return DeviceStatus.OK

    def sample(
        self, elapsed_s: float, *, sim_elapsed_s: float | None = None
    ) -> TelemetryMessage | None:
        """Produce the message for this tick, or ``None`` while offline.

        ``elapsed_s`` is real seconds since the run started and is what the
        message timestamp is built from. ``sim_elapsed_s`` is how far the
        simulated physics has advanced, and defaults to the same thing.

        The two are separate so that a run can be accelerated. Advancing the
        physics faster than the clock is what lets a demo watch a battery reach
        its discharge knee in minutes. Advancing the *timestamp* faster would
        simply produce messages dated hours in the future, which ingest rejects
        as a broken device clock, so the acceleration has to stay on this side
        of the line.
        """
        self._last_elapsed_s = elapsed_s
        if self.fault_plan.in_dropout(elapsed_s):
            return None

        dt_s = max(self.profile.interval_s, 0.001)
        simulated = elapsed_s if sim_elapsed_s is None else sim_elapsed_s
        sim_time_s = self.warm_start_s + simulated

        readings = self._readings(sim_time_s, dt_s)
        if not readings:
            return None

        ts = self.epoch + timedelta(seconds=elapsed_s)
        if self.fault_plan.active(FaultKind.CLOCK_SKEW, elapsed_s):
            ts += timedelta(seconds=self.fault_plan.clock_skew_s)

        self.seq += 1
        schema_version = CURRENT_SCHEMA_VERSION
        device_id = self.device_id

        if self.fault_plan.should_send_bad_schema(self._rng, elapsed_s):
            schema_version = UNSUPPORTED_SCHEMA_VERSION
        if self.fault_plan.should_spoof(self._rng, elapsed_s):
            # Claim to be a neighbour. The topic still carries the real identity,
            # so ingest must reject this on the mismatch.
            device_id = "esp32-000001" if self.device_id != "esp32-000001" else "esp32-000002"

        return TelemetryMessage(
            schema_version=schema_version,
            device_id=device_id,
            site_id=self.site_id,
            seq=self.seq,
            ts=ts,
            fw_version=self.profile.fw_version,
            uptime_s=self.uptime_s,
            status=self._status(),
            readings=readings,
        )

    def lifecycle(self, event: LifecycleEvent, *, reason: str | None = None) -> StatusMessage:
        return StatusMessage(
            device_id=self.device_id,
            site_id=self.site_id,
            ts=datetime.now(UTC),
            event=event,
            fw_version=self.profile.fw_version,
            reason=reason,
        )

    def will_payload(self) -> dict[str, Any]:
        """Last will and testament, published by the broker if the device drops.

        The broker sends this on an ungraceful disconnect, which is the only way
        to tell a crashed device from a quiet one without waiting out a timeout.
        """
        return self.lifecycle(LifecycleEvent.OFFLINE, reason="unexpected disconnect").model_dump(
            mode="json"
        )
