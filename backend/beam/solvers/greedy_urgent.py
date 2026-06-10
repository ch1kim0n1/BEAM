"""Greedy solver #3 - most-urgent-killable first (pdd.md sections 9.2.3, 7).

Among the targets a turret can *still kill before they leak* (their hard deadline
``TTI_j``, pdd.md section 7.1), service the one with the **tightest feasible
deadline** first. This is the "most-urgent-killable" heuristic of pdd.md section
9.2.3 - usually the strongest of the three greedies because it never wastes a beam on
a target that cannot be saved, and it front-loads the ones about to leak.

Model (pdd.md section 7.2). For one turret ``i`` servicing an ordered set of targets,
the completion time of the k-th target accumulates sequence-dependent slew setup and
dwell-to-kill::

    C(pi_1) = s_i(aim, pi_1) + d_i,pi_1
    C(pi_k) = C(pi_{k-1}) + s_i(pi_{k-1}, pi_k) + d_i,pi_k

A target is *killed* iff ``C(j) <= TTI_j`` (and it is in range / has LOS). The
objective is the value of targets killed before their deadline (pdd.md section 7.3).

All physics estimates come from :mod:`beam.engine.physics`:

- slew setup ``s_i`` from :func:`~beam.engine.physics.slew_time`,
- dwell-to-kill ``d_ij`` from the Beer-Lambert delivered-power chain
  (:func:`~beam.engine.physics.delivered_power` ->
  :func:`~beam.engine.physics.track_efficiency` ->
  :func:`~beam.engine.physics.deposition_rate` ->
  :func:`~beam.engine.physics.dwell_to_kill`).

No physics or cost constant is hard-coded here: ``power``, ``slew_rate``,
``settle_time`` and ``range_max`` come off the :class:`~beam.schemas.Turret` (resolved
from config upstream), and ``weather_alpha`` and the track-efficiency coefficients are
carried by the :class:`~beam.schemas.WorldState`. The track-efficiency ``base`` /
``range_falloff`` are not on the wire snapshot, so this solver uses the documented
config defaults (pdd.md section 18) as a degradation-free estimate; the objective is a
self-reported *estimate* (pdd.md section 9.1), the engine is the source of truth for
realized kills.

Determinism (pdd.md section 9.1): this solver reads only ``state``, never mutates it,
uses no global RNG, and iterates in fixed (snapshot) order, so it is a pure function of
``state``. It is cheap (no search) and finishes well inside any sane ``deadline_ms``;
it still checks the wall clock and returns its best-so-far if the budget is hit.
"""

from __future__ import annotations

import math
import time

from beam.engine import physics
from beam.engine.kinematics import time_to_impact
from beam.schemas import Assignment, Drone, Turret, WorldState
from beam.solvers.base import register
from beam.util import heading_to, vdist

# Track-efficiency coefficients are not carried on the WorldState snapshot (only the
# resolved weather alpha is). We read them from config (pdd.md section 18) so the
# dwell-to-kill *estimate* used for scoring matches the falloff the engine actually
# integrates - otherwise a steeper hardcoded falloff makes distant targets look
# unkillable and the solver under-engages at range. Same import-time read as
# greedy_threat; falls back to published defaults if config is unavailable.
_TRACK_QUALITY: float = 1.0
_TRACK_BASE: float = 1.0
_TRACK_RANGE_FALLOFF: float = 0.0015

try:  # pragma: no cover - config is present in normal operation
    from beam.config import load_config as _load_config

    _te = _load_config().physics.track_efficiency
    _TRACK_BASE = float(_te.base)
    _TRACK_RANGE_FALLOFF = float(_te.range_falloff)
except Exception:  # pragma: no cover - fall back to published defaults
    pass


def _remaining_e_kill(drone: Drone) -> float:
    """Energy still needed to kill ``drone`` (E_kill minus already-absorbed)."""
    return max(0.0, drone.hardness - drone.energy_absorbed)


def _dwell_estimate(turret: Turret, drone: Drone, weather_alpha: float) -> float:
    """Estimated continuous dwell-to-kill (s) for ``turret`` on ``drone``.

    Uses the full Beer-Lambert deposition chain from :mod:`beam.engine.physics`,
    against the target's *remaining* energy budget. Returns ``+inf`` when the target
    cannot be hurt from here (deposition underflows to zero).
    """
    rng = vdist(turret.pos, drone.pos)
    p_del = physics.delivered_power(turret.power, weather_alpha, rng)
    eta = physics.track_efficiency(
        _TRACK_QUALITY, rng, base=_TRACK_BASE, range_falloff=_TRACK_RANGE_FALLOFF
    )
    dep = physics.deposition_rate(p_del, eta)
    return float(physics.dwell_to_kill(_remaining_e_kill(drone), dep))


