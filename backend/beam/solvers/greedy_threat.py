"""Greedy highest-threat-first solver (pdd.md sections 9.2.2, 7).

Build-order solver #2 of the suite. The policy is the textbook *value-density per
urgency* greedy for Weapon-Target Assignment (pdd.md section 9.2.2):

    priority(j) = v_j / TTI_j

i.e. high-value drones that will impact soonest are engaged first. We sort the live
target set by this ratio (descending) and walk it once, assigning each target to the
turret that can still *feasibly kill it before its deadline*, given that turret's
already-committed firing schedule for this epoch.

Feasibility mirrors the battery problem (pdd.md section 7.3):

    - range gate     : range_ij <= R_i                       (out-of-range -> skip)
    - LOS gate       : x_ij <= LOS_ij                        (v1: no terrain masking,
                       so LOS is true for any in-range target; gate kept explicit so
                       a future LOS field drops straight in)
    - schedule       : C_i(j) = (running completion time of turret i) + s_i + d_ij,
                       a target is killable iff C_i(j) <= TTI_j           (pdd.md 7.2)
    - thermal        : the dwell d_ij must fit under the turret's remaining thermal
                       headroom before it would trip forced cooldown      (pdd.md 8.5)

The reported ``objective_estimate`` is the total value of targets this assignment
expects to kill before their deadline - exactly the objective the optimality gap is
measured against (pdd.md sections 7.3, 9.3).

Determinism (pdd.md section 21): the solver reads only ``state`` and uses no RNG. The
priority sort is made total by a deterministic tie-break on target id, and turret
iteration follows ``state.turrets`` order, so identical input -> identical output.
The dwell / slew / thermal math is delegated to ``beam.engine.physics`` so there are
no hard-coded physics constants here (pdd.md section 18).
"""

from __future__ import annotations

import math
import time
from typing import Optional

from beam.engine import physics
from beam.schemas import Assignment, Drone, Turret, WorldState
from beam.solvers.base import register
from beam.util import angular_distance, heading_to, vdist

# Track quality is not yet a per-target sensor field in v1's WorldState; the physics
# track-efficiency model lets range drive the falloff (pdd.md 8.3), so callers pass a
# perfect nominal track and let distance degrade eta. This is a modeling default for
# the v1 snapshot, not a tunable physics constant.
_NOMINAL_TRACK_QUALITY = 1.0

# physics.track_efficiency needs base / range_falloff. These live in config
# (physics.track_efficiency.{base,range_falloff}) but are NOT on the solver-facing
# WorldState (pdd.md section 13: the solver gets only the resolved weather alpha). We
# therefore use the documented config defaults (pdd.md section 18) as the solver's
# scoring assumption; the engine's authoritative kill resolution uses the real config.
# These are the same published defaults, kept here so the solver can reason about
# dwell-to-kill without config access. They are read once from config when available.
_DEFAULT_TRACK_BASE = 1.0
_DEFAULT_RANGE_FALLOFF = 0.0015

try:  # pragma: no cover - config is present in normal operation
    from beam.config import load_config as _load_config

    _cfg = _load_config()
    _DEFAULT_TRACK_BASE = float(_cfg.physics.track_efficiency.base)
    _DEFAULT_RANGE_FALLOFF = float(_cfg.physics.track_efficiency.range_falloff)
except Exception:  # pragma: no cover - fall back to published defaults
    pass


def physics_time_to_impact(drone: Drone, asset_pos) -> float:
    """TTI helper kept solver-local so we depend only on geometry in ``state``.

    Closing speed = component of velocity toward the asset; non-positive closing speed
    means the drone never impacts under current motion (TTI = +inf). Mirrors
    ``beam.engine.kinematics.time_to_impact`` without importing the whole kinematics
    module (the solver only needs this one read-only computation).
    """
    dx = asset_pos.x - drone.pos.x
    dy = asset_pos.y - drone.pos.y
    dist = math.hypot(dx, dy)
    if dist <= 1e-12:
        return 0.0
    ux, uy = dx / dist, dy / dist
    closing = drone.vel.x * ux + drone.vel.y * uy
    if closing <= 1e-12:
        return math.inf
    return dist / closing


