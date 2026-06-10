"""Tests for the metaheuristic solvers (beam.solvers.metaheuristic, pdd.md 9.2.5).

Covers the task's acceptance criteria:
  - both "ga" and "sa" register and implement the Solver Protocol;
  - solve() returns a VALID Assignment: every target is in range / line of sight of the
    turret it is assigned to, and no target is assigned to more than one turret
    (the at-most-one-turret constraint, pdd.md 7.3);
  - on a TINY instance the solver's reported objective is <= the brute-force optimum and
    the optimality gap is >= 0 (pdd.md 9.3, mvp.md section 4);
  - determinism: identical state + seed -> identical assignment.

The brute-force optimum enumerates every (assignment x per-turret permutation) and
scores it with the SAME scoring core the solvers use, so the comparison is exact.
"""

from __future__ import annotations

import itertools
import math

import pytest

from beam.config import load_config
from beam.schemas import (
    Assignment,
    Drone,
    ThermalConfig,
    Turret,
    Vec2,
    WorldState,
)
from beam.solvers import REGISTRY, available_solvers
from beam.solvers.base import Solver
from beam.solvers.metaheuristic import (
    GeneticAlgorithmSolver,
    SimulatedAnnealingSolver,
    _ScoringContext,
    _ScoringParams,
)

SOLVER_NAMES = ["ga", "sa"]


# --------------------------------------------------------------------------- #
# Fixtures / builders                                                          #
# --------------------------------------------------------------------------- #


def make_thermal() -> ThermalConfig:
    return ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0)


def make_turret(
    turret_id: str,
    pos: tuple[float, float],
    *,
    aim: float = 0.0,
    power: float = 100.0,
    slew_rate: float = 1.2,
    settle_time: float = 0.1,
    range_max: float = 5000.0,
    thermal: float = 0.0,
) -> Turret:
    return Turret(
        id=turret_id,
        pos=Vec2(x=pos[0], y=pos[1]),
        aim=aim,
        slew_rate=slew_rate,
        settle_time=settle_time,
        power=power,
        range_max=range_max,
        thermal=thermal,
        thermal_cfg=make_thermal(),
    )


def make_drone(
    drone_id: str,
    pos: tuple[float, float],
    vel: tuple[float, float],
    *,
    value: float = 2000.0,
    hardness: float = 40.0,
    class_name: str = "quad_small",
) -> Drone:
    return Drone(
        id=drone_id,
        pos=Vec2(x=pos[0], y=pos[1]),
        vel=Vec2(x=vel[0], y=vel[1]),
        value=value,
        hardness=hardness,
        class_name=class_name,
    )


def tiny_state() -> WorldState:
    """A small but non-trivial snapshot: 2 turrets, 3 close, slow drones.

    Drones are placed near the turrets and move slowly so several are killable within
    the firing horizon, but not all simultaneously -> ordering / assignment matters.
    """
    turrets = [
        make_turret("t1", (-200.0, 0.0)),
        make_turret("t2", (200.0, 0.0)),
    ]
    drones = [
        make_drone("d0", (-150.0, 300.0), (0.0, -40.0), value=2000.0, hardness=40.0),
        make_drone("d1", (150.0, 300.0), (0.0, -40.0), value=15000.0, hardness=120.0),
        make_drone("d2", (0.0, 250.0), (0.0, -30.0), value=2000.0, hardness=40.0),
    ]
    return WorldState(
        t=0.0,
        drones=drones,
        turrets=turrets,
        weather_alpha=load_config().weather("clear").alpha,
        asset_pos=Vec2(x=0.0, y=0.0),
    )


def scoring_ctx(state: WorldState) -> _ScoringContext:
    cfg = load_config()
    params = _ScoringParams(
        track_base=cfg.physics.track_efficiency.base,
        track_range_falloff=cfg.physics.track_efficiency.range_falloff,
    )
    return _ScoringContext(state, params)


# --------------------------------------------------------------------------- #
# Brute-force optimum over the SAME scoring core                              #
# --------------------------------------------------------------------------- #


