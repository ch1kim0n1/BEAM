"""Acceptance tests for the auction / linear-assignment solver (pdd.md 7, 9.2.4).

The auction solver is a *heuristic* assignment policy, so its acceptance bar
(mvp.md section 4, Phase-2) is:

- it returns a **valid** :class:`Assignment` — every ordered target is in range and
  line of sight of its turret, and no target is claimed by two turrets
  (at-most-one-turret-per-target, pdd.md 7.3);
- its self-reported objective is **<= the brute-force optimum** on a TINY instance,
  and the optimality **gap is >= 0** (pdd.md 9.3). (Exact equality to brute force is
  required only of the cp_sat reference, not of this heuristic.)

The brute-force oracle here re-derives the SAME objective the solver targets (pdd.md
7.2/7.3): a target counts iff its cumulative single-machine completion time
(slew + dwell, sequence-dependent) beats its TTI; the value reported is the sum of
on-time target values, maximized over all assignments and per-turret orders.

All physics coefficients come from ``beam.config`` (no hard-coded constants); the few
synthetic geometric values exist only to build a controllable tiny scenario.
"""

from __future__ import annotations

import itertools
import math
from typing import Optional

import pytest

from beam.config import load_config
from beam.engine import kinematics, physics
from beam.schemas import Assignment, Drone, ThermalConfig, Turret, Vec2, WorldState
from beam.solvers import available_solvers, get_solver_class
from beam.util import angular_distance, vdist


# --------------------------------------------------------------------------- #
# Fixtures: a config-driven turret factory + tiny scenarios                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _make_turret(cfg, tid: str, pos: Vec2, aim: float) -> Turret:
    """A turret with all physics params pulled from turret_defaults (no constants)."""
    td = cfg.turret_defaults
    return Turret(
        id=tid,
        pos=pos,
        aim=aim,
        slew_rate=td.slew_rate,
        settle_time=td.settle_time,
        power=td.power,
        range_max=td.range_max,
        thermal=0.0,
        thermal_cfg=ThermalConfig(
            heat_rate=td.thermal.heat_rate,
            cool_rate=td.thermal.cool_rate,
            h_max=td.thermal.h_max,
            h_resume=td.thermal.h_resume,
        ),
    )


def _make_drone(cfg, did: str, pos: Vec2, vel: Vec2, class_name: str) -> Drone:
    dc = cfg.drone_class(class_name)
    return Drone(
        id=did,
        pos=pos,
        vel=vel,
        value=dc.value,
        hardness=dc.hardness,
        class_name=class_name,
    )


def _inbound(asset: Vec2, pos: Vec2, speed: float) -> Vec2:
    return kinematics.initial_velocity(pos, asset, speed)


def _world(cfg, turrets: list[Turret], drones: list[Drone], asset: Vec2) -> WorldState:
    clear = cfg.weather("clear")
    return WorldState(
        t=0.0,
        drones=drones,
        turrets=turrets,
        weather_alpha=clear.alpha,
        asset_pos=asset,
    )


# --------------------------------------------------------------------------- #
# Brute-force oracle (re-derives the pdd.md 7.2/7.3 objective)                 #
# --------------------------------------------------------------------------- #


def _dwell(cfg, tur: Turret, dr: Drone, alpha: float) -> float:
    rng_m = vdist(tur.pos, dr.pos)
    if rng_m > tur.range_max:
        return math.inf
    p = physics.delivered_power(tur.power, alpha, rng_m)
    eta = physics.track_efficiency(
        1.0,
        rng_m,
        base=cfg.physics.track_efficiency.base,
        range_falloff=cfg.physics.track_efficiency.range_falloff,
    )
    return float(physics.dwell_to_kill(dr.hardness, physics.deposition_rate(p, eta)))


def _slew0(tur: Turret, dr: Drone) -> float:
    return kinematics.slew_time(tur, kinematics.aim_at(tur, dr.pos))


def _slew_between(tur: Turret, a: Drone, b: Drone) -> float:
    aa = kinematics.aim_at(tur, a.pos)
    ab = kinematics.aim_at(tur, b.pos)
    return angular_distance(aa, ab) / tur.slew_rate + tur.settle_time


