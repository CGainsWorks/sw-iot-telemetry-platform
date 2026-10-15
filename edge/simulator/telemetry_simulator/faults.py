"""Fault injection.

A fleet that only ever behaves correctly proves nothing. Every fault here maps
to a platform path that is otherwise untestable without waiting for real
hardware to misbehave:

===================  ==========================================================
Fault                What it proves works
===================  ==========================================================
``stuck``            Rollups and a flatline alert rule notice a dead sensor
``spike``            Range validation routes implausible values to the DLQ
``dropout``          Device-offline alerting fires and then recovers
``clock_skew``       Ingest lag metrics separate device clocks from real lag
``drain``            The low-battery rule fires before the device dies
``bearing``          A sustained-breach rule beats a single-sample threshold
``schema_drift``     An unsupported major goes to the DLQ, not into storage
``spoof``            Topic identity beats payload identity, and the mismatch is
                     rejected
===================  ==========================================================
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum


class FaultKind(str, Enum):
    STUCK = "stuck"
    SPIKE = "spike"
    DROPOUT = "dropout"
    CLOCK_SKEW = "clock_skew"
    DRAIN = "drain"
    BEARING = "bearing"
    SCHEMA_DRIFT = "schema_drift"
    SPOOF = "spoof"


ALL_FAULTS: tuple[FaultKind, ...] = tuple(FaultKind)


@dataclass
class FaultPlan:
    """The faults one device will exhibit during a run."""

    kinds: frozenset[FaultKind] = frozenset()
    #: Seconds into the run at which faults begin.
    onset_s: float = 120.0
    #: Fraction of samples affected, for the intermittent faults.
    rate: float = 0.02
    #: Seconds a dropout lasts.
    dropout_duration_s: float = 180.0
    #: Seconds the device clock is offset by.
    clock_skew_s: float = 45.0

    def active(self, kind: FaultKind, elapsed_s: float) -> bool:
        return kind in self.kinds and elapsed_s >= self.onset_s

    def in_dropout(self, elapsed_s: float) -> bool:
        """True while a dropout window is open.

        Dropouts repeat on a cycle so a single long run exercises both the
        firing and the recovery side of the offline rule.
        """
        if not self.active(FaultKind.DROPOUT, elapsed_s):
            return False
        cycle = self.dropout_duration_s * 4.0
        return ((elapsed_s - self.onset_s) % cycle) < self.dropout_duration_s

    def should_spike(self, rng: random.Random, elapsed_s: float) -> bool:
        return self.active(FaultKind.SPIKE, elapsed_s) and rng.random() < self.rate

    def should_send_bad_schema(self, rng: random.Random, elapsed_s: float) -> bool:
        return self.active(FaultKind.SCHEMA_DRIFT, elapsed_s) and rng.random() < self.rate

    def should_spoof(self, rng: random.Random, elapsed_s: float) -> bool:
        return self.active(FaultKind.SPOOF, elapsed_s) and rng.random() < self.rate


def plan_for(
    index: int,
    *,
    fraction: float,
    kinds: tuple[FaultKind, ...] = ALL_FAULTS,
    seed: int = 0,
) -> FaultPlan:
    """Deterministically decide which faults device ``index`` will exhibit.

    ``fraction`` is the share of the fleet that is faulty at all. Devices are
    picked by a seeded generator rather than at random on each run, so a fleet
    of 50 devices has the same 3 broken ones every time and a regression in the
    alerting rules is reproducible.
    """
    if fraction <= 0.0 or not kinds:
        return FaultPlan()

    rng = random.Random((seed << 20) ^ index)
    if rng.random() >= fraction:
        return FaultPlan()

    # A faulty device gets one primary fault, occasionally two. Piling every
    # fault onto one device would make the resulting alerts impossible to read.
    chosen = {rng.choice(kinds)}
    if rng.random() < 0.25:
        chosen.add(rng.choice(kinds))

    return FaultPlan(
        kinds=frozenset(chosen),
        onset_s=rng.uniform(60.0, 300.0),
        rate=rng.uniform(0.01, 0.05),
        dropout_duration_s=rng.uniform(90.0, 300.0),
        clock_skew_s=rng.choice([-120.0, -45.0, 30.0, 90.0]),
    )


def spike_value(rng: random.Random, metric: str, value: float) -> float:
    """Return an implausible reading for ``metric``.

    Values are pushed clearly outside the catalogue range rather than merely to
    the edge of it, so the dead-letter path is unambiguous when it fires.
    """
    table = {
        "temperature_c": 999.0,
        "humidity_pct": 240.0,
        "pressure_hpa": 4.0,
        "battery_v": 42.0,
        "rssi_dbm": 40.0,
        "vibration_rms_g": 250.0,
        "co2_ppm": 90000.0,
    }
    magnitude = table.get(metric, value * 1000.0 + 1000.0)
    return magnitude * rng.uniform(0.95, 1.05)
