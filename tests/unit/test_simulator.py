"""The device simulator.

The simulator is what makes the rest of the platform testable, so its own
behaviour has to be trustworthy. Two properties matter most: runs are
reproducible from a seed, and the injected faults actually produce the
conditions they claim to.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime

import pytest

from telemetry_common.metrics import is_plausible
from telemetry_common.models import DeviceStatus
from telemetry_common.versioning import is_supported
from telemetry_simulator.device import UNSUPPORTED_SCHEMA_VERSION, SimulatedDevice
from telemetry_simulator.faults import ALL_FAULTS, FaultKind, FaultPlan, plan_for, spike_value
from telemetry_simulator.fleet import device_id_for
from telemetry_simulator.profiles import PROFILES, FleetMix, site_for
from telemetry_simulator.signals import (
    BatteryDischarge,
    DiurnalTemperature,
    HumidityFromTemperature,
    MachineVibration,
    OrnsteinUhlenbeck,
    WifiRssi,
)

EPOCH = datetime(2026, 8, 22, 10, 0, tzinfo=UTC)


def make_device(**overrides) -> SimulatedDevice:
    base = {
        "device_id": "esp32-000001",
        "site_id": "site-a",
        "profile": PROFILES["environment"],
        "seed": 42,
        "epoch": EPOCH,
    }
    base.update(overrides)
    return SimulatedDevice(**base)


class TestSignals:
    def test_ou_process_reverts_to_its_mean(self):
        # A plain random walk drifts to nonsense over a long run, which is why
        # the signals use mean reversion.
        rng = random.Random(1)
        process = OrnsteinUhlenbeck(mean=10.0, theta=0.5, sigma=0.1, value=100.0)
        for _ in range(500):
            process.step(rng, 1.0)
        assert process.value == pytest.approx(10.0, abs=1.0)

    def test_diurnal_temperature_peaks_in_the_afternoon(self):
        rng = random.Random(1)
        signal = DiurnalTemperature(mean_c=20.0, amplitude_c=5.0, noise_c=0.0, peak_hour=15.0)
        # No drift, so the shape is the cosine alone.
        signal.drift = OrnsteinUhlenbeck(mean=0.0, theta=0.0, sigma=0.0)
        afternoon = signal.sample(rng, 15 * 3600, 1.0)
        pre_dawn = signal.sample(rng, 3 * 3600, 1.0)
        assert afternoon > pre_dawn

    def test_humidity_falls_as_temperature_rises(self):
        # The anti-correlation is what makes a two-metric rule mean anything.
        rng = random.Random(1)
        signal = HumidityFromTemperature(noise_pct=0.0)
        assert signal.sample(rng, 25.0) < signal.sample(rng, 15.0)

    def test_humidity_stays_within_physical_bounds(self):
        rng = random.Random(1)
        signal = HumidityFromTemperature()
        for temperature in range(-40, 60):
            value = signal.sample(rng, float(temperature))
            assert 0.0 <= value <= 100.0

    def test_battery_has_a_plateau_then_a_knee(self):
        rng = random.Random(1)
        battery = BatteryDischarge(life_s=1000.0, noise_v=0.0)
        early = battery.sample(rng, 100.0, 20.0)
        mid = battery.sample(rng, 500.0, 20.0)
        late = battery.sample(rng, 990.0, 20.0)

        assert early > mid > late
        # The plateau is flat relative to the knee: that is the whole point of
        # modelling it rather than using a straight line.
        assert (early - mid) < (mid - late)

    def test_battery_sags_in_the_cold(self):
        rng = random.Random(1)
        battery = BatteryDischarge(noise_v=0.0)
        assert battery.sample(rng, 0.0, -10.0) < battery.sample(rng, 0.0, 25.0)

    def test_rssi_stays_in_range_even_with_deep_fades(self):
        rng = random.Random(3)
        signal = WifiRssi(mean_dbm=-60.0, fade_probability_per_sample=0.5)
        for _ in range(500):
            value = signal.sample(rng, 1.0)
            assert -120.0 <= value <= -1.0


class TestDeterminism:
    def test_the_same_seed_produces_the_same_run(self):
        # Benchmark numbers quoted in the README have to be regenerable, and a
        # failing alert fixture has to fail the same way twice.
        first = [make_device().sample(float(i)) for i in range(20)]
        second = [make_device().sample(float(i)) for i in range(20)]
        assert [m.readings for m in first] == [m.readings for m in second]

    def test_different_seeds_produce_different_runs(self):
        a = make_device(seed=1).sample(0.0)
        b = make_device(seed=2).sample(0.0)
        assert a.readings != b.readings

    def test_profile_assignment_is_stable_across_runs(self):
        mix = FleetMix()
        assert [mix.assign(i).name for i in range(20)] == [mix.assign(i).name for i in range(20)]

    def test_sites_are_spread_across_the_fleet(self):
        assert len({site_for(i) for i in range(30)}) == 3

    def test_device_ids_are_zero_padded(self):
        assert device_id_for(7, "esp32") == "esp32-000007"


class TestMessages:
    def test_a_message_only_reports_its_profile_metrics(self):
        device = make_device(profile=PROFILES["machine"])
        message = device.sample(0.0)
        assert set(message.readings) == set(PROFILES["machine"].metrics)

    def test_sequence_numbers_increase(self):
        device = make_device()
        assert [device.sample(float(i)).seq for i in range(5)] == [1, 2, 3, 4, 5]

    def test_timestamps_advance_with_elapsed_time(self):
        device = make_device()
        first = device.sample(0.0)
        later = device.sample(60.0)
        assert (later.ts - first.ts).total_seconds() == pytest.approx(60.0)

    def test_readings_are_plausible_for_a_healthy_device(self):
        # A healthy simulated device must never trip the ingest range check,
        # otherwise the dead-letter counts in the demo are meaningless.
        device = make_device()
        for step in range(300):
            message = device.sample(float(step * 10))
            for metric, value in message.readings.items():
                assert is_plausible(metric, value), f"{metric}={value} at step {step}"

    def test_all_profiles_produce_plausible_readings(self):
        for name, profile in PROFILES.items():
            device = make_device(profile=profile, seed=hash(name) % 10_000)
            for step in range(200):
                message = device.sample(float(step * profile.interval_s))
                for metric, value in message.readings.items():
                    assert is_plausible(metric, value), f"{name}: {metric}={value}"

    def test_publish_intervals_stay_within_the_jitter_band(self):
        profile = PROFILES["environment"]
        device = make_device(profile=profile)
        for _ in range(200):
            interval = device.next_interval_s()
            assert profile.interval_s * (1 - profile.jitter_fraction) <= interval
            assert interval <= profile.interval_s * (1 + profile.jitter_fraction)

    def test_warm_start_gives_a_non_zero_uptime(self):
        device = make_device(warm_start_s=7200.0)
        assert device.sample(10.0).uptime_s >= 7200


class TestFaultInjection:
    def test_no_faults_when_the_fraction_is_zero(self):
        assert all(plan_for(i, fraction=0.0).kinds == frozenset() for i in range(50))

    def test_fault_assignment_is_deterministic(self):
        first = [
            sorted(k.value for k in plan_for(i, fraction=0.3, seed=7).kinds) for i in range(50)
        ]
        second = [
            sorted(k.value for k in plan_for(i, fraction=0.3, seed=7).kinds) for i in range(50)
        ]
        assert first == second

    def test_roughly_the_requested_fraction_is_faulty(self):
        faulty = sum(1 for i in range(2000) if plan_for(i, fraction=0.2, seed=1).kinds)
        assert 0.15 < faulty / 2000 < 0.25

    def test_a_dropout_suppresses_publishing_and_then_recovers(self):
        plan = FaultPlan(
            kinds=frozenset({FaultKind.DROPOUT}), onset_s=0.0, dropout_duration_s=100.0
        )
        device = make_device(fault_plan=plan)

        assert device.sample(50.0) is None  # inside the dropout
        assert device.sample(150.0) is not None  # after it

    def test_a_stuck_sensor_freezes_its_value_but_stays_plausible(self):
        # This is the hard case: nothing about the value is out of range, so
        # only a flatline rule can catch it.
        plan = FaultPlan(kinds=frozenset({FaultKind.STUCK}), onset_s=0.0)
        device = make_device(fault_plan=plan)
        values = [device.sample(float(i * 10)).readings["temperature_c"] for i in range(1, 30)]

        assert len(set(values[1:])) == 1
        assert is_plausible("temperature_c", values[-1])

    def test_a_stuck_device_reports_itself_as_faulted(self):
        plan = FaultPlan(kinds=frozenset({FaultKind.STUCK}), onset_s=0.0)
        assert make_device(fault_plan=plan).sample(10.0).status is DeviceStatus.FAULT

    def test_clock_skew_shifts_the_timestamp(self):
        plan = FaultPlan(kinds=frozenset({FaultKind.CLOCK_SKEW}), onset_s=0.0, clock_skew_s=120.0)
        skewed = make_device(fault_plan=plan).sample(0.0)
        clean = make_device().sample(0.0)
        assert (skewed.ts - clean.ts).total_seconds() == pytest.approx(120.0)

    def test_spoofing_changes_the_payload_identity_only(self):
        plan = FaultPlan(kinds=frozenset({FaultKind.SPOOF}), onset_s=0.0, rate=1.0)
        device = make_device(device_id="esp32-000005", fault_plan=plan)
        message = device.sample(10.0)
        # The topic still carries the real identity, so ingest must reject this.
        assert message.device_id != "esp32-000005"

    def test_schema_drift_stamps_an_unsupported_version(self):
        plan = FaultPlan(kinds=frozenset({FaultKind.SCHEMA_DRIFT}), onset_s=0.0, rate=1.0)
        message = make_device(fault_plan=plan).sample(10.0)
        assert message.schema_version == UNSUPPORTED_SCHEMA_VERSION
        assert not is_supported(message.schema_version)

    def test_spikes_are_clearly_outside_the_range_not_merely_at_its_edge(self):
        rng = random.Random(1)
        for metric in ("temperature_c", "humidity_pct", "battery_v", "co2_ppm"):
            assert not is_plausible(metric, spike_value(rng, metric, 20.0))

    def test_a_bearing_fault_grows_the_amplitude(self):
        # Tested on the signal directly with the duty cycle held on. At the
        # device level the machine spends part of every cycle idle, so two
        # arbitrary samples can easily compare a faulted idle reading against a
        # healthy running one and show the growth going backwards.
        rng = random.Random(1)
        signal = MachineVibration(
            duty_fraction=1.0, noise_fraction=0.0, fault_doubling_s=600.0, fault_start_s=0.0
        )
        early = signal.sample(rng, 0.0)
        later = signal.sample(rng, 600.0)
        assert later == pytest.approx(early * 2.0, rel=1e-6)

    def test_a_bearing_fault_raises_the_peak_the_platform_actually_sees(self):
        # The device-level claim: over a window long enough to cover the duty
        # cycle, the peak grows. This is the signal the rate-of-change rule reads.
        plan = FaultPlan(kinds=frozenset({FaultKind.BEARING}), onset_s=0.0)
        device = make_device(profile=PROFILES["machine"], fault_plan=plan)

        early_peak = max(
            device.sample(float(t)).readings["vibration_rms_g"] for t in range(0, 900, 2)
        )
        late_peak = max(
            device.sample(float(t)).readings["vibration_rms_g"] for t in range(3600, 4500, 2)
        )
        assert late_peak > early_peak

    def test_drain_shortens_battery_life(self):
        drained = make_device(fault_plan=FaultPlan(kinds=frozenset({FaultKind.DRAIN}), onset_s=0.0))
        healthy = make_device()
        # A day in, the draining device should be measurably lower.
        day = 86_400.0
        assert drained.sample(day).readings["battery_v"] < healthy.sample(day).readings["battery_v"]

    def test_every_declared_fault_kind_can_be_planned(self):
        # Guards against adding a FaultKind and forgetting to wire it up.
        seen: set[FaultKind] = set()
        for i in range(5000):
            seen |= plan_for(i, fraction=1.0, kinds=ALL_FAULTS, seed=3).kinds
        assert seen == set(ALL_FAULTS)


class TestLifecycle:
    def test_the_will_payload_reports_an_unexpected_disconnect(self):
        payload = make_device().will_payload()
        assert payload["event"] == "offline"
        assert "unexpected" in payload["reason"]

    def test_the_will_payload_is_json_serialisable(self):
        import json

        json.dumps(make_device().will_payload())


class TestNumericSafety:
    def test_no_reading_is_ever_nan_or_infinite(self):
        # A NaN survives a JSON round trip in some encoders and then poisons
        # every average downstream.
        for name, profile in PROFILES.items():
            device = make_device(profile=profile, seed=len(name))
            for step in range(500):
                for metric, value in device.sample(float(step)).readings.items():
                    assert math.isfinite(value), f"{name}/{metric} produced {value}"