def _sequence_value(cfg, tur: Turret, seq: list[Drone], tti, alpha: float) -> float:
    """Value of on-time targets for one turret firing ``seq`` in order (pdd.md 7.2)."""
    completion = 0.0
    last: Optional[Drone] = None
    total = 0.0
    for dr in seq:
        setup = _slew0(tur, dr) if last is None else _slew_between(tur, last, dr)
        completion += setup + _dwell(cfg, tur, dr, alpha)
        if math.isfinite(completion) and completion <= tti[dr.id] + 1e-9:
            total += dr.value
        last = dr
    return total


def _best_for_turret(cfg, tur: Turret, subset: list[Drone], tti, alpha: float) -> float:
    """Max on-time value over all orderings of ``subset`` for a single turret."""
    best = 0.0
    if not subset:
        return 0.0
    for perm in itertools.permutations(subset):
        best = max(best, _sequence_value(cfg, tur, list(perm), tti, alpha))
    return best


def _brute_force_optimum(cfg, state: WorldState) -> float:
    """Optimal total protected value over all assignments + per-turret orders.

    Enumerates every way to assign each target to a turret or to "none" (the
    at-most-one-turret-per-target constraint, pdd.md 7.3), then optimizes each
    turret's order independently. Tiny-instance only (exponential).
    """
    turrets = state.turrets
    drones = [d for d in state.drones if d.state in ("alive", "engaged")]
    alpha = state.weather_alpha
    tti = {d.id: kinematics.time_to_impact(d, state.asset_pos) for d in drones}

    n_i = len(turrets)
    best_total = 0.0
    # Each drone -> turret index in [0, n_i) or n_i meaning "unassigned".
    for choice in itertools.product(range(n_i + 1), repeat=len(drones)):
        buckets: list[list[Drone]] = [[] for _ in range(n_i)]
        for d_idx, t_idx in enumerate(choice):
            if t_idx < n_i:
                buckets[t_idx].append(drones[d_idx])
        total = sum(
            _best_for_turret(cfg, turrets[i], buckets[i], tti, alpha)
            for i in range(n_i)
        )
        best_total = max(best_total, total)
    return best_total


# --------------------------------------------------------------------------- #
# Assignment validity helper                                                   #
# --------------------------------------------------------------------------- #


def _assert_valid_assignment(state: WorldState, asn: Assignment) -> None:
    tur_by_id = {t.id: t for t in state.turrets}
    dr_by_id = {d.id: d for d in state.drones}
    seen: set[str] = set()
    for tid, targets in asn.turret_orders.items():
        assert tid in tur_by_id, f"unknown turret {tid}"
        tur = tur_by_id[tid]
        for did in targets:
            assert did in dr_by_id, f"unknown drone {did}"
            # at-most-one-turret-per-target (pdd.md 7.3).
            assert did not in seen, f"target {did} claimed twice"
            seen.add(did)
            dr = dr_by_id[did]
            # in range (LOS in v1 = range; no occluders) (pdd.md 7.3).
            assert vdist(tur.pos, dr.pos) <= tur.range_max + 1e-6, (
                f"{tid}->{did} out of range"
            )


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_registered_and_protocol():
    assert "auction" in available_solvers()
    from beam.solvers.base import Solver

    solver = get_solver_class("auction")(seed=1337)
    assert solver.name == "auction"
    assert isinstance(solver, Solver)


def test_empty_world_is_safe(cfg):
    asset = Vec2(x=0.0, y=0.0)
    turrets = [_make_turret(cfg, "t1", Vec2(x=0, y=0), 0.0)]
    state = _world(cfg, turrets, [], asset)
    asn = get_solver_class("auction")(seed=1337).solve(state, deadline_ms=50)
    assert asn.turret_orders == {}
    assert asn.objective_estimate == 0.0