def brute_force_optimum(ctx: _ScoringContext) -> float:
    """Exact battery optimum on a tiny instance (pdd.md 7.3).

    Enumerates every assignment of drones to turrets (or un-engaged) and, for each, every
    per-turret firing permutation, scoring with the solver's own scoring core. Tractable
    only for tiny instances - that is the point (the gap reference).
    """
    n_t = ctx.n_turrets
    n_d = ctx.n_drones
    # Each drone -> a turret index in [0, n_t) that can feasibly engage it, or n_t (=un-
    # engaged). Restrict to feasible turrets so we don't waste the enumeration.
    per_drone_choices: list[list[int]] = []
    for j in range(n_d):
        feas = [i for i in range(n_t) if ctx.feasible[i, j]]
        per_drone_choices.append(feas + [n_t])  # n_t == leave un-engaged

    best = 0.0
    for assignment in itertools.product(*per_drone_choices):
        # Group drones by assigned turret.
        groups: dict[int, list[int]] = {i: [] for i in range(n_t)}
        for j, i in enumerate(assignment):
            if i < n_t:
                groups[i].append(j)
        # For each turret independently, the best permutation maximizes its own value
        # (turrets are independent given the assignment), so we can optimize per turret.
        total = 0.0
        for i in range(n_t):
            members = groups[i]
            if not members:
                continue
            best_turret = 0.0
            for perm in itertools.permutations(members):
                sc = ctx.score_turret(i, list(perm))
                if sc > best_turret:
                    best_turret = sc
            total += best_turret
        if total > best:
            best = total
    return best


# --------------------------------------------------------------------------- #
# Validation helpers                                                           #
# --------------------------------------------------------------------------- #


def assert_valid_assignment(assignment: Assignment, state: WorldState) -> None:
    """LOS/range respected + at-most-one-turret-per-target (pdd.md 7.3)."""
    ctx = scoring_ctx(state)
    seen: set[str] = set()
    for turret_id, targets in assignment.turret_orders.items():
        assert turret_id in ctx.turret_index, f"unknown turret {turret_id!r}"
        i = ctx.turret_index[turret_id]
        for tid in targets:
            assert tid in ctx.drone_index, f"unknown drone {tid!r}"
            # At most one turret per target.
            assert tid not in seen, f"target {tid!r} assigned to >1 turret"
            seen.add(tid)
            j = ctx.drone_index[tid]
            # Engaged targets must be in range / reachable (LOS) for this turret.
            assert ctx.feasible[i, j], (
                f"target {tid!r} assigned to {turret_id!r} but not feasible "
                f"(out of range / no LOS)"
            )


# --------------------------------------------------------------------------- #
# Registration / protocol                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_registered(name: str) -> None:
    assert name in available_solvers()
    assert name in REGISTRY


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_implements_solver_protocol(name: str) -> None:
    cfg = load_config()
    instance = REGISTRY[name](config=cfg, seed=7)
    assert isinstance(instance, Solver)
    assert instance.name == name


# --------------------------------------------------------------------------- #
# Valid assignment                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_returns_valid_assignment(name: str) -> None:
    state = tiny_state()
    solver = REGISTRY[name](config=load_config(), seed=123)
    assignment = solver.solve(state, deadline_ms=80)
    assert isinstance(assignment, Assignment)
    assert_valid_assignment(assignment, state)


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_empty_state_returns_empty_assignment(name: str) -> None:
    state = WorldState(
        t=0.0,
        drones=[],
        turrets=[make_turret("t1", (0.0, 0.0))],
        weather_alpha=0.002,
        asset_pos=Vec2(x=0.0, y=0.0),
    )
    solver = REGISTRY[name](config=load_config(), seed=1)
    assignment = solver.solve(state, deadline_ms=20)
    assert assignment.turret_orders == {}
    assert assignment.objective_estimate == 0.0


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_out_of_range_drone_never_engaged(name: str) -> None:
    """A drone beyond every turret's range_max must be left un-engaged (pdd.md 7.3)."""
    turrets = [make_turret("t1", (0.0, 0.0), range_max=1000.0)]
    drones = [
        make_drone("near", (0.0, 500.0), (0.0, -20.0)),
        make_drone("far", (0.0, 4000.0), (0.0, -20.0)),  # > range_max
    ]
    state = WorldState(
        t=0.0,
        drones=drones,
        turrets=turrets,
        weather_alpha=load_config().weather("clear").alpha,
        asset_pos=Vec2(x=0.0, y=0.0),
    )
    solver = REGISTRY[name](config=load_config(), seed=42)
    assignment = solver.solve(state, deadline_ms=60)
    assert_valid_assignment(assignment, state)
    engaged = {t for ts in assignment.turret_orders.values() for t in ts}
    assert "far" not in engaged


