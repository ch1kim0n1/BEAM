"""Phase-1 physics acceptance gates (mvp.md section 4 / pdd.md section 8).

These tests are the *correctness contract* for the physics layer. They assert the
monotonicity and bookkeeping properties the rest of BEAM depends on:

- kill time rises with range and with worse weather (higher alpha);
- slew time rises with angular distance;
- the thermal cap forces a cooldown latch that holds until h_resume;
- energy bookkeeping is conserved (deposition * time integrates to E_kill).

Every physics coefficient is pulled from ``beam.config.load_config`` -- no constants
are duplicated here, matching the "no hard-coded physics" rule (pdd.md 18). A handful
of synthetic geometric/thermal values used purely to *exercise* the functions are
local to the tests, not physics defaults.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from beam.config import load_config
from beam.engine.physics import (
    delivered_power,
    deposition_rate,
    dwell_to_kill,
    slew_time,
    thermal_update,
    track_efficiency,
)
from beam.util import angular_distance


@pytest.fixture(scope="module")
def cfg():
    """Default config tree (pdd.md 18). Source of every physics constant used here."""
    return load_config()


def _kill_time(cfg, *, alpha: float, range_m: float, e_kill: float, power: float) -> float:
    """End-to-end dwell-to-kill helper composing the 8.2 + 8.3 pipeline from config."""
    teff = cfg.physics.track_efficiency
    p_del = delivered_power(power, alpha, range_m)
    eta = track_efficiency(
        1.0, range_m, base=teff.base, range_falloff=teff.range_falloff
    )
    dep = deposition_rate(p_del, eta)
    return float(dwell_to_kill(e_kill, dep))


# --------------------------------------------------------------------------- #
# 8.2 Beer-Lambert attenuation                                                #
# --------------------------------------------------------------------------- #


def test_delivered_power_matches_beer_lambert(cfg):
    power = cfg.turret_defaults.power
    alpha = cfg.weather("haze").alpha
    rng = 1234.0
    expected = power * math.exp(-alpha * rng)
    assert delivered_power(power, alpha, rng) == pytest.approx(expected)


def test_delivered_power_decreases_with_range(cfg):
    power = cfg.turret_defaults.power
    alpha = cfg.weather("clear").alpha
    ranges = np.array([0.0, 1000.0, 2000.0, 4000.0])
    out = delivered_power(power, alpha, ranges)
    # Strictly decreasing in range.
    assert np.all(np.diff(out) < 0.0)
    assert out[0] == pytest.approx(power)  # zero range -> full power


def test_delivered_power_decreases_with_worse_weather(cfg):
    power = cfg.turret_defaults.power
    rng = 2000.0
    profiles = ["clear", "haze", "rain", "fog", "dust"]
    vals = [delivered_power(power, cfg.weather(p).alpha, rng) for p in profiles]
    # clear < haze < rain < fog < dust in alpha -> delivered power strictly decreasing.
    assert all(b < a for a, b in zip(vals, vals[1:]))


def test_delivered_power_vectorized_equals_scalar(cfg):
    power = cfg.turret_defaults.power
    alpha = cfg.weather("rain").alpha
    ranges = [10.0, 500.0, 3300.0]
    vec = delivered_power(power, alpha, np.array(ranges))
    for r, v in zip(ranges, vec):
        assert delivered_power(power, alpha, r) == pytest.approx(v)


# --------------------------------------------------------------------------- #
# 8.3 Dwell-to-kill: MONOTONICITY gates                                       #
# --------------------------------------------------------------------------- #


def test_track_efficiency_in_unit_interval_and_decreasing(cfg):
    teff = cfg.physics.track_efficiency
    ranges = np.array([0.0, 1000.0, 5000.0])
    eta = track_efficiency(1.0, ranges, base=teff.base, range_falloff=teff.range_falloff)
    assert np.all((eta >= 0.0) & (eta <= 1.0))
    assert np.all(np.diff(eta) < 0.0)  # worse with range


def test_track_efficiency_increases_with_track_quality(cfg):
    teff = cfg.physics.track_efficiency
    lo = track_efficiency(0.5, 1000.0, base=teff.base, range_falloff=teff.range_falloff)
    hi = track_efficiency(1.0, 1000.0, base=teff.base, range_falloff=teff.range_falloff)
    assert hi > lo


def test_kill_time_rises_with_range(cfg):
    alpha = cfg.weather("clear").alpha
    e_kill = cfg.drone_class("quad_small").hardness
    power = cfg.turret_defaults.power
    ranges = [100.0, 1000.0, 2500.0, 4500.0]
    times = [
        _kill_time(cfg, alpha=alpha, range_m=r, e_kill=e_kill, power=power)
        for r in ranges
    ]
    assert all(b > a for a, b in zip(times, times[1:])), times


def test_kill_time_rises_with_worse_weather(cfg):
    e_kill = cfg.drone_class("quad_small").hardness
    power = cfg.turret_defaults.power
    # Short range keeps delivered power finite/distinct across all profiles; at long
    # range high-alpha weather (fog/dust) drives delivered power to ~0 (kill -> inf),
    # which is the correct Beer-Lambert behaviour but not a strict-ordering test.
    rng = 300.0
    profiles = ["clear", "haze", "rain", "fog", "dust"]
    times = [
        _kill_time(cfg, alpha=cfg.weather(p).alpha, range_m=rng, e_kill=e_kill, power=power)
        for p in profiles
    ]
    assert all(b > a for a, b in zip(times, times[1:])), times


def test_kill_time_rises_with_hardness(cfg):
    """Tougher drone class (fixed_wing) takes longer than quad_small, all else equal."""
    alpha = cfg.weather("clear").alpha
    power = cfg.turret_defaults.power
    rng = 1500.0
    soft = _kill_time(
        cfg, alpha=alpha, range_m=rng,
        e_kill=cfg.drone_class("quad_small").hardness, power=power,
    )
    hard = _kill_time(
        cfg, alpha=alpha, range_m=rng,
        e_kill=cfg.drone_class("fixed_wing").hardness, power=power,
    )
    assert hard > soft


def test_dwell_to_kill_infinite_when_no_deposition(cfg):
    # Zero deposition rate (out of reach) -> unkillable, not a crash.
    assert dwell_to_kill(40.0, 0.0) == math.inf


# --------------------------------------------------------------------------- #
# 8.4 Slew (setup) model: MONOTONICITY gate                                   #
# --------------------------------------------------------------------------- #


def test_slew_time_rises_with_angular_distance(cfg):
    sr = cfg.turret_defaults.slew_rate
    st = cfg.turret_defaults.settle_time
    base = 0.0
    targets = [0.1, 0.5, 1.0, 2.0, math.pi]
    times = [slew_time(base, a, slew_rate=sr, settle_time=st) for a in targets]
    assert all(b > a for a, b in zip(times, times[1:])), times


def test_slew_time_formula_and_settle_floor(cfg):
    sr = cfg.turret_defaults.slew_rate
    st = cfg.turret_defaults.settle_time
    # No rotation -> just the settle time.
    assert slew_time(0.7, 0.7, slew_rate=sr, settle_time=st) == pytest.approx(st)
    # General case matches angular_distance/slew_rate + settle_time.
    a, b = 0.2, 2.6
    expected = angular_distance(a, b) / sr + st
    assert slew_time(a, b, slew_rate=sr, settle_time=st) == pytest.approx(expected)


def test_slew_time_symmetric(cfg):
    sr = cfg.turret_defaults.slew_rate
    st = cfg.turret_defaults.settle_time
    assert slew_time(0.3, 2.9, slew_rate=sr, settle_time=st) == pytest.approx(
        slew_time(2.9, 0.3, slew_rate=sr, settle_time=st)
    )


def test_slew_time_rejects_nonpositive_rate(cfg):
    with pytest.raises(ValueError):
        slew_time(0.0, 1.0, slew_rate=0.0, settle_time=cfg.turret_defaults.settle_time)


# --------------------------------------------------------------------------- #
# 8.5 Thermal model: forced cooldown + hysteresis                             #
# --------------------------------------------------------------------------- #


def test_thermal_heats_while_firing(cfg):
    th = cfg.turret_defaults.thermal
    res = thermal_update(
        0.0, firing=True, forced_cooldown=False, dt=1.0,
        heat_rate=th.heat_rate, cool_rate=th.cool_rate,
        h_max=th.h_max, h_resume=th.h_resume,
    )
    assert res.heat == pytest.approx(th.heat_rate)
    assert res.forced_cooldown is False


def test_thermal_cools_while_idle_and_floors_at_zero(cfg):
    th = cfg.turret_defaults.thermal
    res = thermal_update(
        th.cool_rate * 0.5, firing=False, forced_cooldown=False, dt=1.0,
        heat_rate=th.heat_rate, cool_rate=th.cool_rate,
        h_max=th.h_max, h_resume=th.h_resume,
    )
    assert res.heat == 0.0  # floored, not negative


def test_thermal_cap_forces_cooldown(cfg):
    """Firing past h_max latches forced cooldown (mvp.md section 4 gate)."""
    th = cfg.turret_defaults.thermal
    # Start just below the cap, fire enough to cross it.
    start = th.h_max - th.heat_rate * 0.5
    res = thermal_update(
        start, firing=True, forced_cooldown=False, dt=1.0,
        heat_rate=th.heat_rate, cool_rate=th.cool_rate,
        h_max=th.h_max, h_resume=th.h_resume,
    )
    assert res.heat >= th.h_max
    assert res.forced_cooldown is True


def test_thermal_latch_holds_and_ignores_fire_request_until_resume(cfg):
    """While latched, the turret cools even if firing is requested, until h_resume."""
    th = cfg.turret_defaults.thermal
    # Drive into cooldown.
    state = thermal_update(
        th.h_max, firing=True, forced_cooldown=False, dt=0.0,
        heat_rate=th.heat_rate, cool_rate=th.cool_rate,
        h_max=th.h_max, h_resume=th.h_resume,
    )
    assert state.forced_cooldown is True
    prev_heat = state.heat
    # Keep requesting fire; latch must keep cooling (firing ignored) until h_resume.
    steps = 0
    while state.forced_cooldown:
        state = thermal_update(
            state.heat, firing=True, forced_cooldown=state.forced_cooldown, dt=1.0,
            heat_rate=th.heat_rate, cool_rate=th.cool_rate,
            h_max=th.h_max, h_resume=th.h_resume,
        )
        assert state.heat < prev_heat  # strictly cooling despite firing=True
        prev_heat = state.heat
        steps += 1
        assert steps < 10_000, "cooldown never released"
    # Latch releases only at/below h_resume.
    assert state.heat <= th.h_resume


def test_thermal_below_cap_does_not_latch(cfg):
    th = cfg.turret_defaults.thermal
    res = thermal_update(
        th.h_resume + 1.0, firing=False, forced_cooldown=False, dt=0.0,
        heat_rate=th.heat_rate, cool_rate=th.cool_rate,
        h_max=th.h_max, h_resume=th.h_resume,
    )
    assert res.forced_cooldown is False


def test_thermal_rejects_bad_thresholds(cfg):
    th = cfg.turret_defaults.thermal
    with pytest.raises(ValueError):
        thermal_update(
            0.0, firing=False, forced_cooldown=False, dt=1.0,
            heat_rate=th.heat_rate, cool_rate=th.cool_rate,
            h_max=th.h_resume, h_resume=th.h_max,  # inverted
        )
    with pytest.raises(ValueError):
        thermal_update(
            0.0, firing=False, forced_cooldown=False, dt=-1.0,
            heat_rate=th.heat_rate, cool_rate=th.cool_rate,
            h_max=th.h_max, h_resume=th.h_resume,
        )


# --------------------------------------------------------------------------- #
# Energy bookkeeping conservation (mvp.md section 4 gate)                      #
# --------------------------------------------------------------------------- #


def test_energy_bookkeeping_conserved(cfg):
    """Integrating deposition_rate over the dwell time deposits exactly E_kill.

    Continuous engagement: energy_absorbed(d_ij) = deposition_rate * d_ij == E_kill.
    """
    teff = cfg.physics.track_efficiency
    alpha = cfg.weather("haze").alpha
    power = cfg.turret_defaults.power
    e_kill = cfg.drone_class("quad_small").hardness
    rng = 1800.0

    p_del = delivered_power(power, alpha, rng)
    eta = track_efficiency(1.0, rng, base=teff.base, range_falloff=teff.range_falloff)
    dep = float(deposition_rate(p_del, eta))
    d = float(dwell_to_kill(e_kill, dep))

    deposited = dep * d
    assert deposited == pytest.approx(e_kill)


def test_energy_bookkeeping_partial_then_complete(cfg):
    """Energy accumulates linearly under continuous engagement; sums to E_kill at d."""
    teff = cfg.physics.track_efficiency
    alpha = cfg.weather("clear").alpha
    power = cfg.turret_defaults.power
    e_kill = cfg.drone_class("fixed_wing").hardness
    rng = 900.0

    p_del = delivered_power(power, alpha, rng)
    eta = track_efficiency(1.0, rng, base=teff.base, range_falloff=teff.range_falloff)
    dep = float(deposition_rate(p_del, eta))
    d = float(dwell_to_kill(e_kill, dep))

    # Split the dwell into N steps; cumulative energy must reach E_kill, no more.
    n = 7
    dt = d / n
    absorbed = 0.0
    for _ in range(n):
        absorbed += dep * dt
    assert absorbed == pytest.approx(e_kill)
    # Half the dwell deposits half the kill energy (linearity).
    assert dep * (d / 2.0) == pytest.approx(e_kill / 2.0)
