"""Greedy nearest-first solver (pdd.md sections 9.2.1, 7) - the baseline floor.

Each turret services the closest in-range, line-of-sight target first, then chains
on additional reachable targets in nearest-next order, building a per-turret firing
sequence. A target is handled by at most one turret (pdd.md 7.3 constraint
``Sum_i x_ij <= 1``). This is the simplest interchangeable :class:`Solver` and the
lower bound every smarter policy is measured against (pdd.md 9.2.1).

The reported objective is the value of targets this policy expects to **kill before
they leak**, i.e. before their time-to-impact (pdd.md 7.2/7.3): for each turret we
walk its order accumulating completion times

    C(pi_1) = s_i(aim, pi_1) + d_i,pi_1
    C(pi_k) = C(pi_{k-1}) + s_i(pi_{k-1}, pi_k) + d_i,pi_k

(pdd.md 7.2) and count a target's value iff ``C(j) <= TTI_j``. ``s`` is the slew
(setup) time (pdd.md 8.4) and ``d`` the dwell-to-kill time (pdd.md 8.3), both from
:mod:`beam.engine.physics`.

Determinism (mandatory): selection is a pure function of the :class:`WorldState`
(distances, with stable target/turret iteration order as the tie-break). No global
RNG is touched; the constructor still accepts a ``seed`` to satisfy the solver
contract (and for symmetry with stochastic solvers), but this policy is deterministic
regardless of it.

NO hardcoded physics constants: every coefficient (track-efficiency ``base`` /
``range_falloff``) is read from :mod:`beam.config` at construction; the per-turret
power, range, slew rate, settle time, and the resolved ``weather_alpha`` come from the
:class:`WorldState` snapshot. The drone ``hardness`` is its configured ``E_kill``.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from beam.config import load_config
from beam.engine import physics
from beam.schemas import Assignment, Drone, Turret, Vec2, WorldState
from beam.solvers.base import register
from beam.util import vdist

__all__ = ["GreedyNearestSolver"]


def _time_to_impact(pos: Vec2, vel: Vec2, asset_pos: Vec2) -> float:
    """Time (s) until a target at ``pos`` moving at ``vel`` reaches ``asset_pos``.

    Mirrors :func:`beam.engine.kinematics.time_to_impact` but works off the plain
    vectors carried in the :class:`WorldState` snapshot (the solver reads only state).
    Closing speed is the velocity component along the line to the asset; non-positive
    closing speed (stationary / receding) yields ``+inf`` (never impacts).
    """
    to_asset_x = asset_pos.x - pos.x
    to_asset_y = asset_pos.y - pos.y
    dist = math.hypot(to_asset_x, to_asset_y)
    if dist <= 1e-12:
        return 0.0
    ux, uy = to_asset_x / dist, to_asset_y / dist
    closing = vel.x * ux + vel.y * uy
    if closing <= 1e-12:
        return math.inf
    return dist / closing


@register("greedy_nearest")
class GreedyNearestSolver:
    """Nearest-first greedy assignment (pdd.md 9.2.1). Baseline floor solver."""

    name = "greedy_nearest"

    def __init__(
        self,
        seed: int = 0,
        *,
        track_base: Optional[float] = None,
        track_range_falloff: Optional[float] = None,
    ) -> None:
        """Construct the solver.

        Args:
            seed: accepted for contract symmetry; this policy is deterministic and
                does not consume randomness.
            track_base: ``physics.track_efficiency.base`` override. Defaults to the
                value loaded from ``config/defaults.yaml`` (no hardcoded constant).
            track_range_falloff: ``physics.track_efficiency.range_falloff`` override.
                Defaults to the configured value.
        """
        self.seed = seed
        if track_base is None or track_range_falloff is None:
            te = load_config().physics.track_efficiency
            if track_base is None:
                track_base = te.base
            if track_range_falloff is None:
                track_range_falloff = te.range_falloff
        self._track_base = float(track_base)
        self._track_range_falloff = float(track_range_falloff)

    # ------------------------------------------------------------------ #
    # Feasibility / scoring helpers (pure functions of the snapshot)      #
    # ------------------------------------------------------------------ #

    def _has_los(self, turret: Turret, drone: Drone, state: WorldState) -> bool:
        """Line-of-sight test (pdd.md 7.1 ``LOS_ij``).

        v1 has no terrain / elevation occlusion (that is post-MVP, mvp.md section 3),
        so LOS is unobstructed for any target the turret can physically reach. This is
        a single, explicit hook so terrain masking can drop in later without touching
        the selection logic.
        """
        return True

    def _in_range(self, turret: Turret, drone: Drone) -> bool:
        return vdist(turret.pos, drone.pos) <= turret.range_max

    def _engageable(self, turret: Turret, drone: Drone, state: WorldState) -> bool:
        """A target is a candidate for a turret iff it is live, in range, and in LOS."""
        if drone.state in ("dead", "leaked"):
            return False
        return self._in_range(turret, drone) and self._has_los(turret, drone, state)

    def _dwell_to_kill(self, turret: Turret, drone: Drone, weather_alpha: float) -> float:
        """Continuous dwell-to-kill time (s) for ``turret`` on ``drone`` (pdd.md 8.3).

        Composes the physics pipeline (delivered power -> track efficiency ->
        deposition rate -> dwell). Returns ``+inf`` if the target cannot be killed from
        here (deposition underflows). Accounts for energy already absorbed: only the
        *remaining* E_kill must still be deposited.
        """
        rng = vdist(turret.pos, drone.pos)
        delivered = physics.delivered_power(turret.power, weather_alpha, rng)
        eta = physics.track_efficiency(
            1.0,
            rng,
            base=self._track_base,
            range_falloff=self._track_range_falloff,
        )
        dep = physics.deposition_rate(delivered, eta)
        remaining = max(0.0, drone.hardness - drone.energy_absorbed)
        return float(physics.dwell_to_kill(remaining, dep))

    def _slew_time(self, turret: Turret, from_aim: float, drone: Drone) -> float:
        """Setup time (s) to re-aim ``turret`` from ``from_aim`` onto ``drone`` (8.4)."""
        target_aim = math.atan2(drone.pos.y - turret.pos.y, drone.pos.x - turret.pos.x)
        return physics.slew_time(
            from_aim,
            target_aim,
            slew_rate=turret.slew_rate,
            settle_time=turret.settle_time,
        )

    # ------------------------------------------------------------------ #
    # Solve                                                               #
    # ------------------------------------------------------------------ #

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Greedy nearest-first assignment (pdd.md 9.1, 9.2.1).

        For each turret in battery order, repeatedly take the nearest still-unassigned,
        engageable target and append it to that turret's firing order, chaining the
        completion-time bookkeeping (pdd.md 7.2) to score the objective. A target is
        claimed by exactly one turret (the first turret, in battery order, that reaches
        it as its nearest). Respects ``deadline_ms``: returns the best assignment built
        so far if the wall-clock budget is exhausted.
        """
        start = time.perf_counter()
        deadline_s = max(0.0, deadline_ms) / 1000.0

        def out_of_time() -> bool:
            return (time.perf_counter() - start) >= deadline_s

        # Precompute TTI per live drone (deterministic, fixed iteration order).
        tti: dict[str, float] = {
            d.id: _time_to_impact(d.pos, d.vel, state.asset_pos)
            for d in state.drones
        }
        drone_by_id: dict[str, Drone] = {d.id: d for d in state.drones}

        assigned: set[str] = set()
        turret_orders: dict[str, list[str]] = {}
        objective = 0.0

        for turret in state.turrets:
            if out_of_time():
                break

            order: list[str] = []
            cur_aim = turret.aim
            completion = 0.0  # cumulative C() along this turret's order

            # Greedily chain nearest reachable targets until none remain feasible.
            while True:
                if out_of_time():
                    break

                # Nearest unassigned engageable target to this turret.
                best: Optional[Drone] = None
                best_dist = math.inf
                for drone in state.drones:  # fixed order -> deterministic tie-break
                    if drone.id in assigned:
                        continue
                    if not self._engageable(turret, drone, state):
                        continue
                    dist = vdist(turret.pos, drone.pos)
                    if dist < best_dist:
                        best_dist = dist
                        best = drone

                if best is None:
                    break

                # Completion time if we service this target next (pdd.md 7.2).
                s = self._slew_time(turret, cur_aim, best)
                d = self._dwell_to_kill(turret, best, state.weather_alpha)
                next_completion = completion + s + d

                # Always claim it (nearest-first commits regardless of feasibility:
                # the baseline floor does not look ahead). Score only if it can be
                # killed before it leaks (C(j) <= TTI_j).
                assigned.add(best.id)
                order.append(best.id)
                if next_completion <= tti.get(best.id, math.inf):
                    objective += best.value

                completion = next_completion
                cur_aim = math.atan2(
                    best.pos.y - turret.pos.y, best.pos.x - turret.pos.x
                )

            if order:
                turret_orders[turret.id] = order

        _ = drone_by_id  # retained for readability/extension; no-op
        return Assignment(turret_orders=turret_orders, objective_estimate=objective)