# --------------------------------------------------------------------------- #
# Objective <= brute-force optimum, gap >= 0                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_objective_le_brute_force_and_gap_nonneg(name: str) -> None:
    state = tiny_state()
    ctx = scoring_ctx(state)
    optimum = brute_force_optimum(ctx)

    solver = REGISTRY[name](config=load_config(), seed=2024)
    assignment = solver.solve(state, deadline_ms=120)

    reported = assignment.objective_estimate
    # The solver must never claim more value than is achievable.
    assert reported <= optimum + 1e-6, (
        f"{name} reported {reported} > brute-force optimum {optimum}"
    )

    # gap = (obj_optimal - obj_policy) / max(obj_optimal, eps) must be non-negative.
    eps = 1e-9
    gap = (optimum - reported) / max(optimum, eps)
    assert gap >= -1e-9, f"{name} gap {gap} is negative"


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_reported_objective_matches_recomputed_score(name: str) -> None:
    """The self-reported objective_estimate must equal a fresh re-score of the returned
    assignment (no inflated estimates)."""
    state = tiny_state()
    ctx = scoring_ctx(state)
    solver = REGISTRY[name](config=load_config(), seed=99)
    assignment = solver.solve(state, deadline_ms=100)

    # Rebuild index-based orders and re-score.
    orders: dict[int, list[int]] = {}
    for turret_id, targets in assignment.turret_orders.items():
        i = ctx.turret_index[turret_id]
        orders[i] = [ctx.drone_index[t] for t in targets]
    recomputed = ctx.score(orders)
    assert math.isclose(recomputed, assignment.objective_estimate, rel_tol=0, abs_tol=1e-6)


def test_metaheuristic_finds_optimum_on_tiny_instance() -> None:
    """Given enough budget, the search should reach the brute-force optimum on a tiny
    instance (sanity that the search actually optimizes, not just stays feasible)."""
    state = tiny_state()
    ctx = scoring_ctx(state)
    optimum = brute_force_optimum(ctx)
    assert optimum > 0.0  # the tiny instance is non-degenerate

    ga = GeneticAlgorithmSolver(config=load_config(), seed=7)
    sa = SimulatedAnnealingSolver(config=load_config(), seed=7)
    ga_obj = ga.solve(state, deadline_ms=300).objective_estimate
    sa_obj = sa.solve(state, deadline_ms=300).objective_estimate
    assert math.isclose(ga_obj, optimum, rel_tol=0, abs_tol=1e-6)
    assert math.isclose(sa_obj, optimum, rel_tol=0, abs_tol=1e-6)


# --------------------------------------------------------------------------- #
# Determinism                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_deterministic_given_same_state_and_seed(name: str) -> None:
    state1 = tiny_state()
    state2 = tiny_state()
    s1 = REGISTRY[name](config=load_config(), seed=555)
    s2 = REGISTRY[name](config=load_config(), seed=555)
    a1 = s1.solve(state1, deadline_ms=100)
    a2 = s2.solve(state2, deadline_ms=100)
    assert a1.turret_orders == a2.turret_orders
    assert math.isclose(a1.objective_estimate, a2.objective_estimate, abs_tol=1e-9)


@pytest.mark.parametrize("name", SOLVER_NAMES)
def test_default_seed_from_config(name: str) -> None:
    """No explicit seed -> uses config's sim.seed; still deterministic."""
    state = tiny_state()
    a1 = REGISTRY[name](config=load_config()).solve(tiny_state(), deadline_ms=80)
    a2 = REGISTRY[name](config=load_config()).solve(state, deadline_ms=80)
    assert a1.turret_orders == a2.turret_orders
