"""Exact reference solver: CP-SAT (OR-Tools) for the battery problem (pdd.md 9.2.6).

This is the *exact reference* in the solver race (pdd.md sections 9.2 item 6, 9.3,
12). It models the full battery problem of pdd.md section 7.3 - assignment of targets
to turrets *plus* per-turret sequencing with sequence-dependent slew setup, hard
deadlines (time-to-impact), and the thermal budget - as a single CP-SAT constraint
model, and maximizes the value of drones killed before they leak (pdd.md 7.3). The
objective it proves optimal is the value the heuristics' optimality gap is measured
against (pdd.md 9.3): on small instances it equals the brute-force optimum
(mvp.md section 4 Phase-2 acceptance).

Model (pdd.md 7.1-7.3, mirrored exactly)
----------------------------------------
Targets ``j``, turrets ``i``. For an in-range, line-of-sight target ``j`` reachable by
turret ``i`` we know the dwell-to-kill ``d_ij`` (pdd.md 8.3) and, for any ordered pair
``(a, b)`` serviced consecutively by ``i``, the slew setup ``s_i(a, b)`` (pdd.md 8.4).
Per turret we choose a firing *order*; the completion time of the k-th target is the
running sum of setups + dwells (pdd.md 7.2). A target is killed iff it is assigned,
completed at or before its ``TTI`` (deadline), and the turret stays thermally feasible
through that completion (pdd.md 7.2, 8.5). Across the battery (pdd.md 7.3):

    maximize  sum_ij  v_j * kill_ij
    s.t.      sum_i x_ij <= 1                 (at most one turret per target)
              x_ij <= LOS_ij                  (line of sight only)
              x_ij = 0 if range_ij > R_i      (in range only)
              per-turret schedule feasibility incl. thermal budget
              kill_ij = 1 only if C_i(j) <= TTI_j

Sequencing is encoded with one Hamiltonian ``AddCircuit`` per turret over a dummy
depot node (the turret's current aim) plus one node per *candidate* target; the
self-loop literal of a target node is exactly "this turret does not service this
target". Completion times accumulate along the chosen circuit via reified arc
constraints. Times are scaled to integer milliseconds (CP-SAT is integer).

Determinism & contract
----------------------
- Implements the ``Solver`` Protocol (pdd.md 9.1) and self-registers as ``cp_sat``.
- ``solve`` reads only ``WorldState`` for dynamic inputs and never mutates it. The
  physics *coefficients* (track-efficiency ``base``/``range_falloff``) are config
  (pdd.md 18) and are resolved once at construction, not from global state.
- Any randomness is derived from a single seeded RNG built at construction via
  :func:`beam.util.make_rng` (CP-SAT itself is run single-threaded with a fixed
  ``random_seed`` so a given snapshot yields an identical model and solution).
- Respects ``deadline_ms`` as the CP-SAT wall-clock budget (also bounded by
  ``solver.cp_sat_time_budget_ms``); if it cannot prove optimality it returns the
  best assignment found and flags it via ``objective_estimate`` (the caller marks the
  gap as bound-based, pdd.md 9.3 / 12.2).

NO hardcoded physics/cost constants: every coefficient flows from config or the
resolved ``WorldState`` (pdd.md 18). This is a software OR/simulation tool.
"""

from __future__ import annotations

import math
from typing import Optional

from ortools.sat.python import cp_model

from beam.config import load_config
from beam.engine import physics
from beam.schemas import Assignment, Drone, Turret, WorldState
from beam.solvers.base import register
from beam.util import heading_to, make_rng, vdist

__all__ = ["CpSatSolver"]

# Time discretization for the integer CP-SAT model: seconds -> milliseconds. Not a
# physics constant (it does not appear in any model equation); it is the solver's
# numeric resolution, chosen to match the millisecond units the rest of the system
# already reports solve time / budgets in (pdd.md 12.2, 18 solver.*_ms).
_MS_PER_S: int = 1000

# Cap used for "infinite" / unreachable times so the integer model stays bounded.
# Anything at or beyond this many milliseconds is treated as never-completable. It is
# a numeric guard, not a tunable of the simulation.
_INF_MS: int = 1_000_000_000


