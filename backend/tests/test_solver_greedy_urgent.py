"""Tests for the greedy_urgent solver (pdd.md sections 7, 9.2.3; mvp.md section 4).

What we assert:

- It self-registers in the solver REGISTRY and satisfies the Solver Protocol.
- Its output is a valid Assignment: only in-range (LOS=1 in v1) targets, every target
  claimed by at most one turret, every id real.
- It is deterministic (pdd.md section 9.1): identical state -> identical assignment,
  and input drone ordering does not change the result.
- On a TINY instance, its self-reported objective is ``<= the brute-force optimum`` and
  the optimality gap is ``>= 0`` (mvp.md section 4). The brute force enumerates every
  assignment + per-turret ordering under the *same* completion-time model the solver
  uses, so it is a true upper bound for this objective.
"""

from __future__ import annotations

import itertools
import math

import pytest

from beam.engine.kinematics import time_to_impact
from beam.schemas import (
    Assignment,
    Drone,
    ThermalConfig,
    Turret,
    Vec2,
    WorldState,
)
from beam.solvers import REGISTRY, get_solver_class
from beam.solvers.base import Solver
from beam.solvers.greedy_urgent import (
    GreedyUrgent,
    _dwell_estimate,
    _slew_estimate,
)
from beam.util import heading_to, vdist

# Track-efficiency estimation coefficients mirrored from the solver (pdd.md 18). The
# brute force must score with the exact same model the solver uses.
_ALPHA = 0.002  # "clear" weather extinction


def _thermal() -> ThermalConfig:
    return ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0)


def _turret(tid: str, x: float, y: float, *, aim: float = 0.0,
            range_max: float = 5000.0, power: float = 100.0) -> Turret:
    return Turret(
        id=tid,
        pos=Vec2(x=x, y=y),
        aim=aim,
        slew_rate=1.2,
        settle_time=0.1,
        power=power,
        range_max=range_max,
        thermal=0.0,
        thermal_cfg=_thermal(),
    )


def _drone(did: str, x: float, y: float, vx: float, vy: float, *,
           value: float = 2000.0, hardness: float = 40.0,
           class_name: str = "quad_small") -> Drone:
    return Drone(
        id=did,
        pos=Vec2(x=x, y=y),
        vel=Vec2(x=vx, y=vy),
        value=value,
        hardness=hardness,
        class_name=class_name,
    )


# --------------------------------------------------------------------------- #
# Brute-force optimum over the SAME completion-time model the solver uses      #
# --------------------------------------------------------------------------- #


def _completion_objective(state: WorldState, assignment: dict[str, list[str]]) -> float:
    """Value of targets killed before TTI under the pdd 7.2 completion model.

    Mirrors the solver: per turret, sweep its ordered list accumulating
    slew_setup + dwell; a target counts (adds its value) iff its cumulative
    completion time <= its TTI and it is in range.
    """
    turret_by_id = {t.id: t for t in state.turrets}
    drone_by_id = {d.id: d for d in state.drones}
    tti = {d.id: time_to_impact(d, state.asset_pos) for d in state.drones}
    total = 0.0
    for tid, order in assignment.items():
        tu = turret_by_id[tid]
        acc = 0.0
        aim = tu.aim
        for did in order:
            dr = drone_by_id[did]
            if vdist(tu.pos, dr.pos) > tu.range_max:
                continue  # infeasible: out of range, contributes nothing
            dwell = _dwell_estimate(tu, dr, state.weather_alpha)
            slew = _slew_estimate(tu, aim, dr)
            acc += slew + dwell
            aim = heading_to(tu.pos, dr.pos)
            if math.isfinite(acc) and acc <= tti[did]:
                total += dr.value
    return total


def _brute_force_optimum(state: WorldState) -> float:
    """Exhaustively enumerate assignment + per-turret order; return best objective.

    Tiny instances only: for each target choose a turret or "unassigned", then try
    every ordering of each turret's claimed set. This is the true optimum of the
    pdd 7.3 battery objective under the model both it and the solver share.
    """
    turret_ids = [t.id for t in state.turrets]
    drone_ids = [d.id for d in state.drones
                 if d.state in ("alive", "engaged")]
    choices = turret_ids + [None]  # None == leave un-engaged

    best = 0.0
    for combo in itertools.product(choices, repeat=len(drone_ids)):
        by_turret: dict[str, list[str]] = {tid: [] for tid in turret_ids}
        for did, pick in zip(drone_ids, combo):
            if pick is not None:
                by_turret[pick].append(did)
        # Try every per-turret ordering (independent across turrets).
        per_turret_perms = [
            list(itertools.permutations(by_turret[tid])) or [()]
            for tid in turret_ids
        ]
        for perm_combo in itertools.product(*per_turret_perms):
            assignment = {
                tid: list(perm) for tid, perm in zip(turret_ids, perm_combo)
            }
            obj = _completion_objective(state, assignment)
            if obj > best:
                best = obj
    return best


# --------------------------------------------------------------------------- #
# Scenarios                                                                    #
# --------------------------------------------------------------------------- #


