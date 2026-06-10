"""Physics model (pdd.md section 8): the four configurable constraints.

This module implements the *illustrative, configuration-driven* physics of BEAM as a
set of **pure functions**. Nothing here reads globals, mutates inputs, or hard-codes a
physics constant: every coefficient (power, alpha, track-efficiency, slew rate,
thermal rates) is passed in by the caller, who resolves it from ``beam.config``
(pdd.md section 18). See pdd.md sections 8.1-8.6.

The four Phase-1 constraints (mvp.md section 4):

1. Atmospheric attenuation -- Beer-Lambert :func:`delivered_power`.
2. Dwell-to-kill           -- :func:`track_efficiency`, :func:`deposition_rate`,
                              :func:`dwell_to_kill`.
3. Slew (setup) time       -- :func:`slew_time`.
4. Thermal model           -- :func:`thermal_update` (heat/cool + forced-cooldown
                              hysteresis between ``h_max`` and ``h_resume``).

All functions accept plain floats and are numpy-friendly: where a function does
vectorizable math it accepts numpy arrays too (e.g. an array of ranges), returning a
numpy array of the same shape. Scalars in -> scalar out.

NOTE: This is a software OR/simulation tool. The numbers are internally consistent
and demonstrable, not sourced from real weapon-engineering data (pdd.md section 8,
section 18).
"""

from __future__ import annotations

from typing import NamedTuple, Union

import numpy as np

from beam.util import angular_distance

# Try to use the compiled Rust extension for hot-path functions.
# Falls back to pure numpy when the extension is not built (e.g. CI without Rust).
try:
    import beam_physics as _rust  # type: ignore[import]
    _RUST = True
except ImportError:
    _rust = None  # type: ignore[assignment]
    _RUST = False

# A value that is either a python float or a numpy array (for vectorized callers).
FloatOrArray = Union[float, np.ndarray]

# Numerical floor so deposition_rate -> dwell_to_kill never divides by zero. Not a
# physics constant: it is a guard against degenerate inputs (zero power / range so far
# that delivered power underflows). Effectively infinite dwell time.
_EPS = 1e-12


# --------------------------------------------------------------------------- #
# 8.2 Atmospheric attenuation (Beer-Lambert)                                  #
# --------------------------------------------------------------------------- #


def delivered_power(
    power_emitted: FloatOrArray,
    alpha: float,
    range_m: FloatOrArray,
) -> FloatOrArray:
    """Delivered beam power after Beer-Lambert atmospheric extinction (pdd.md 8.2).

    ``P_delivered(range) = P_emitted * exp(-alpha_weather * range)``

    Args:
        power_emitted: emitted power at the aperture (illustrative units). From
            ``turret.power`` / ``turret_defaults.power``.
        alpha: weather extinction coefficient (1/m). From the resolved
            ``WeatherProfile.alpha`` (``WorldState.weather_alpha``).
        range_m: turret->target distance in metres (scalar or array).

    Returns:
        Delivered power, same shape as ``range_m`` (or scalar). Monotonically
        decreasing in both ``alpha`` and ``range_m``.
    """
    return power_emitted * np.exp(-alpha * np.asarray(range_m, dtype=float))


# --------------------------------------------------------------------------- #
# 8.3 Dwell-to-kill                                                           #
# --------------------------------------------------------------------------- #


def track_efficiency(
    track_quality: FloatOrArray,
    range_m: FloatOrArray,
    *,
    base: float,
    range_falloff: float,
) -> FloatOrArray:
    """Beam-spread / jitter efficiency ``eta_track`` in [0, 1] (pdd.md 8.3).

    Efficiency degrades with range (beam spread / pointing jitter grow) and scales
    with how well the target is tracked::

        eta = clamp( base * track_quality * exp(-range_falloff * range), 0, 1 )

    Using an exponential range term keeps eta strictly positive and monotonically
    decreasing in range, mirroring the Beer-Lambert form so "closer + better-tracked"
    is always faster (pdd.md 8.3). ``track_quality`` is a unitless [0,1] fraction
    supplied by the caller (1.0 = perfect track); v1 callers typically pass 1.0 and
    let range drive the falloff.

    Args:
        track_quality: tracking quality in [0, 1] (scalar or array).
        range_m: turret->target distance in metres (scalar or array).
        base: ``physics.track_efficiency.base`` (config).
        range_falloff: ``physics.track_efficiency.range_falloff`` (1/m, config).

    Returns:
        Efficiency in [0, 1], same shape as the broadcast of the inputs.
    """
    tq = np.asarray(track_quality, dtype=float)
    rng = np.asarray(range_m, dtype=float)
    eta = base * tq * np.exp(-range_falloff * rng)
    return np.clip(eta, 0.0, 1.0)