def _slew_estimate(turret: Turret, aim_from: float, drone: Drone) -> float:
    """Estimated slew setup (s) to swing ``turret`` from ``aim_from`` onto ``drone``."""
    aim_to = heading_to(turret.pos, drone.pos)
    return physics.slew_time(
        aim_from, aim_to, slew_rate=turret.slew_rate, settle_time=turret.settle_time
    )


def _in_range(turret: Turret, drone: Drone) -> bool:
    """Range feasibility ``range_ij <= R_i`` (pdd.md section 7.3). LOS is 1 in v1."""
    return vdist(turret.pos, drone.pos) <= turret.range_max


@register("greedy_urgent")
class GreedyUrgent:
    """Most-urgent-killable greedy assignment (pdd.md section 9.2.3).

    Construction takes an optional ``seed`` for interface parity with stochastic
    solvers; this policy is deterministic and does not use it.
    """

    name = "greedy_urgent"

    def __init__(self, seed: int = 0) -> None:
        # Stored only for parity with the Solver construction convention; this solver
        # is fully deterministic and never consumes randomness (pdd.md section 9.1).
        self._seed = seed

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Assign targets most-urgent-killable-first, per turret (pdd.md 9.2.3).

        Each live target is claimed by at most one turret. We sweep targets in
        ascending TTI (tightest deadline first); for each we pick the turret that can
        complete the kill before the deadline with the **earliest completion time**,
        appending it to that turret's running schedule (whose accumulated completion
        time and current aim advance with each claim). Targets that no turret can kill
        before they leak are left un-engaged.

        Respects ``deadline_ms`` wall-clock: returns the best-so-far assignment built
        up to that point if the budget is exhausted mid-sweep.
        """
        t_start = time.monotonic()
        budget_s = max(0.0, deadline_ms / 1000.0)

        asset = state.asset_pos
        alpha = state.weather_alpha

        # Per-turret running schedule state: accumulated completion time C() and the
        # aim heading after the last claimed target (drives the next slew estimate).
        sched_time: dict[str, float] = {tu.id: 0.0 for tu in state.turrets}
        sched_aim: dict[str, float] = {tu.id: tu.aim for tu in state.turrets}
        turret_by_id: dict[str, Turret] = {tu.id: tu for tu in state.turrets}

        orders: dict[str, list[str]] = {tu.id: [] for tu in state.turrets}
        objective_estimate = 0.0

        # Live, engageable targets only. Compute each TTI once (the deadline TTI_j).
        live = [d for d in state.drones if d.state in ("alive", "engaged")]
        tti: dict[str, float] = {d.id: time_to_impact(d, asset) for d in live}

        # Tightest feasible deadline first. Ties broken by drone id for a fully
        # deterministic order independent of input list ordering (pdd.md section 9.1).
        ordered_targets = sorted(live, key=lambda d: (tti[d.id], d.id))

        for drone in ordered_targets:
            # Honor the wall-clock budget: stop adding and return best-so-far.
            if time.monotonic() - t_start >= budget_s:
                break

            deadline = tti[drone.id]
            best_turret: str | None = None
            best_completion = math.inf

            for tu in state.turrets:
                if not _in_range(tu, drone):
                    continue
                dwell = _dwell_estimate(tu, drone, alpha)
                if not math.isfinite(dwell):
                    continue  # cannot be hurt from here
                slew = _slew_estimate(tu, sched_aim[tu.id], drone)
                completion = sched_time[tu.id] + slew + dwell
                # "Killable before TTI": completion must meet the hard deadline.
                if completion > deadline:
                    continue
                # Among feasible turrets pick the earliest completion; tie-break by
                # turret id for determinism.
                if completion < best_completion or (
                    completion == best_completion
                    and (best_turret is None or tu.id < best_turret)
                ):
                    best_completion = completion
                    best_turret = tu.id

            if best_turret is None:
                continue  # un-killable before its deadline -> leave un-engaged

            # Commit the claim: advance that turret's schedule and aim.
            orders[best_turret].append(drone.id)
            sched_time[best_turret] = best_completion
            sched_aim[best_turret] = heading_to(
                turret_by_id[best_turret].pos, drone.pos
            )
            objective_estimate += drone.value

        # Drop turrets with no assigned targets (Assignment omits un-used turrets).
        turret_orders = {tid: tids for tid, tids in orders.items() if tids}
        return Assignment(
            turret_orders=turret_orders, objective_estimate=objective_estimate
        )


__all__ = ["GreedyUrgent"]