def _tiny_state() -> WorldState:
    """2 turrets, 3 drones, clear weather; a mix of killable / unkillable deadlines."""
    turrets = [
        _turret("t1", -100.0, 0.0),
        _turret("t2", 100.0, 0.0),
    ]
    drones = [
        # Close, slow -> easily killable before impact.
        _drone("d1", 0.0, 500.0, 0.0, -20.0),
        # Closing fast from another bearing -> tight deadline.
        _drone("d2", 400.0, 300.0, -40.0, -80.0),
        # Far but slow -> killable, looser deadline.
        _drone("d3", -600.0, 800.0, 10.0, -15.0),
    ]
    return WorldState(
        t=0.0,
        drones=drones,
        turrets=turrets,
        weather_alpha=_ALPHA,
        asset_pos=Vec2(x=0.0, y=0.0),
    )


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_registered_and_protocol():
    assert "greedy_urgent" in REGISTRY
    assert get_solver_class("greedy_urgent") is GreedyUrgent
    solver = GreedyUrgent(seed=1337)
    assert solver.name == "greedy_urgent"
    assert isinstance(solver, Solver)


def test_returns_valid_assignment():
    state = _tiny_state()
    result = GreedyUrgent().solve(state, deadline_ms=250)
    assert isinstance(result, Assignment)

    turret_ids = {t.id for t in state.turrets}
    drone_by_id = {d.id: d for d in state.drones}
    turret_by_id = {t.id: t for t in state.turrets}

    seen: set[str] = set()
    for tid, order in result.turret_orders.items():
        assert tid in turret_ids
        for did in order:
            assert did in drone_by_id, "assigned a non-existent target"
            # at-most-one-turret-per-target
            assert did not in seen, f"target {did} assigned to >1 turret"
            seen.add(did)
            # range / LOS feasibility (LOS=1 in v1)
            assert vdist(turret_by_id[tid].pos, drone_by_id[did].pos) <= \
                turret_by_id[tid].range_max


def test_out_of_range_never_assigned():
    # A drone far beyond every turret's range_max must never be engaged.
    turrets = [_turret("t1", 0.0, 0.0, range_max=1000.0)]
    drones = [
        _drone("near", 0.0, 300.0, 0.0, -10.0),
        _drone("far", 0.0, 5000.0, 0.0, -10.0),  # outside range_max
    ]
    state = WorldState(
        t=0.0, drones=drones, turrets=turrets,
        weather_alpha=_ALPHA, asset_pos=Vec2(x=0.0, y=0.0),
    )
    result = GreedyUrgent().solve(state, deadline_ms=250)
    assigned = {d for order in result.turret_orders.values() for d in order}
    assert "far" not in assigned


def test_objective_le_brute_force_and_gap_nonneg():
    state = _tiny_state()
    result = GreedyUrgent().solve(state, deadline_ms=250)
    optimum = _brute_force_optimum(state)

    # Greedy can never beat the exhaustive optimum (mvp.md section 4).
    assert result.objective_estimate <= optimum + 1e-9

    # gap = (opt - policy) / max(opt, eps) must be >= 0.
    eps = 1e-9
    gap = (optimum - result.objective_estimate) / max(optimum, eps)
    assert gap >= -1e-9


def test_objective_matches_self_consistency():
    # The solver's self-reported objective must equal the value its OWN assignment
    # scores under the shared completion model (no double counting / inflation).
    state = _tiny_state()
    result = GreedyUrgent().solve(state, deadline_ms=250)
    scored = _completion_objective(state, result.turret_orders)
    assert result.objective_estimate == pytest.approx(scored, rel=1e-9, abs=1e-6)


def test_deterministic_and_order_independent():
    state = _tiny_state()
    a = GreedyUrgent().solve(state, deadline_ms=250)
    b = GreedyUrgent().solve(state, deadline_ms=250)
    assert a.turret_orders == b.turret_orders
    assert a.objective_estimate == b.objective_estimate

    # Shuffling the input drone list must not change the assignment (fixed tie-break).
    shuffled = WorldState(
        t=state.t,
        drones=list(reversed(state.drones)),
        turrets=state.turrets,
        weather_alpha=state.weather_alpha,
        asset_pos=state.asset_pos,
    )
    c = GreedyUrgent().solve(shuffled, deadline_ms=250)
    assert c.turret_orders == a.turret_orders


def test_does_not_mutate_state():
    state = _tiny_state()
    before = state.model_dump()
    GreedyUrgent().solve(state, deadline_ms=250)
    assert state.model_dump() == before


def test_skips_unkillable_targets():
    # A drone about to impact (TTI ~ 0) that no turret can complete in time must be
    # left un-engaged; the assignment should simply omit it.
    turrets = [_turret("t1", 0.0, 0.0)]
    drones = [
        _drone("doomed", 0.0, 1.0, 0.0, -10000.0),  # impacts almost instantly
        _drone("savable", 0.0, 400.0, 0.0, -10.0),
    ]
    state = WorldState(
        t=0.0, drones=drones, turrets=turrets,
        weather_alpha=_ALPHA, asset_pos=Vec2(x=0.0, y=0.0),
    )
    result = GreedyUrgent().solve(state, deadline_ms=250)
    assigned = {d for order in result.turret_orders.values() for d in order}
    assert "doomed" not in assigned