def deposition_rate(
    power_delivered: FloatOrArray,
    eta_track: FloatOrArray,
) -> FloatOrArray:
    """Rate of energy deposition on a target (pdd.md 8.3).

    ``deposition_rate_ij = P_delivered(range_ij) * eta_track(track_quality, range)``

    Both factors are non-negative, so the rate is non-negative and increases with
    delivered power and with track efficiency.

    Args:
        power_delivered: from :func:`delivered_power`.
        eta_track: from :func:`track_efficiency`, in [0, 1].

    Returns:
        Deposition rate (energy units / second), shape = broadcast of inputs.
    """
    return np.asarray(power_delivered, dtype=float) * np.asarray(eta_track, dtype=float)


def dwell_to_kill(
    e_kill: FloatOrArray,
    deposition: FloatOrArray,
) -> FloatOrArray:
    """Continuous dwell time to destroy a target (pdd.md 8.3).

    ``d_ij = E_kill_j / deposition_rate_ij``

    Energy-budget model only -- no material-penetration physics (pdd.md 8.3).
    ``E_kill_j = hardness_j`` (configurable per drone class, pdd.md 8.3 / 18).

    A non-positive deposition rate (target out of effective reach: zero delivered
    power or zero efficiency) yields ``+inf`` dwell -- i.e. unkillable from here --
    rather than a divide-by-zero error.

    Args:
        e_kill: energy required to kill (= drone ``hardness`` / ``E_kill``).
        deposition: deposition rate from :func:`deposition_rate`.

    Returns:
        Dwell-to-kill in seconds (or ``inf``), shape = broadcast of inputs.
        Monotonically increasing in range and in alpha (worse weather), since both
        shrink the deposition rate.
    """
    ek = np.asarray(e_kill, dtype=float)
    dep = np.asarray(deposition, dtype=float)
    return np.where(dep > _EPS, ek / np.where(dep > _EPS, dep, 1.0), np.inf)


# --------------------------------------------------------------------------- #
# 8.4 Slew (setup) model                                                      #
# --------------------------------------------------------------------------- #


def slew_time(
    aim_from: float,
    aim_to: float,
    *,
    slew_rate: float,
    settle_time: float,
) -> float:
    """Sequence-dependent setup time to re-aim a turret (pdd.md 8.4).

    ``s_i(a, b) = angular_distance(aim_a, aim_b) / slew_rate_i + settle_time_i``

    The angular distance is the shortest rotation in [0, pi] (``beam.util``), so the
    setup cost is symmetric in the two headings and rises monotonically with their
    separation. ``slew_rate`` and ``settle_time`` come from the turret / defaults
    config (rad/s and s respectively).

    Args:
        aim_from: current aim heading (rad).
        aim_to: target aim heading (rad).
        slew_rate: angular speed limit (rad/s, must be > 0).
        settle_time: fixed post-slew settle delay (s).

    Returns:
        Setup time in seconds (scalar).

    Raises:
        ValueError: if ``slew_rate`` is non-positive.
    """
    if slew_rate <= 0.0:
        raise ValueError(f"slew_rate must be positive, got {slew_rate!r}")
    return angular_distance(aim_from, aim_to) / slew_rate + settle_time


# --------------------------------------------------------------------------- #
# 8.5 Thermal model (with forced-cooldown hysteresis)                         #
# --------------------------------------------------------------------------- #


class ThermalResult(NamedTuple):
    """Outcome of one thermal step (pdd.md 8.5).

    Attributes:
        heat: new accumulated heat after ``dt`` (floored at 0, never below).
        forced_cooldown: whether the turret is (still) in forced cooldown after this
            step. While True the turret cannot fire regardless of orders, until heat
            falls to/below ``h_resume``.
    """

    heat: float
    forced_cooldown: bool