def _dwell_to_kill(turret: Turret, drone: Drone, rng_m: float, weather_alpha: float) -> float:
    """Continuous dwell (s) for ``turret`` to kill ``drone`` at range ``rng_m``.

    Delegates the Beer-Lambert + track-efficiency + energy-budget chain to
    ``beam.engine.physics`` (pdd.md sections 8.2-8.3); no constants are inlined.
    Returns ``+inf`` if delivered power is too low to ever kill from here.
    """
    p_del = float(physics.delivered_power(turret.power, weather_alpha, rng_m))
    eta = float(
        physics.track_efficiency(
            _NOMINAL_TRACK_QUALITY,
            rng_m,
            base=_DEFAULT_TRACK_BASE,
            range_falloff=_DEFAULT_RANGE_FALLOFF,
        )
    )
    dep = float(physics.deposition_rate(p_del, eta))
    return float(physics.dwell_to_kill(drone.hardness, dep))


def _thermal_headroom_seconds(turret: Turret) -> float:
    """Max continuous firing time (s) before this turret trips forced cooldown.

    A turret already at/over ``h_max`` (or with no heat_rate) has zero headroom. Pure
    function of the turret's current ``thermal`` and its ``thermal_cfg`` (pdd.md 8.5).
    """
    cfg = turret.thermal_cfg
    if cfg.heat_rate <= 0.0:
        return math.inf  # never heats -> unlimited dwell (degenerate config)
    if turret.thermal >= cfg.h_max:
        return 0.0
    return (cfg.h_max - turret.thermal) / cfg.heat_rate


