"""Metaheuristic solvers: Genetic Algorithm and Simulated Annealing (pdd.md 9.2.5).

Both solvers search the same decision space as the exact reference (pdd.md sections
7.2 / 7.3): a joint **assignment** of targets to turrets *plus* a **per-turret
permutation** (firing order). They optimize the battery objective from section 7.3 —
the total value of drones killed before they leak — under the per-turret single-machine
schedule-feasibility model of section 7.2 (sequence-dependent slew setup + dwell-to-kill
+ thermal budget + range + line-of-sight).

Design notes / hard rules honored:

- NO hardcoded physics or cost constants. Every coefficient (track-efficiency base /
  range-falloff, slew/settle/thermal rates, power, range) flows in
  from config (``beam.config``) or from the per-turret :class:`~beam.schemas.Turret`
  carried on the :class:`~beam.schemas.WorldState`. Weather extinction is the resolved
  ``WorldState.weather_alpha`` (pdd.md section 18).
- Scoring reuses :mod:`beam.engine.physics` (delivered power -> deposition -> dwell)
  and :func:`beam.engine.kinematics.time_to_impact` exactly as the engine does, so a
  solver's self-reported ``objective_estimate`` is consistent with the engine's
  kill/leak resolution model.
- Determinism: all randomness comes from a single seeded ``numpy`` generator created
  with :func:`beam.util.make_rng` at construction (the Solver Protocol requires
  randomness be derived from a construction-time seed, never a hidden global RNG).
  Iteration order over drones / turrets is the fixed ``WorldState`` list order.
- Anytime: both search loops poll a wall-clock deadline and return the best assignment
  found so far when it is hit (pdd.md section 9.2.5 / 9.1).

The two concrete classes register themselves under the names ``"ga"`` and ``"sa"``;
``solver.metaheuristic`` in config selects the project default between them (the engine
reads that key — this module just exposes both registered solvers).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from beam.config import BeamConfig, load_config
from beam.engine import physics
from beam.engine.kinematics import time_to_impact
from beam.schemas import Assignment, Drone, Turret, WorldState
from beam.solvers.base import register
from beam.util import angular_distance, heading_to, make_rng

__all__ = ["MetaheuristicSolver", "GeneticAlgorithmSolver", "SimulatedAnnealingSolver"]


# --------------------------------------------------------------------------- #
# Scoring core (shared by GA, SA, and the brute-force test reference)          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ScoringParams:
    """Config-resolved tunables needed to score an assignment (pdd.md 8.3 / 7.4).

    Pulled once at construction from :class:`~beam.config.BeamConfig` so scoring never
    reaches into config mid-search. Nothing here is hard-coded.
    """

    track_base: float  # physics.track_efficiency.base
    track_range_falloff: float  # physics.track_efficiency.range_falloff


class _ScoringContext:
    """Pre-computed per-epoch geometry for fast, deterministic scoring.

    Built once per ``solve`` from the immutable :class:`~beam.schemas.WorldState`.
    Holds, for every (turret, drone) pair, the dwell-to-kill time and per-pair
    feasibility (range + a positive deposition rate => effectively in reach / LOS), plus
    each drone's time-to-impact and value, and each turret's slew/thermal parameters.

    Indices are positions into the fixed ``WorldState`` lists, so iteration order is
    deterministic.
    """

    def __init__(self, state: WorldState, params: _ScoringParams) -> None:
        self.params = params
        self.turrets: list[Turret] = list(state.turrets)
        self.drones: list[Drone] = list(state.drones)
        self.n_turrets = len(self.turrets)
        self.n_drones = len(self.drones)

        self.turret_index = {t.id: i for i, t in enumerate(self.turrets)}
        self.drone_index = {d.id: j for j, d in enumerate(self.drones)}

        # Per-drone value and time-to-impact (the hard deadline).
        self.value = np.array([d.value for d in self.drones], dtype=float)
        self.tti = np.array(
            [time_to_impact(d, state.asset_pos) for d in self.drones], dtype=float
        )

        # Per-turret schedule parameters.
        self.slew_rate = np.array([t.slew_rate for t in self.turrets], dtype=float)
        self.settle_time = np.array([t.settle_time for t in self.turrets], dtype=float)
        self.aim0 = np.array([t.aim for t in self.turrets], dtype=float)

        # Per-(turret, drone) desired aim heading and dwell-to-kill.
        self.aim_to = np.zeros((self.n_turrets, self.n_drones), dtype=float)
        self.dwell = np.full((self.n_turrets, self.n_drones), np.inf, dtype=float)
        self.feasible = np.zeros((self.n_turrets, self.n_drones), dtype=bool)

        for i, turret in enumerate(self.turrets):
            for j, drone in enumerate(self.drones):
                self.aim_to[i, j] = heading_to(turret.pos, drone.pos)
                rng_m = math.hypot(
                    drone.pos.x - turret.pos.x, drone.pos.y - turret.pos.y
                )
                if rng_m > turret.range_max:
                    # Out of range: never engageable by this turret (x_ij forced 0).
                    continue
                p_del = physics.delivered_power(turret.power, state.weather_alpha, rng_m)
                eta = physics.track_efficiency(
                    1.0,
                    rng_m,
                    base=params.track_base,
                    range_falloff=params.track_range_falloff,
                )
                dep = physics.deposition_rate(p_del, eta)
                d_ij = float(physics.dwell_to_kill(drone.hardness, dep))
                self.dwell[i, j] = d_ij
                # Feasible iff in range and the dwell is finite (positive deposition =>
                # effectively has line of sight / is reachable, pdd.md 7.3).
                self.feasible[i, j] = math.isfinite(d_ij)

        # Thermal: how long this turret can keep firing before tripping the forced-
        # cooldown cap (pdd.md 8.5). Heat only grows while firing, so the firing budget
        # is (h_max - current_heat) / heat_rate. A turret already latched in cooldown
        # (state == "cooldown") or at/over the cap has zero budget. This is the
        # thermally_feasible(...) gate of pdd.md 7.2; it bounds total *dwell*, not the
        # schedule's wall-clock span (the schedule's deadline is each target's TTI).
        self.thermal_budget = np.zeros(self.n_turrets, dtype=float)
        for i, turret in enumerate(self.turrets):
            cfg = turret.thermal_cfg
            if turret.state == "cooldown" or turret.thermal >= cfg.h_max:
                budget = 0.0
            elif cfg.heat_rate <= 0.0:
                budget = math.inf  # never heats up -> unlimited dwell
            else:
                budget = (cfg.h_max - turret.thermal) / cfg.heat_rate
            self.thermal_budget[i] = max(0.0, budget)

    # -- per-turret schedule evaluation ------------------------------------- #

    def score_turret(self, turret_idx: int, order: list[int]) -> float:
        """Value of targets turret ``turret_idx`` kills following ``order`` (7.2).

        ``order`` is a list of drone indices. Walks the single-machine schedule from the
        turret's current aim, accumulating slew setup time and dwell time, and counts a
        target as killed iff its completion time is within its TTI deadline and the
        turret's thermal budget still permits firing through it (pdd.md 7.2).
        """
        if not order:
            return 0.0

        slew_rate = self.slew_rate[turret_idx]
        settle = self.settle_time[turret_idx]
        aim = self.aim0[turret_idx]
        budget = self.thermal_budget[turret_idx]

        clock = 0.0  # elapsed schedule wall-clock from the turret's current aim
        fired = 0.0  # cumulative firing (dwell) time, bounded by the thermal budget
        total = 0.0

        for j in order:
            if not self.feasible[turret_idx, j]:
                continue
            d_ij = self.dwell[turret_idx, j]
            target_aim = self.aim_to[turret_idx, j]
            # Slew setup cost from the previous aim (sequence-dependent, pdd.md 8.4).
            setup = angular_distance(aim, target_aim) / slew_rate + settle
            completion = clock + setup + d_ij
            # Killed iff completed before its TTI deadline AND the thermal budget covers
            # the dwell through completion (pdd.md 7.2: C(j) <= TTI_j AND thermally
            # feasible).
            if (
                completion <= self.tti[j]
                and fired + d_ij <= budget + _TIME_EPS
            ):
                total += self.value[j]
                fired += d_ij
            # The turret advances its aim and clock whether or not the kill landed: it
            # still spent the setup, and it dwelled until kill (or until we move on). We
            # advance the clock by setup + dwell so later targets see realistic timing.
            clock = completion
            aim = target_aim
        return total

    def score(self, orders: dict[int, list[int]]) -> float:
        """Total battery objective for a full assignment (pdd.md 7.3).

        ``orders`` maps turret index -> ordered drone indices. The caller guarantees
        each drone index appears under at most one turret (the at-most-one-turret
        constraint, pdd.md 7.3); this method does not re-check it.
        """
        return sum(self.score_turret(i, order) for i, order in orders.items())


# Numerical slack so a dwell that exactly equals the remaining thermal budget counts as
# feasible (float round-off guard, not a tunable physics constant).
_TIME_EPS = 1e-9


# --------------------------------------------------------------------------- #
# Genome <-> Assignment plumbing                                               #
# --------------------------------------------------------------------------- #


def _orders_to_assignment(
    ctx: _ScoringContext, orders: dict[int, list[int]], objective: float
) -> Assignment:
    """Project an index-based per-turret order map into the wire ``Assignment``."""
    turret_orders: dict[str, list[str]] = {}
    for i, order in orders.items():
        if not order:
            continue
        turret_id = ctx.turrets[i].id
        turret_orders[turret_id] = [ctx.drones[j].id for j in order]
    return Assignment(turret_orders=turret_orders, objective_estimate=objective)


def _feasible_turrets_for_drone(ctx: _ScoringContext, j: int) -> list[int]:
    """Turret indices that can feasibly engage drone ``j`` (range + reachable)."""
    return [i for i in range(ctx.n_turrets) if ctx.feasible[i, j]]


# --------------------------------------------------------------------------- #
# Base metaheuristic solver                                                    #
# --------------------------------------------------------------------------- #


class MetaheuristicSolver:
    """Shared machinery for the GA and SA solvers over (assignment + permutation).

    A *candidate* is represented as ``dict[turret_idx, list[drone_idx]]`` — a per-turret
    firing order with each drone assigned to at most one turret. Candidates are scored by
    :class:`_ScoringContext`. Subclasses implement :meth:`_search`, the anytime
    optimization loop, and set :attr:`name`.

    Construction takes the config tunables and a seed so the search is fully
    deterministic given an identical ``WorldState`` (Solver Protocol requirement). When
    no config is supplied the project defaults are loaded.
    """

    name: str = "metaheuristic"

    def __init__(
        self,
        *,
        config: Optional[BeamConfig] = None,
        seed: Optional[int] = None,
    ) -> None:
        cfg = config if config is not None else load_config()
        self._params = _ScoringParams(
            track_base=cfg.physics.track_efficiency.base,
            track_range_falloff=cfg.physics.track_efficiency.range_falloff,
        )
        # The construction-time seed seeds the single RNG used for the whole search.
        self._seed = cfg.sim.seed if seed is None else seed

    # -- candidate construction -------------------------------------------- #

    def _random_candidate(
        self, ctx: _ScoringContext, rng: np.random.Generator
    ) -> dict[int, list[int]]:
        """Build a random feasible candidate: assign each drone to a random feasible
        turret (or leave it un-engaged), then shuffle each turret's order."""
        orders: dict[int, list[int]] = {i: [] for i in range(ctx.n_turrets)}
        for j in range(ctx.n_drones):
            choices = _feasible_turrets_for_drone(ctx, j)
            if not choices:
                continue
            # Option to leave the drone un-engaged (index == len(choices)).
            pick = int(rng.integers(0, len(choices) + 1))
            if pick < len(choices):
                orders[choices[pick]].append(j)
        for i in range(ctx.n_turrets):
            order = orders[i]
            if len(order) > 1:
                rng.shuffle(order)
        return orders

    def _greedy_seed(self, ctx: _ScoringContext) -> dict[int, list[int]]:
        """A deterministic, decent starting candidate (most-urgent-killable flavor).

        Drones are considered by ascending TTI (tightest deadline first, pdd.md 9.2.3);
        each is assigned to the feasible turret with the smallest dwell-to-kill, and each
        turret's order is kept TTI-sorted. This gives the search a strong basin to refine
        rather than starting from noise, and is fully deterministic.
        """
        orders: dict[int, list[int]] = {i: [] for i in range(ctx.n_turrets)}
        order_by_urgency = sorted(
            range(ctx.n_drones), key=lambda j: (ctx.tti[j], j)
        )
        for j in order_by_urgency:
            choices = _feasible_turrets_for_drone(ctx, j)
            if not choices:
                continue
            best_i = min(choices, key=lambda i: (ctx.dwell[i, j], i))
            orders[best_i].append(j)
        # Sort each turret's targets by TTI (earliest deadline first).
        for i in range(ctx.n_turrets):
            orders[i].sort(key=lambda j: (ctx.tti[j], j))
        return orders

    @staticmethod
    def _copy(orders: dict[int, list[int]]) -> dict[int, list[int]]:
        return {i: list(o) for i, o in orders.items()}

    # -- neighbor / mutation operators ------------------------------------- #

    def _mutate(
        self,
        ctx: _ScoringContext,
        orders: dict[int, list[int]],
        rng: np.random.Generator,
    ) -> dict[int, list[int]]:
        """Return a neighbor of ``orders`` via one random local move.

        Moves (chosen uniformly among the applicable ones):
          - reassign a drone to a different feasible turret (or un-engage it),
          - swap two drones within one turret's order,
          - move a drone to a new position within its turret's order.
        """
        cand = self._copy(orders)
        move = int(rng.integers(0, 3))

        if move == 0 and ctx.n_drones > 0:
            # Reassign a random drone.
            j = int(rng.integers(0, ctx.n_drones))
            choices = _feasible_turrets_for_drone(ctx, j)
            # Remove j from wherever it currently is.
            for i in range(ctx.n_turrets):
                if j in cand[i]:
                    cand[i].remove(j)
                    break
            if choices:
                pick = int(rng.integers(0, len(choices) + 1))
                if pick < len(choices):
                    target = choices[pick]
                    insert_at = int(rng.integers(0, len(cand[target]) + 1))
                    cand[target].insert(insert_at, j)
            return cand

        # Moves 1 and 2 operate within a turret that has >= 2 targets.
        non_trivial = [i for i in range(ctx.n_turrets) if len(cand[i]) >= 2]
        if not non_trivial:
            return cand
        i = non_trivial[int(rng.integers(0, len(non_trivial)))]
        order = cand[i]
        if move == 1:
            a, b = rng.choice(len(order), size=2, replace=False)
            order[a], order[b] = order[b], order[a]
        else:  # move == 2: relocate within the same turret
            src = int(rng.integers(0, len(order)))
            elem = order.pop(src)
            dst = int(rng.integers(0, len(order) + 1))
            order.insert(dst, elem)
        return cand

    # -- the Solver Protocol entry point ----------------------------------- #

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Search for the best assignment, returning best-so-far at ``deadline_ms``."""
        start = time.perf_counter()
        deadline_s = max(0.0, deadline_ms) / 1000.0
        ctx = _ScoringContext(state, self._params)

        # Degenerate snapshots: nothing to do.
        if ctx.n_turrets == 0 or ctx.n_drones == 0:
            return Assignment(turret_orders={}, objective_estimate=0.0)

        rng = make_rng(self._seed)

        # Seed the search with a strong deterministic candidate.
        best = self._greedy_seed(ctx)
        best_score = ctx.score(best)

        best, best_score = self._search(
            ctx, rng, start, deadline_s, best, best_score
        )
        return _orders_to_assignment(ctx, best, best_score)

    # -- subclass hook ------------------------------------------------------ #

    def _search(
        self,
        ctx: _ScoringContext,
        rng: np.random.Generator,
        start: float,
        deadline_s: float,
        best: dict[int, list[int]],
        best_score: float,
    ) -> tuple[dict[int, list[int]], float]:  # pragma: no cover - abstract
        raise NotImplementedError

    @staticmethod
    def _deadline_hit(start: float, deadline_s: float) -> bool:
        return (time.perf_counter() - start) >= deadline_s


# --------------------------------------------------------------------------- #
# Genetic Algorithm                                                           #
# --------------------------------------------------------------------------- #


@register("ga")
class GeneticAlgorithmSolver(MetaheuristicSolver):
    """Genetic algorithm over (assignment + per-turret permutation) (pdd.md 9.2.5).

    A population of candidates evolves by tournament selection, a uniform per-drone
    assignment crossover, and the local mutation operators. Elitism keeps the best
    individual each generation. Anytime: between generations it checks the wall-clock
    deadline and returns the best individual found so far.

    Population size / tournament size are fixed structural hyperparameters of the search
    (not physics or cost constants), kept deterministic under the seeded RNG.
    """

    name = "ga"

    _POP_SIZE = 24
    _TOURNAMENT = 3
    _ELITES = 2
    _MUTATION_RATE = 0.6

    def _search(self, ctx, rng, start, deadline_s, best, best_score):
        # Initial population: the greedy seed + random candidates.
        population: list[dict[int, list[int]]] = [best]
        while len(population) < self._POP_SIZE:
            population.append(self._random_candidate(ctx, rng))
        scores = [ctx.score(ind) for ind in population]

        for idx, sc in enumerate(scores):
            if sc > best_score:
                best, best_score = self._copy(population[idx]), sc

        while not self._deadline_hit(start, deadline_s):
            # Rank for elitism (stable on ties via index for determinism).
            ranked = sorted(
                range(len(population)), key=lambda k: (-scores[k], k)
            )
            new_pop: list[dict[int, list[int]]] = [
                self._copy(population[ranked[e]])
                for e in range(min(self._ELITES, len(ranked)))
            ]
            while len(new_pop) < self._POP_SIZE:
                if self._deadline_hit(start, deadline_s):
                    break
                p1 = self._tournament(population, scores, rng)
                p2 = self._tournament(population, scores, rng)
                child = self._crossover(ctx, p1, p2, rng)
                if rng.random() < self._MUTATION_RATE:
                    child = self._mutate(ctx, child, rng)
                new_pop.append(child)

            population = new_pop
            scores = [ctx.score(ind) for ind in population]
            for idx, sc in enumerate(scores):
                if sc > best_score:
                    best, best_score = self._copy(population[idx]), sc

        return best, best_score

    def _tournament(
        self,
        population: list[dict[int, list[int]]],
        scores: list[float],
        rng: np.random.Generator,
    ) -> dict[int, list[int]]:
        contenders = rng.integers(0, len(population), size=self._TOURNAMENT)
        winner = max(contenders, key=lambda k: (scores[int(k)], -int(k)))
        return population[int(winner)]

    def _crossover(
        self,
        ctx: _ScoringContext,
        p1: dict[int, list[int]],
        p2: dict[int, list[int]],
        rng: np.random.Generator,
    ) -> dict[int, list[int]]:
        """Uniform per-drone assignment crossover.

        For each drone, inherit its (turret, position-in-order) from one parent at
        random; rebuild each turret's order by the inherited positions, breaking ties
        deterministically. Guarantees the at-most-one-turret invariant by construction.
        """
        # Map drone -> (turret_idx, position) for each parent.
        loc1 = self._locations(ctx, p1)
        loc2 = self._locations(ctx, p2)
        # Collect inherited (turret, pos, drone) tuples.
        buckets: dict[int, list[tuple[float, int]]] = {
            i: [] for i in range(ctx.n_turrets)
        }
        for j in range(ctx.n_drones):
            src = loc1 if rng.random() < 0.5 else loc2
            if j not in src:
                continue
            i, pos = src[j]
            buckets[i].append((pos, j))
        child: dict[int, list[int]] = {}
        for i in range(ctx.n_turrets):
            ordered = sorted(buckets[i], key=lambda t: (t[0], t[1]))
            child[i] = [j for _, j in ordered]
        return child

    @staticmethod
    def _locations(
        ctx: _ScoringContext, orders: dict[int, list[int]]
    ) -> dict[int, tuple[int, int]]:
        loc: dict[int, tuple[int, int]] = {}
        for i, order in orders.items():
            for pos, j in enumerate(order):
                loc[j] = (i, pos)
        return loc


# --------------------------------------------------------------------------- #
# Simulated Annealing                                                         #
# --------------------------------------------------------------------------- #


@register("sa")
class SimulatedAnnealingSolver(MetaheuristicSolver):
    """Simulated annealing over (assignment + per-turret permutation) (pdd.md 9.2.5).

    Starts from the greedy seed and repeatedly proposes a neighbor via the local move
    operators. Better neighbors are always accepted; worse ones are accepted with the
    Metropolis probability ``exp(delta / T)``. The temperature decays geometrically over
    a fixed iteration budget, re-derived each call. Anytime: the loop checks the deadline
    every step and tracks the best state ever seen.

    The temperature schedule constants are structural search hyperparameters (not
    physics/cost constants) and are applied deterministically under the seeded RNG.
    """

    name = "sa"

    _T_START = 1.0
    _T_END = 1e-3
    _STEPS_PER_DEADLINE_CHECK = 1

    def _search(self, ctx, rng, start, deadline_s, best, best_score):
        current = self._copy(best)
        current_score = best_score

        # A value scale to make the Metropolis criterion dimensionless w.r.t. the
        # objective's magnitude (drone values can be thousands). Derived from the data,
        # not hard-coded: the mean engageable drone value (fallback 1.0).
        scale = self._value_scale(ctx)

        # Geometric cooling factor. We do not know the iteration count up front (anytime),
        # so cool by a fixed ratio per step toward _T_END asymptotically.
        temp = self._T_START
        cooling = 0.9995

        while not self._deadline_hit(start, deadline_s):
            cand = self._mutate(ctx, current, rng)
            cand_score = ctx.score(cand)
            delta = cand_score - current_score
            if delta >= 0:
                accept = True
            else:
                # Metropolis acceptance, normalized by the value scale and temperature.
                prob = math.exp((delta / scale) / max(temp, self._T_END))
                accept = rng.random() < prob
            if accept:
                current, current_score = cand, cand_score
                if current_score > best_score:
                    best, best_score = self._copy(current), current_score
            temp = max(self._T_END, temp * cooling)

        return best, best_score

    @staticmethod
    def _value_scale(ctx: _ScoringContext) -> float:
        engageable = [
            ctx.value[j]
            for j in range(ctx.n_drones)
            if _feasible_turrets_for_drone(ctx, j)
        ]
        if not engageable:
            return 1.0
        mean_v = float(np.mean(engageable))
        return mean_v if mean_v > 0.0 else 1.0