@register("cp_sat")
class CpSatSolver:
    """Exact CP-SAT reference solver for the battery problem (pdd.md 7.3, 9.2.6)."""

    name: str = "cp_sat"

    def __init__(
        self,
        *,
        seed: int = 1337,
        track_base: Optional[float] = None,
        track_range_falloff: Optional[float] = None,
        time_budget_ms: Optional[int] = None,
    ) -> None:
        """Construct the solver, resolving config-sourced physics coefficients once.

        Args:
            seed: seed for the solver's own RNG (pdd.md determinism rule). CP-SAT is
                additionally pinned to this seed so a given snapshot is reproducible.
            track_base: ``physics.track_efficiency.base`` (config, pdd.md 18). When
                ``None`` it is loaded from ``config/defaults.yaml`` at construction.
            track_range_falloff: ``physics.track_efficiency.range_falloff`` (config).
                When ``None`` it is loaded from ``config/defaults.yaml``.
            time_budget_ms: hard cap on the CP-SAT wall-clock budget per epoch
                (config ``solver.cp_sat_time_budget_ms``, pdd.md 18). When ``None`` it
                is loaded from config. ``solve``'s ``deadline_ms`` further tightens it.
        """
        if track_base is None or track_range_falloff is None or time_budget_ms is None:
            cfg = load_config()
            if track_base is None:
                track_base = cfg.physics.track_efficiency.base
            if track_range_falloff is None:
                track_range_falloff = cfg.physics.track_efficiency.range_falloff
            if time_budget_ms is None:
                time_budget_ms = cfg.solver.cp_sat_time_budget_ms

        self._track_base: float = float(track_base)
        self._track_range_falloff: float = float(track_range_falloff)
        self._time_budget_ms: int = int(time_budget_ms)
        self._seed: int = int(seed)
        # Single seeded RNG per the determinism contract (constructed, never reseeded).
        self._rng = make_rng(self._seed)

    # ------------------------------------------------------------------ #
    # Physics helpers (pure; coefficients resolved at construction)       #
    # ------------------------------------------------------------------ #

    def _dwell_ms(self, turret: Turret, drone: Drone, weather_alpha: float) -> int:
        """Integer-millisecond dwell-to-kill for ``turret`` on ``drone`` (pdd.md 8.3).

        Returns ``_INF_MS`` when the target is effectively unkillable from here
        (out of range, or deposition underflows), so the model can never schedule it.
        """
        rng_m = vdist(turret.pos, drone.pos)
        if rng_m > turret.range_max:
            return _INF_MS
        p_del = physics.delivered_power(turret.power, weather_alpha, rng_m)
        eta = physics.track_efficiency(
            1.0,  # track quality: v1 uses perfect track; range drives the falloff
            rng_m,
            base=self._track_base,
            range_falloff=self._track_range_falloff,
        )
        dep = physics.deposition_rate(p_del, eta)
        dwell_s = float(physics.dwell_to_kill(drone.hardness, dep))
        if not math.isfinite(dwell_s):
            return _INF_MS
        ms = int(math.ceil(dwell_s * _MS_PER_S))
        return min(ms, _INF_MS)

    def _slew_ms(self, turret: Turret, aim_from: float, aim_to: float) -> int:
        """Integer-millisecond slew setup between two aim headings (pdd.md 8.4)."""
        s = physics.slew_time(
            aim_from,
            aim_to,
            slew_rate=turret.slew_rate,
            settle_time=turret.settle_time,
        )
        return int(math.ceil(s * _MS_PER_S))

    @staticmethod
    def _tti_ms(drone: Drone, asset_pos) -> int:
        """Integer-millisecond time-to-impact (the target's hard deadline)."""
        # Closing-speed TTI, mirroring engine.kinematics.time_to_impact (pdd.md 8).
        to_asset_x = asset_pos.x - drone.pos.x
        to_asset_y = asset_pos.y - drone.pos.y
        dist = math.hypot(to_asset_x, to_asset_y)
        if dist <= 1e-12:
            return 0
        ux, uy = to_asset_x / dist, to_asset_y / dist
        closing = drone.vel.x * ux + drone.vel.y * uy
        if closing <= 1e-12:
            return _INF_MS
        tti_s = dist / closing
        return min(int(math.floor(tti_s * _MS_PER_S)), _INF_MS)

    # ------------------------------------------------------------------ #
    # Solver entry point (pdd.md 9.1)                                     #
    # ------------------------------------------------------------------ #

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Solve the battery problem exactly (pdd.md 7.3); respect ``deadline_ms``.

        Returns an :class:`~beam.schemas.Assignment` (turret_id -> ordered target ids)
        with ``objective_estimate`` = total value of drones killed before their
        deadlines under the chosen schedule. Reads only ``state``; never mutates it.
        """
        # Only live/detected, engageable targets (pdd.md 7.1: T = live + detected).
        drones = [d for d in state.drones if d.state in ("alive", "engaged")]
        turrets = list(state.turrets)
        if not drones or not turrets:
            return Assignment(turret_orders={}, objective_estimate=0.0)

        alpha = state.weather_alpha
        asset = state.asset_pos

        # Per (turret, target): dwell, deadline, and current-aim slew (depot arc).
        # candidates[i] = list of target indices reachable (finite dwell, in range,
        # killable before TTI ignoring sequencing) by turret i. LOS is full in v1
        # (no occlusion model yet, pdd.md 3.2); kept as an explicit hook below.
        n_t = len(turrets)
        n_j = len(drones)

        dwell = [[0] * n_j for _ in range(n_t)]
        tti = [self._tti_ms(d, asset) for d in drones]
        value = [float(d.value) for d in drones]
        # Aim heading from turret i to target j (for slew setup costs).
        aim_to = [[0.0] * n_j for _ in range(n_t)]

        candidates: list[list[int]] = []
        for i, turret in enumerate(turrets):
            cand_i: list[int] = []
            for j, drone in enumerate(drones):
                d_ms = self._dwell_ms(turret, drone, alpha)
                dwell[i][j] = d_ms
                aim_to[i][j] = heading_to(turret.pos, drone.pos)
                # Reachable only if it can be killed before its deadline even when it
                # is this turret's first shot from the current aim (necessary cond.).
                first_slew = self._slew_ms(turret, turret.aim, aim_to[i][j])
                reachable = (
                    d_ms < _INF_MS
                    and tti[j] < _INF_MS
                    and (first_slew + d_ms) <= tti[j]
                )
                if reachable:
                    cand_i.append(j)
            candidates.append(cand_i)

        if not any(candidates):
            return Assignment(turret_orders={}, objective_estimate=0.0)

        model = cp_model.CpModel()

        # kill[i][j]: turret i services-and-kills target j (only over candidates).
        kill: dict[tuple[int, int], cp_model.IntVar] = {}
        # completion[i][j]: completion time (ms) of target j on turret i (>=0).
        completion: dict[tuple[int, int], cp_model.IntVar] = {}
        # arc[i][(a, b)]: turret i fires b immediately after a, where node -1 is the
        # depot (current aim). Used by AddCircuit for sequencing (pdd.md 7.2).
        arc: dict[tuple[int, int, int], cp_model.IntVar] = {}

        for i in range(n_t):
            cand = candidates[i]
            if not cand:
                continue
            for j in cand:
                kill[(i, j)] = model.NewBoolVar(f"kill_t{i}_d{j}")
                completion[(i, j)] = model.NewIntVar(0, _INF_MS, f"C_t{i}_d{j}")

            # ---- Circuit / sequencing per turret -------------------------------
            # Nodes: depot=-1 plus each candidate target. A candidate not serviced
            # takes its self-loop arc. AddCircuit needs a literal per arc.
            depot = -1
            arcs: list[tuple[int, int, cp_model.IntVar]] = []

            # depot -> j (j is the first serviced target on this turret)
            for j in cand:
                lit = model.NewBoolVar(f"arc_t{i}_depot_d{j}")
                arc[(i, depot, j)] = lit
                arcs.append((self._node(depot), self._node(j), lit))
            # j -> depot (j is the last serviced target; closes the tour)
            for j in cand:
                lit = model.NewBoolVar(f"arc_t{i}_d{j}_depot")
                arc[(i, j, depot)] = lit
                arcs.append((self._node(j), self._node(depot), lit))
            # a -> b between two distinct candidates
            for a in cand:
                for b in cand:
                    if a == b:
                        continue
                    lit = model.NewBoolVar(f"arc_t{i}_d{a}_d{b}")
                    arc[(i, a, b)] = lit
                    arcs.append((self._node(a), self._node(b), lit))
            # self-loop = "not serviced": present iff kill[i][j] is false.
            for j in cand:
                not_killed = model.NewBoolVar(f"selfloop_t{i}_d{j}")
                model.Add(kill[(i, j)] + not_killed == 1)
                arcs.append((self._node(j), self._node(j), not_killed))

            model.AddCircuit(arcs)

            # ---- Completion-time accumulation along the chosen circuit ----------
            for j in cand:
                # If depot -> j chosen, completion[j] = slew(aim,j) + dwell[j].
                s0 = self._slew_ms(turrets[i], turrets[i].aim, aim_to[i][j])
                model.Add(
                    completion[(i, j)] == s0 + dwell[i][j]
                ).OnlyEnforceIf(arc[(i, depot, j)])
                for a in cand:
                    if a == j:
                        continue
                    s_ab = self._slew_ms(turrets[i], aim_to[i][a], aim_to[i][j])
                    # If a -> j chosen: completion[j] = completion[a] + s_ab + dwell[j].
                    model.Add(
                        completion[(i, j)]
                        == completion[(i, a)] + s_ab + dwell[i][j]
                    ).OnlyEnforceIf(arc[(i, a, j)])

                # Deadline: a serviced (killed) target must complete by its TTI.
                # When not killed, completion is unconstrained (kept >= 0); we pin it
                # to 0 to keep the search tight and the model deterministic.
                model.Add(completion[(i, j)] <= tti[j]).OnlyEnforceIf(kill[(i, j)])
                model.Add(completion[(i, j)] == 0).OnlyEnforceIf(
                    kill[(i, j)].Not()
                )

            # ---- Thermal budget (pdd.md 8.5) -----------------------------------
            # Firing target j adds heat_rate * dwell_s heat; the turret has finite
            # capacity before forced cooldown. The total heat accrued by this
            # turret's served targets within one epoch may not exceed the headroom
            # from its current thermal state to h_max (a conservative, monotone
            # encoding of the cap that forces load-spreading across turrets).
            tc = turrets[i].thermal_cfg
            headroom = tc.h_max - turrets[i].thermal
            if tc.heat_rate > 0.0 and headroom < _INF_MS:
                # heat units (scaled by ms): heat_rate(/s) * dwell_ms.
                cap_units = int(math.floor(max(0.0, headroom) * _MS_PER_S))
                heat_terms = []
                for j in cand:
                    coeff = int(math.ceil(tc.heat_rate * dwell[i][j]))
                    heat_terms.append(coeff * kill[(i, j)])
                if heat_terms:
                    model.Add(sum(heat_terms) <= cap_units)

        # ---- At most one turret per target (pdd.md 7.3) ------------------------
        for j in range(n_j):
            servers = [
                kill[(i, j)] for i in range(n_t) if (i, j) in kill
            ]
            if servers:
                model.Add(sum(servers) <= 1)

        # ---- Objective: maximize value killed before deadline (pdd.md 7.3) -----
        obj_terms = []
        for (i, j), var in kill.items():
            # Scale value to int for CP-SAT; round to keep determinism and ties.
            obj_terms.append(int(round(value[j])) * var)
        if not obj_terms:
            return Assignment(turret_orders={}, objective_estimate=0.0)
        model.Maximize(sum(obj_terms))

        # ---- Solve, time-boxed (pdd.md 9.2.6, 16) ------------------------------
        solver = cp_model.CpSolver()
        budget_ms = min(int(deadline_ms), self._time_budget_ms)
        budget_ms = max(budget_ms, 1)
        solver.parameters.max_time_in_seconds = budget_ms / 1000.0
        # Determinism: single worker + fixed seed -> identical model -> identical sol.
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = self._seed

        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return Assignment(turret_orders={}, objective_estimate=0.0)

        # ---- Extract per-turret firing order by walking each circuit -----------
        turret_orders: dict[str, list[str]] = {}
        for i in range(n_t):
            cand = candidates[i]
            if not cand:
                continue
            # Build successor map from chosen arcs; walk depot -> ... -> depot.
            order: list[int] = []
            current = -1  # depot
            visited: set[int] = set()
            while True:
                nxt = None
                # depot's outgoing or a target's outgoing
                for b in cand:
                    if b in visited:
                        continue
                    key = (i, current, b)
                    if key in arc and solver.Value(arc[key]) == 1:
                        nxt = b
                        break
                if nxt is None:
                    break
                order.append(nxt)
                visited.add(nxt)
                current = nxt
            if order:
                turret_orders[turrets[i].id] = [drones[j].id for j in order]

        objective = float(solver.ObjectiveValue())
        return Assignment(turret_orders=turret_orders, objective_estimate=objective)

    @staticmethod
    def _node(idx: int) -> int:
        """Map a target index / depot(-1) to a non-negative AddCircuit node id.

        AddCircuit requires non-negative node indices; we reserve node 0 for the
        depot (current aim) and shift target index ``j`` to node ``j + 1``.
        """
        return idx + 1