@register("greedy_threat")
class GreedyThreat:
    """Highest-threat-first greedy assignment (pdd.md section 9.2.2).

    Prioritizes targets by value density per urgency ``v_j / TTI_j`` and assigns each,
    in that order, to the turret that can still kill it before its deadline given the
    turret's running schedule. Respects the Solver Protocol (pdd.md section 9.1).
    """

    name = "greedy_threat"

    def __init__(self, seed: Optional[int] = None) -> None:
        # No randomness in this policy; seed accepted for interface symmetry with the
        # stochastic solvers (pdd.md section 9.1: derive any randomness from a seed).
        self._seed = seed

    # ------------------------------------------------------------------ #
    # Solver Protocol                                                    #
    # ------------------------------------------------------------------ #

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Return the highest-threat-first assignment for this epoch.

        Reads only ``state`` (never mutates it). Respects ``deadline_ms`` wall-clock:
        the single greedy pass is linear and fast, but we check the budget between
        targets and return the best-so-far assignment if it is exhausted (pdd.md 9.1).
        """
        start = time.perf_counter()
        budget_s = max(0.0, deadline_ms / 1000.0)

        asset_pos = state.asset_pos
        weather_alpha = state.weather_alpha

        # Only engage live/detected, still-killable targets.
        live = [d for d in state.drones if d.state in ("alive", "engaged")]

        # Per-target precompute: TTI and threat priority v/TTI (pdd.md 9.2.2).
        # TTI == 0 (already at asset) or +inf (not closing) are handled: a zero TTI is
        # un-killable (no time to fire) and gets lowest priority via +inf-guarded ratio;
        # an infinite TTI yields ratio 0 (no urgency) -> engaged last.
        priorities: list[tuple[float, str, Drone, float]] = []
        for d in live:
            tti = physics_time_to_impact(d, asset_pos)
            if tti <= 0.0 or not math.isfinite(tti):
                ratio = 0.0 if not math.isfinite(tti) else -math.inf
            else:
                ratio = d.value / tti
            priorities.append((ratio, d.id, d, tti))

        # Sort by priority desc, tie-break on id asc for a total, deterministic order.
        priorities.sort(key=lambda r: (-r[0], r[1]))

        # Per-turret running schedule state for this epoch.
        # running_C: completion time of the last committed target (starts at 0; the
        #            first slew is measured from the turret's current aim).
        # last_aim:  aim heading after the last committed target (or current aim).
        # heat_used: cumulative dwell seconds committed (against thermal headroom).
        turrets = state.turrets
        running_C: dict[str, float] = {t.id: 0.0 for t in turrets}
        last_aim: dict[str, float] = {t.id: t.aim for t in turrets}
        heat_used: dict[str, float] = {t.id: 0.0 for t in turrets}
        headroom: dict[str, float] = {t.id: _thermal_headroom_seconds(t) for t in turrets}
        # A turret currently latched in forced cooldown cannot fire this epoch.
        available: dict[str, bool] = {t.id: t.state != "cooldown" for t in turrets}

        orders: dict[str, list[str]] = {t.id: [] for t in turrets}
        objective = 0.0
        turret_by_id = {t.id: t for t in turrets}

        for ratio, did, drone, tti in priorities:
            # Deadline check between targets: return best-so-far if budget is spent.
            if budget_s > 0.0 and (time.perf_counter() - start) >= budget_s:
                break

            # An un-killable deadline (already arrived / not closing) is never engaged.
            if tti <= 0.0 or not math.isfinite(tti):
                continue

            best_turret: Optional[str] = None
            best_completion = math.inf

            for t in turrets:
                tid = t.id
                if not available[tid]:
                    continue

                rng_m = vdist(t.pos, drone.pos)
                # Range gate (pdd.md 7.3): only engage in-range targets.
                if rng_m > t.range_max:
                    continue
                # LOS gate (pdd.md 7.3): v1 has no terrain masking, so any in-range
                # target is in line of sight. Kept explicit as the insertion point for
                # a future LOS field on WorldState.
                # if not los(tid, did): continue

                dwell = _dwell_to_kill(t, drone, rng_m, weather_alpha)
                if not math.isfinite(dwell) or dwell <= 0.0:
                    if dwell <= 0.0:
                        # Degenerate zero-energy kill: still needs slew only.
                        dwell = 0.0
                    else:
                        continue  # unkillable from here (delivered power too low)

                # Thermal feasibility (pdd.md 8.5): the dwell must fit under remaining
                # thermal headroom for this turret this epoch.
                if heat_used[tid] + dwell > headroom[tid]:
                    continue

                # Schedule feasibility (pdd.md 7.2): completion time = running C +
                # slew from last aim + dwell, must beat the deadline.
                target_aim = heading_to(t.pos, drone.pos)
                slew = (
                    angular_distance(last_aim[tid], target_aim) / t.slew_rate
                    + t.settle_time
                )
                completion = running_C[tid] + slew + dwell
                if completion > tti:
                    continue

                # Prefer the turret that frees up soonest (earliest completion) so the
                # rest of the threat list still has scheduling slack. Tie-break on
                # turret id keeps it deterministic.
                if completion < best_completion or (
                    completion == best_completion
                    and (best_turret is None or tid < best_turret)
                ):
                    best_completion = completion
                    best_turret = tid

            if best_turret is None:
                continue  # no turret can kill this target before its deadline

            # Commit: append to the turret's order and advance its schedule state.
            t = turret_by_id[best_turret]
            target_aim = heading_to(t.pos, drone.pos)
            rng_m = vdist(t.pos, drone.pos)
            dwell = _dwell_to_kill(t, drone, rng_m, weather_alpha)
            if not math.isfinite(dwell):
                continue
            orders[best_turret].append(did)
            running_C[best_turret] = best_completion
            last_aim[best_turret] = target_aim
            heat_used[best_turret] += max(0.0, dwell)
            objective += drone.value

        # Drop empty turret entries so the Assignment lists only engaged turrets.
        turret_orders = {tid: lst for tid, lst in orders.items() if lst}
        return Assignment(turret_orders=turret_orders, objective_estimate=objective)
