"""Auction / linear-assignment solver (pdd.md sections 7, 9.2.4).

This solver treats the battery problem (pdd.md 7.3) as a **linear assignment** on a
value/feasibility benefit matrix and then orders each turret's targets by deadline
(TTI), exactly as called for in pdd.md section 9.2.4:

    "Auction / Hungarian assignment. Solve the turret-to-target assignment as a
     linear assignment on a value/feasibility matrix; order per-turret by deadline."

Objective (pdd.md 7.2 / 7.3): the value reported is the total value of targets that
can be **killed before their TTI**. A target j handled by turret i counts iff its
completion time ``C_i(j) = slew_i->j + d_ij`` (and, for additional targets queued on
the same turret, the cumulative single-machine completion time of pdd.md 7.2) is at
most ``TTI_j``. ``d_ij`` (dwell-to-kill) and the slew setup time come from
``beam.engine.physics`` so range, weather (``weather_alpha``) and thermal load drive
the score — no physics constants are duplicated here.

Algorithm
---------
1. Build the benefit matrix ``a[i][j]`` over turrets x in-range / in-LOS targets:
   ``a[i][j] = v_j`` if turret i can kill j before ``TTI_j`` from its current aim
   (a single, first-in-queue kill), else ``0`` (infeasible / un-profitable).
2. Solve the maximum-weight linear assignment (one target per turret) with a
   self-contained **Bertsekas auction** (the solver's namesake) — deterministic,
   integer-free, and dependency-light.
3. For each turret, starting from its assigned primary target, greedily append more
   unassigned feasible targets in **earliest-deadline (smallest TTI) order**, keeping
   the single-machine schedule (pdd.md 7.2) feasible (each appended target's
   cumulative completion time must still beat its TTI). A target is assigned to at
   most one turret (pdd.md 7.3 constraint).
4. ``objective_estimate`` = total value of all on-time targets across the battery.

Determinism (contract): no global RNG. The auction's optional tie-break jitter is
drawn from a per-instance ``numpy`` generator seeded from a fixed solver seed plus a
hash of the snapshot, so identical ``WorldState`` -> identical output. The deadline /
``deadline_ms`` budget is honored: the construction is near-instant, but the auction
loop checks the wall clock and returns the best assignment found so far if hit.

This is a software OR/simulation tool; all numbers are illustrative and config-driven.
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np

from beam.engine import kinematics, physics
from beam.schemas import Assignment, Drone, Turret, WorldState
from beam.solvers.base import register
from beam.util import angular_distance, make_rng, vdist

__all__ = ["AuctionSolver"]

# Numerical floor shared with the physics module's reasoning about reach.
_EPS = 1e-12

# Solver hysteresis: multiplicative bias the matching gives a turret's current target
# so the value-maximizing assignment does not reshuffle primaries every epoch (which
# re-slews turrets off a target mid-dwell and loses progress). Algorithm tunable, not a
# physics/cost constant. >1 favors commitment (pdd.md 6: "commit through to kill or abort").
_STICKINESS_BONUS = 1.5


@register("auction")
class AuctionSolver:
    """Linear-assignment (auction) solver (pdd.md 9.2.4).

    Construction takes an optional ``seed`` so any tie-breaking is reproducible; the
    solver derives all randomness from it deterministically (contract: no hidden global
    RNG). ``track_base`` / ``track_range_falloff`` mirror ``physics.track_efficiency``
    config; the engine passes them through so dwell-to-kill scoring matches the sim.
    """

    name = "auction"

    def __init__(
        self,
        seed: int = 1337,
        *,
        track_base: Optional[float] = None,
        track_range_falloff: Optional[float] = None,
    ) -> None:
        self._seed = int(seed)
        # Mirror physics.track_efficiency from config when not explicitly given, so the
        # solver's dwell-to-kill feasibility scoring matches what the engine actually
        # integrates. A hardcoded steeper falloff made the solver believe distant
        # targets were unkillable and refuse to assign them until the swarm closed in —
        # the laser only engaged at short range despite its true reach. (Same
        # config-defaulting pattern as cp_sat / greedy_nearest / metaheuristic.)
        if track_base is None or track_range_falloff is None:
            from beam.config import load_config

            te = load_config().physics.track_efficiency
            if track_base is None:
                track_base = te.base
            if track_range_falloff is None:
                track_range_falloff = te.range_falloff
        self._track_base = float(track_base)
        self._track_range_falloff = float(track_range_falloff)

    # ------------------------------------------------------------------ #
    # Solver Protocol                                                    #
    # ------------------------------------------------------------------ #

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Return a turret->ordered-targets assignment (pdd.md 9.1, 9.2.4)."""
        t_start = time.perf_counter()
        deadline_s = max(0.0, deadline_ms) / 1000.0

        turrets = list(state.turrets)
        # Only live/detected, still-killable targets are candidates.
        drones = [d for d in state.drones if d.state in ("alive", "engaged")]

        if not turrets or not drones:
            return Assignment(turret_orders={}, objective_estimate=0.0)

        # Per-(turret, target) primitives, all from engine.physics (no constants here).
        dwell = self._dwell_matrix(turrets, drones, state.weather_alpha)
        slew0 = self._slew_from_current(turrets, drones)  # slew from current aim
        tti = self._tti_vector(drones, state.asset_pos)

        n_i, n_j = len(turrets), len(drones)

        # Benefit matrix for the linear assignment: value if a *first* kill on j by i
        # beats its TTI, else 0 (infeasible / un-profitable).
        benefit = np.zeros((n_i, n_j), dtype=float)
        for i in range(n_i):
            for j in range(n_j):
                completion = slew0[i, j] + dwell[i, j]
                if np.isfinite(completion) and completion <= tti[j] + _EPS:
                    benefit[i, j] = drones[j].value

        # Hysteresis: bias each turret toward the target it is already engaging so the
        # value-maximizing matching does not swap primaries every epoch and re-slew off
        # a mid-dwell kill (pdd.md 6 "commit through to kill or abort"). Only where the
        # ongoing engagement is still feasible (benefit > 0).
        id_to_j = {d.id: j for j, d in enumerate(drones)}
        for i, tur in enumerate(turrets):
            cur = tur.current_target
            if cur is not None:
                j = id_to_j.get(cur, -1)
                if j >= 0 and benefit[i, j] > 0.0:
                    benefit[i, j] *= _STICKINESS_BONUS

        # Max-weight linear assignment (one target per turret) via auction.
        rng = self._rng_for(state)
        assigned_col = self._auction_assign(benefit, rng, t_start, deadline_s)

        # Build per-turret ordered lists, then greedily extend by earliest deadline.
        orders, objective = self._build_orders(
            turrets, drones, dwell, slew0, tti, benefit, assigned_col
        )

        return Assignment(turret_orders=orders, objective_estimate=float(objective))

    # ------------------------------------------------------------------ #
    # Matrix construction (delegates physics to engine.physics)          #
    # ------------------------------------------------------------------ #

    def _dwell_matrix(
        self, turrets: list[Turret], drones: list[Drone], weather_alpha: float
    ) -> np.ndarray:
        """``d_ij`` dwell-to-kill (s); ``inf`` where out of range / no reach (pdd 8.3)."""
        n_i, n_j = len(turrets), len(drones)
        out = np.full((n_i, n_j), np.inf, dtype=float)
        for i, tur in enumerate(turrets):
            for j, dr in enumerate(drones):
                rng_m = vdist(tur.pos, dr.pos)
                if rng_m > tur.range_max + _EPS:
                    continue  # out of effective range -> unkillable (x_ij=0, pdd 7.3)
                p_del = physics.delivered_power(tur.power, weather_alpha, rng_m)
                eta = physics.track_efficiency(
                    1.0,
                    rng_m,
                    base=self._track_base,
                    range_falloff=self._track_range_falloff,
                )
                dep = physics.deposition_rate(p_del, eta)
                # Dwell on the energy STILL needed, not full hardness: a target already
                # part-killed (energy_absorbed > 0) needs only its remainder, so an
                # in-progress engagement stays feasible/cheap and the auction keeps it
                # instead of thrashing to a fresh target (pdd.md 8.3/8.6). Physically
                # correct — only the remaining energy must be delivered.
                remaining = max(0.0, dr.hardness - dr.energy_absorbed)
                out[i, j] = float(physics.dwell_to_kill(remaining, dep))
        return out

    def _slew_from_current(
        self, turrets: list[Turret], drones: list[Drone]
    ) -> np.ndarray:
        """Setup time to slew each turret from its *current* aim onto each target."""
        n_i, n_j = len(turrets), len(drones)
        out = np.zeros((n_i, n_j), dtype=float)
        for i, tur in enumerate(turrets):
            for j, dr in enumerate(drones):
                aim = kinematics.aim_at(tur, dr.pos)
                out[i, j] = kinematics.slew_time(tur, aim)
        return out

    def _slew_between(self, turret: Turret, a: Drone, b: Drone) -> float:
        """Sequence-dependent slew between two targets (pdd.md 8.4)."""
        aim_a = kinematics.aim_at(turret, a.pos)
        aim_b = kinematics.aim_at(turret, b.pos)
        # s = angular_distance(a,b)/slew_rate + settle_time (pdd.md 8.4).
        return angular_distance(aim_a, aim_b) / turret.slew_rate + turret.settle_time

    def _tti_vector(self, drones: list[Drone], asset_pos) -> np.ndarray:
        """Time-to-impact (hard deadline) per target (pdd.md 8 glossary / 7.1)."""
        return np.array(
            [kinematics.time_to_impact(d, asset_pos) for d in drones], dtype=float
        )

    # ------------------------------------------------------------------ #
    # Bertsekas auction (max-weight assignment, one target per turret)   #
    # ------------------------------------------------------------------ #

    def _auction_assign(
        self,
        benefit: np.ndarray,
        rng: np.random.Generator,
        t_start: float,
        deadline_s: float,
    ) -> list[int]:
        """Max-weight one-to-one assignment via the auction algorithm.

        Returns a list ``assigned_col[i]`` giving the target index assigned to turret
        ``i`` (or ``-1`` if unassigned). Only strictly-positive benefits are ever
        assigned, so an infeasible / zero-value turret is left unassigned.

        Deterministic given identical inputs; respects the wall-clock deadline by
        returning the best feasible matching found so far (greedy fallback for any
        still-unassigned turrets).
        """
        n_i, n_j = benefit.shape

        # Tiny deterministic perturbation breaks ties without changing the optimum on
        # the integer-valued (value-scaled) benefits we use. Drawn from the seeded RNG.
        eps_scale = 1e-6 * (1.0 + float(np.max(benefit)) if benefit.size else 1.0)
        jitter = rng.random((n_i, n_j)) * eps_scale
        adj = np.where(benefit > 0.0, benefit + jitter, -np.inf)

        prices = np.zeros(n_j, dtype=float)
        owner = np.full(n_j, -1, dtype=int)  # which turret owns each target
        assigned_col = np.full(n_i, -1, dtype=int)

        # Auction bidding increment; scale to the problem so it terminates quickly.
        max_b = float(np.max(benefit)) if benefit.size else 0.0
        mu = max(1e-9, max_b / (4.0 * max(1, min(n_i, n_j))))

        unassigned = [i for i in range(n_i) if np.any(adj[i] > -np.inf)]
        # Bound the number of rounds; the deadline check is the real guard.
        max_rounds = (n_i + 1) * (n_j + 1) + 16

        rounds = 0
        while unassigned and rounds < max_rounds:
            rounds += 1
            if deadline_s > 0.0 and (time.perf_counter() - t_start) >= deadline_s:
                break  # out of time -> finish with a greedy completion below

            i = unassigned.pop(0)
            net = adj[i] - prices  # value of each target to turret i net of price
            # Best and second-best feasible targets.
            if not np.any(np.isfinite(net)):
                continue  # no feasible target for this turret
            best_j = int(np.argmax(net))
            best_val = net[best_j]
            net2 = net.copy()
            net2[best_j] = -np.inf
            second_val = np.max(net2) if np.any(np.isfinite(net2)) else -np.inf

            # Bid raises the price of best_j by the value margin + mu.
            margin = best_val - (second_val if np.isfinite(second_val) else best_val)
            prices[best_j] += margin + mu

            prev = owner[best_j]
            if prev != -1:
                assigned_col[prev] = -1
                unassigned.append(prev)  # displaced turret re-bids
            owner[best_j] = i
            assigned_col[i] = best_j

        # Greedy completion for any turret still unassigned (deadline hit or no bid).
        self._greedy_complete(adj, owner, assigned_col)
        return assigned_col.tolist()

    @staticmethod
    def _greedy_complete(
        adj: np.ndarray, owner: np.ndarray, assigned_col: np.ndarray
    ) -> None:
        """Fill remaining turrets greedily with their best free positive target."""
        n_i, n_j = adj.shape
        free_cols = {j for j in range(n_j) if owner[j] == -1}
        for i in range(n_i):
            if assigned_col[i] != -1:
                continue
            best_j, best_v = -1, -np.inf
            for j in free_cols:
                if adj[i, j] > best_v:
                    best_v, best_j = adj[i, j], j
            if best_j != -1 and np.isfinite(best_v):
                assigned_col[i] = best_j
                owner[best_j] = i
                free_cols.discard(best_j)

    # ------------------------------------------------------------------ #
    # Per-turret ordering + objective (pdd.md 7.2 single-machine)         #
    # ------------------------------------------------------------------ #

    def _build_orders(
        self,
        turrets: list[Turret],
        drones: list[Drone],
        dwell: np.ndarray,
        slew0: np.ndarray,
        tti: np.ndarray,
        benefit: np.ndarray,
        assigned_col: list[int],
    ) -> tuple[dict[str, list[str]], float]:
        """Order each turret by deadline; greedily extend with feasible free targets.

        Returns ``(turret_orders, objective)`` where the objective is the total value
        of on-time targets (pdd.md 7.2/7.3).
        """
        n_i, n_j = len(turrets), len(drones)
        taken = [False] * n_j  # at-most-one-turret-per-target (pdd.md 7.3)
        orders: dict[str, list[str]] = {}
        objective = 0.0

        # Reserve every turret's auction-assigned primary up front. Without this, an
        # earlier-indexed turret's greedy "extras" scan (which only excludes already-
        # taken targets) could grab a later turret's primary, which that later turret
        # then re-queues unconditionally — double-claiming one target. The steep legacy
        # track falloff hid this by making few extras feasible; at realistic laser reach
        # many extras are feasible and the collision surfaces (pdd.md 7.3: a target is
        # assigned to at most one turret).
        for i in range(n_i):
            primary = assigned_col[i]
            if primary is not None and primary >= 0 and benefit[i, primary] > 0.0:
                taken[primary] = True

        # Iterate turrets in fixed order for determinism.
        for i in range(n_i):
            primary = assigned_col[i]
            queue: list[int] = []
            if primary is not None and primary >= 0 and benefit[i, primary] > 0.0:
                queue.append(primary)
                taken[primary] = True

            # Candidate extra targets: free, in-range (finite dwell), positive value,
            # sorted by earliest deadline then by dwell (tightest, cheapest first).
            extras = [
                j
                for j in range(n_j)
                if not taken[j] and np.isfinite(dwell[i, j]) and drones[j].value > 0.0
            ]
            extras.sort(key=lambda j: (tti[j], dwell[i, j], j))

            # Single-machine schedule starting from the turret's current aim. The
            # completion time of the k-th queued target accumulates slew + dwell
            # (pdd.md 7.2). We seed the schedule from the primary if present.
            ordered: list[int] = []
            completion = 0.0
            last_j: Optional[int] = None
            local_value = 0.0

            # First place the primary (already feasible as a first kill), then walk
            # extras by deadline, appending any that remain on-time.
            walk = (queue + extras) if queue else extras
            for j in walk:
                if last_j is None:
                    setup = slew0[i, j]
                else:
                    setup = self._slew_between(turrets[i], drones[last_j], drones[j])
                cand_completion = completion + setup + dwell[i, j]
                if np.isfinite(cand_completion) and cand_completion <= tti[j] + _EPS:
                    ordered.append(j)
                    completion = cand_completion
                    last_j = j
                    taken[j] = True
                    local_value += drones[j].value
                else:
                    # Not on-time in this sequence: drop it and free it (it was only
                    # tentatively reserved). Keep scanning later (looser-deadline)
                    # extras, which may still fit.
                    taken[j] = False

            if ordered:
                orders[turrets[i].id] = [drones[j].id for j in ordered]
                objective += local_value

        return orders, objective

    # ------------------------------------------------------------------ #
    # Deterministic RNG (contract: derived from a construction seed)      #
    # ------------------------------------------------------------------ #

    def _rng_for(self, state: WorldState) -> np.random.Generator:
        """Per-snapshot generator seeded from the solver seed + a state digest.

        Same ``WorldState`` -> same seed -> same tie-breaks (contract determinism). The
        digest is order-stable: it folds in turret/drone ids and quantized geometry.
        """
        acc = 1469598103934665603  # FNV-1a 64-bit offset basis
        prime = 1099511628211
        mask = 0xFFFFFFFFFFFFFFFF

        def _fold_int(x: int) -> None:
            nonlocal acc
            acc = ((acc ^ (x & mask)) * prime) & mask

        def _fold_str(s: str) -> None:
            # PYTHONHASHSEED-independent: fold the raw bytes (stable across processes).
            for b in s.encode("utf-8"):
                _fold_int(b)

        _fold_int(self._seed)
        _fold_int(int(round(state.t * 1000.0)))
        for tur in state.turrets:
            _fold_str(tur.id)
            _fold_int(int(round(tur.aim * 1e4)) & mask)
        for dr in state.drones:
            _fold_str(dr.id)
            _fold_int(int(round(dr.pos.x)) & mask)
            _fold_int(int(round(dr.pos.y)) & mask)
        return make_rng(acc & 0x7FFFFFFF)