def thermal_update(
    heat: float,
    *,
    firing: bool,
    forced_cooldown: bool,
    dt: float,
    heat_rate: float,
    cool_rate: float,
    h_max: float,
    h_resume: float,
) -> ThermalResult:
    """Advance a turret's thermal state by ``dt`` seconds (pdd.md 8.5).

    Model::

        on fire:  H += heat_rate * dt
        on idle:  H -= cool_rate * dt          (floored at 0)
        if H >= H_max: turret forced to COOLDOWN until H <= H_resume

    The forced-cooldown latch is *hysteretic*: once heat reaches ``h_max`` the turret
    is latched into cooldown (and thus cools, regardless of the ``firing`` request)
    until heat drops to/below ``h_resume``; only then does it release. This caps the
    sustained duty cycle and forces the optimizer to spread load (pdd.md 8.5). The
    caller passes the previous latch state in ``forced_cooldown`` and stores the
    returned latch for the next step.

    All rates/caps come from the turret's :class:`~beam.schemas.ThermalConfig`
    (config; pdd.md 18). This function is pure: it returns a new value and never
    mutates the turret.

    Args:
        heat: current accumulated heat ``H_i``.
        firing: whether the scheduler wants this turret firing during ``dt``.
        forced_cooldown: previous forced-cooldown latch state.
        dt: timestep in seconds (>= 0).
        heat_rate: heat added per second while firing (config).
        cool_rate: heat removed per second while not firing (config).
        h_max: forced-cooldown trip point (config).
        h_resume: forced-cooldown release point (config, < h_max).

    Returns:
        :class:`ThermalResult` with the new heat and latch state.

    Raises:
        ValueError: if ``dt`` is negative or ``h_resume`` >= ``h_max``.
    """
    if dt < 0.0:
        raise ValueError(f"dt must be non-negative, got {dt!r}")
    if h_resume >= h_max:
        raise ValueError(
            f"h_resume ({h_resume!r}) must be strictly below h_max ({h_max!r})"
        )

    # A latched turret cannot fire: it cools no matter what the scheduler requested.
    effectively_firing = firing and not forced_cooldown

    if effectively_firing:
        new_heat = heat + heat_rate * dt
    else:
        new_heat = max(0.0, heat - cool_rate * dt)

    # Latch transitions (hysteresis):
    if forced_cooldown:
        # Stay latched until cooled to/below the resume threshold.
        new_forced = new_heat > h_resume
    else:
        # Trip into cooldown once we reach the cap.
        new_forced = new_heat >= h_max

    return ThermalResult(heat=new_heat, forced_cooldown=new_forced)


# --------------------------------------------------------------------------- #
# Batch wrappers - use Rust when compiled, numpy fallback otherwise            #
# --------------------------------------------------------------------------- #


def delivered_power_batch(
    power_emitted: float,
    alpha: float,
    ranges: np.ndarray,
) -> np.ndarray:
    """Vectorized Beer-Lambert over a range array. Uses Rust extension when available."""
    r = np.asarray(ranges, dtype=np.float64)
    if _RUST and _rust is not None:
        return _rust.delivered_power_batch(float(power_emitted), float(alpha), r)
    return delivered_power(power_emitted, alpha, r)


def dwell_matrix_batch(
    powers: np.ndarray,
    ranges: np.ndarray,
    hardness: np.ndarray,
    alpha: float,
    track_base: float,
    track_falloff: float,
) -> np.ndarray:
    """Full n_turrets x n_drones dwell-to-kill matrix. Uses Rust when available."""
    if _RUST and _rust is not None:
        return _rust.dwell_matrix(
            np.asarray(powers, dtype=np.float64),
            np.asarray(ranges, dtype=np.float64),
            np.asarray(hardness, dtype=np.float64),
            float(alpha),
            float(track_base),
            float(track_falloff),
        )
    # Numpy fallback
    n_t, n_d = ranges.shape
    out = np.full((n_t, n_d), np.inf)
    for i in range(n_t):
        p_del = delivered_power(powers[i], alpha, ranges[i])
        eta = track_efficiency(1.0, ranges[i], base=track_base, range_falloff=track_falloff)
        dep = deposition_rate(p_del, eta)
        out[i] = dwell_to_kill(hardness, dep)
    return out