def test_out_of_range_target_never_engaged(cfg):
    """A drone beyond range_max must not appear in any turret's order (pdd.md 7.3)."""
    asset = Vec2(x=0.0, y=0.0)
    td = cfg.turret_defaults
    turrets = [_make_turret(cfg, "t1", Vec2(x=0, y=0), 0.0)]
    far = _make_drone(
        cfg, "d_far", Vec2(x=td.range_max * 2.0, y=0.0), Vec2(x=-10, y=0), "quad_small"
    )
    state = _world(cfg, turrets, [far], asset)
    asn = get_solver_class("auction")(seed=1337).solve(state, deadline_ms=50)
    _assert_valid_assignment(state, asn)
    assert all("d_far" not in t for t in asn.turret_orders.values())


def _tiny_scenario(cfg):
    """2 turrets, 3 inbound drones inside range — small enough to brute force."""
    asset = Vec2(x=0.0, y=0.0)
    turrets = [
        _make_turret(cfg, "t1", Vec2(x=-200.0, y=0.0), 0.0),
        _make_turret(cfg, "t2", Vec2(x=200.0, y=0.0), math.pi),
    ]
    speed = 60.0
    drones = [
        _make_drone(cfg, "d0", Vec2(x=-400.0, y=300.0), Vec2(), "quad_small"),
        _make_drone(cfg, "d1", Vec2(x=350.0, y=250.0), Vec2(), "fixed_wing"),
        _make_drone(cfg, "d2", Vec2(x=50.0, y=-450.0), Vec2(), "quad_small"),
    ]
    for d in drones:
        d.vel = _inbound(asset, d.pos, speed)
    return _world(cfg, turrets, drones, asset)


def test_tiny_valid_and_bounded_by_brute_force(cfg):
    state = _tiny_scenario(cfg)
    solver = get_solver_class("auction")(
        seed=1337,
        track_base=cfg.physics.track_efficiency.base,
        track_range_falloff=cfg.physics.track_efficiency.range_falloff,
    )
    asn = solver.solve(state, deadline_ms=250)

    # Valid assignment (range + at-most-one-turret-per-target).
    _assert_valid_assignment(state, asn)

    optimum = _brute_force_optimum(cfg, state)

    # Heuristic objective must not exceed the true optimum, and gap >= 0 (pdd.md 9.3).
    assert asn.objective_estimate <= optimum + 1e-6, (
        f"auction obj {asn.objective_estimate} > brute-force optimum {optimum}"
    )
    eps = 1e-9
    gap = (optimum - asn.objective_estimate) / max(optimum, eps)
    assert gap >= -1e-9, f"negative gap {gap}"


def test_objective_matches_assignment_value(cfg):
    """The reported objective equals the on-time value of the chosen schedule.

    This guards against a solver that over-reports: recomputing each turret's queued
    sequence value (pdd.md 7.2) must reproduce ``objective_estimate``.
    """
    state = _tiny_scenario(cfg)
    asn = get_solver_class("auction")(
        seed=1337,
        track_base=cfg.physics.track_efficiency.base,
        track_range_falloff=cfg.physics.track_efficiency.range_falloff,
    ).solve(state, deadline_ms=250)

    tti = {d.id: kinematics.time_to_impact(d, state.asset_pos) for d in state.drones}
    dr_by_id = {d.id: d for d in state.drones}
    tur_by_id = {t.id: t for t in state.turrets}

    recomputed = 0.0
    for tid, dids in asn.turret_orders.items():
        seq = [dr_by_id[d] for d in dids]
        recomputed += _sequence_value(
            cfg, tur_by_id[tid], seq, tti, state.weather_alpha
        )
    assert recomputed == pytest.approx(asn.objective_estimate, abs=1e-6)


def test_deterministic_under_identical_state(cfg):
    """Identical WorldState -> identical Assignment (contract determinism)."""
    state = _tiny_scenario(cfg)
    a = get_solver_class("auction")(seed=1337).solve(state, deadline_ms=250)
    b = get_solver_class("auction")(seed=1337).solve(state, deadline_ms=250)
    assert a.turret_orders == b.turret_orders
    assert a.objective_estimate == b.objective_estimate


def test_does_not_mutate_state(cfg):
    """Solver reads only the snapshot; it must not mutate WorldState (pdd.md 9.1)."""
    state = _tiny_scenario(cfg)
    before = state.model_dump()
    get_solver_class("auction")(seed=1337).solve(state, deadline_ms=250)
    assert state.model_dump() == before
