"""Synthetic sensor signals.

The point of the simulator is not to emit random numbers. It is to emit numbers
that behave enough like real ones that the rest of the platform is exercised
honestly: rollups have something to average, alert rules have thresholds that
are actually crossed, compression has correlated data to compress and the
dashboard has curves rather than noise.

Every generator is seeded per device, so a run is reproducible. That matters
because the benchmark numbers quoted in the README have to be regenerable, and
because a failing alert test needs to fail the same way twice.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

SECONDS_PER_DAY = 86_400.0


@dataclass
class OrnsteinUhlenbeck:
    """Mean-reverting random walk.

    Real environmental sensors wander but do not run away, which is exactly what
    an OU process describes. A plain random walk drifts to nonsense over a long
    run, and independent samples look nothing like a sensor.
    """

    mean: float
    #: Pull back towards the mean, per second. Larger is stiffer.
    theta: float
    #: Volatility per sqrt(second).
    sigma: float
    value: float = field(default=float("nan"))

    def __post_init__(self) -> None:
        if self.value != self.value:  # NaN sentinel: start at the mean
            self.value = self.mean

    def step(self, rng: random.Random, dt_s: float) -> float:
        drift = self.theta * (self.mean - self.value) * dt_s
        diffusion = self.sigma * math.sqrt(max(dt_s, 1e-9)) * rng.gauss(0.0, 1.0)
        self.value += drift + diffusion
        return self.value


@dataclass
class DiurnalTemperature:
    """Daily temperature cycle with a slow baseline drift.

    Coldest around 05:00, warmest around 15:00, which is roughly what an indoor
    sensor near a window sees.
    """

    mean_c: float = 19.0
    amplitude_c: float = 4.5
    peak_hour: float = 15.0
    noise_c: float = 0.15
    drift: OrnsteinUhlenbeck = field(
        default_factory=lambda: OrnsteinUhlenbeck(mean=0.0, theta=1e-4, sigma=0.004)
    )

    def sample(self, rng: random.Random, t_s: float, dt_s: float) -> float:
        phase = 2.0 * math.pi * ((t_s / SECONDS_PER_DAY) - (self.peak_hour / 24.0))
        cycle = self.amplitude_c * math.cos(phase)
        return self.mean_c + cycle + self.drift.step(rng, dt_s) + rng.gauss(0.0, self.noise_c)


@dataclass
class HumidityFromTemperature:
    """Relative humidity, anti-correlated with temperature.

    Absolute moisture changes slowly indoors, so as the air warms the relative
    humidity falls. Modelling that correlation rather than drawing an independent
    value is what makes a two-metric alert rule meaningful.
    """

    reference_c: float = 19.0
    reference_pct: float = 52.0
    slope_pct_per_c: float = -2.1
    noise_pct: float = 0.6

    def sample(self, rng: random.Random, temperature_c: float) -> float:
        value = (
            self.reference_pct
            + self.slope_pct_per_c * (temperature_c - self.reference_c)
            + rng.gauss(0.0, self.noise_pct)
        )
        return min(100.0, max(0.0, value))


@dataclass
class BatteryDischarge:
    """Li-ion discharge curve: a long flat plateau then a knee.

    A linear ramp would make a low-battery alert fire predictably and prove
    nothing. The knee is where the interesting behaviour is, so it is modelled.
    """

    full_v: float = 4.20
    knee_v: float = 3.60
    empty_v: float = 3.30
    life_s: float = 45.0 * SECONDS_PER_DAY
    #: Voltage sag per degree below 20 degC.
    temp_coeff_v_per_c: float = 0.004
    noise_v: float = 0.004

    def sample(self, rng: random.Random, elapsed_s: float, temperature_c: float) -> float:
        fraction = min(1.0, max(0.0, elapsed_s / self.life_s))
        # Plateau for the first 85 per cent of life, then fall away sharply.
        if fraction < 0.85:
            base = self.full_v - (self.full_v - self.knee_v) * (fraction / 0.85) ** 2.2
        else:
            knee_progress = (fraction - 0.85) / 0.15
            base = self.knee_v - (self.knee_v - self.empty_v) * knee_progress**0.6
        sag = self.temp_coeff_v_per_c * max(0.0, 20.0 - temperature_c)
        return max(0.0, base - sag + rng.gauss(0.0, self.noise_v))


@dataclass
class WifiRssi:
    """Signal strength with a slow wander and occasional deep fades.

    Fades are what a device-health rule has to tolerate without crying wolf, so
    the simulator produces them deliberately.
    """

    mean_dbm: float = -62.0
    process: OrnsteinUhlenbeck = field(default=None)  # type: ignore[assignment]
    fade_probability_per_sample: float = 0.01
    fade_depth_db: float = 22.0

    def __post_init__(self) -> None:
        if self.process is None:
            self.process = OrnsteinUhlenbeck(mean=self.mean_dbm, theta=0.02, sigma=0.9)

    def sample(self, rng: random.Random, dt_s: float) -> float:
        value = self.process.step(rng, dt_s)
        if rng.random() < self.fade_probability_per_sample:
            value -= abs(rng.gauss(self.fade_depth_db, 6.0))
        return min(-1.0, max(-120.0, value))


@dataclass
class MachineVibration:
    """Duty-cycled machine vibration with an optional growing bearing fault.

    The fault grows exponentially, which is what gives the alerting rule engine a
    sustained-breach condition to detect rather than a single spike.
    """

    idle_rms_g: float = 0.012
    running_rms_g: float = 0.32
    duty_period_s: float = 1800.0
    duty_fraction: float = 0.6
    noise_fraction: float = 0.08
    #: Doubling time of the fault amplitude. None means a healthy machine.
    fault_doubling_s: float | None = None
    fault_start_s: float = 0.0

    def sample(self, rng: random.Random, t_s: float) -> float:
        running = (t_s % self.duty_period_s) < (self.duty_period_s * self.duty_fraction)
        base = self.running_rms_g if running else self.idle_rms_g
        if self.fault_doubling_s and t_s >= self.fault_start_s:
            growth = 2.0 ** ((t_s - self.fault_start_s) / self.fault_doubling_s)
            base *= min(growth, 40.0)
        return max(0.0, base * (1.0 + rng.gauss(0.0, self.noise_fraction)))


@dataclass
class Co2Occupancy:
    """Occupancy-driven CO2 with exponential decay back to outdoor baseline."""

    baseline_ppm: float = 430.0
    occupied_gain_ppm: float = 620.0
    decay_tau_s: float = 900.0
    arrival_probability_per_sample: float = 0.004
    level: float = 0.0

    def sample(self, rng: random.Random, dt_s: float) -> float:
        self.level *= math.exp(-dt_s / self.decay_tau_s)
        if rng.random() < self.arrival_probability_per_sample:
            self.level += self.occupied_gain_ppm * rng.uniform(0.4, 1.0)
        return self.baseline_ppm + self.level + rng.gauss(0.0, 6.0)


@dataclass
class Pressure:
    """Barometric pressure: a slow wander around standard atmosphere."""

    process: OrnsteinUhlenbeck = field(
        default_factory=lambda: OrnsteinUhlenbeck(mean=1013.25, theta=2e-5, sigma=0.02)
    )

    def sample(self, rng: random.Random, dt_s: float) -> float:
        return self.process.step(rng, dt_s)
