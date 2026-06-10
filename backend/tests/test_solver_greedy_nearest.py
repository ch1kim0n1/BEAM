"""Tests for the greedy nearest-first solver (pdd.md 9.2.1, 7).

Validates the solver contract surface (valid Assignment respecting range/LOS and the
at-most-one-turret-per-target constraint, pdd.md 7.3) and the headline correctness
property for a baseline policy: its self-reported objective never exceeds the
brute-force optimum on a TINY instance, and the implied optimality gap is >= 0
(mvp.md section 4 Phase-2 acceptance: gaps non-negative).

The brute-force reference here enumerates every assignment of targets to turrets and
every per-turret firing order, scoring each with the SAME physics the solver uses
(delivered power -> track efficiency -> dwell, plus slew setup and TTI deadlines,
pdd.md 7.2 / 8.3 / 8.4). It is the exact optimum for the modeled objective on small
instances, mirroring the role CP-SAT plays at scale (which must EQUAL brute force on
tiny instances per mvp.md section 4).
"""

from __future__ import annotations

import itertools
import math
from typing import Iterable


from beam.config import load_config
from beam.engine import physics
from beam.schemas import Assignment, Drone, ThermalConfig, Turret, Vec2, WorldState
from beam.solvers import REGISTRY, available_solvers
from beam.solvers.greedy_nearest import GreedyNearestSolver, _time_to_impact
from beam.util import vdist


# --------------------------------------------------------------------------- #
# Fixtures / builders                                                          #
# --------------------------------------------------------------------------- #


def _thermal() -> ThermalConfig:
    t = load_config().turret_defaults.thermal
    return ThermalConfig(
        heat_rate=t.heat_rate, cool_rate=t.cool_rate, h_max=t.h_max, h_resume=t.h_resume
    )


def _turret(
    tid: str, pos: Vec2, aim: float = 0.0, *, range_max: float = 5000.0, power: float = 100.0
) -> Turret:
    d = load_config().turret_defaults
    return Turret(
        id=tid,
        pos=pos,
        aim=aim,
        slew_rate=d.slew_rate,
        settle_time=d.settle_time,
        power=power,
        range_max=range_max,
        thermal=0.0,
        thermal_cfg=_thermal(),
    )


def _drone(
    did: str,
    pos: Vec2,
    vel: Vec2,
    *,
    value: float = 2000.0,
    hardness: float = 40.0,
    class_name: str = "quad_small",
) -> Drone:
    return Drone(
        id=did, pos=pos, vel=vel, value=value, hardness=hardness, class_name=class_name
    )


def _tiny_world() -> WorldState:
    """A small, easily-killable scenario: 2 turrets, 3 slow drones, clear weather."""
    cfg = load_config()
    alpha = cfg.weather("clear").alpha
    asset = Vec2(x=0.0, y=0.0)
    turrets = [
        _turret("t1", Vec2(x=-500.0, y=0.0)),
        _turret("t2", Vec2(x=500.0, y=0.0)),
    ]
    # Slow drones (speed ~5/s from ~600 out -> ~120 s TTI) so kills are feasible.
    drones = [
        _drone("d0", Vec2(x=-600.0, y=100.0), Vec2(x=5.0, y=-1.0)),
        _drone("d1", Vec2(x=600.0, y=-100.0), Vec2(x=-5.0, y=1.0)),
        _drone("d2", Vec2(x=0.0, y=700.0), Vec2(x=0.0, y=-5.0)),
    ]
    return WorldState(
        t=0.0, drones=drones, turrets=turrets, weather_alpha=alpha, asset_pos=asset
    )


# --------------------------------------------------------------------------- #
# Brute-force reference (exact optimum for the modeled objective, tiny only)   #
# --------------------------------------------------------------------------- #


def _dwell(turret: Turret, drone: Drone, alpha: float, base: float, falloff: float) -> float:
    rng = vdist(turret.pos, drone.pos)
    delivered = physics.delivered_power(turret.power, alpha, rng)
    eta = physics.track_efficiency(1.0, rng, base=base, range_falloff=falloff)
    dep = physics.deposition_rate(delivered, eta)
    remaining = max(0.0, drone.hardness - drone.energy_absorbed)
    return float(physics.dwell_to_kill(remaining, dep))


def _slew(turret: Turret, from_aim: float, drone: Drone) -> float:
    aim = math.atan2(drone.pos.y - turret.pos.y, drone.pos.x - turret.pos.x)
    return physics.slew_time(
        from_aim, aim, slew_rate=turret.slew_rate, settle_time=turret.settle_time
    )


def _score_order(
    turret: Turret,
    order: list[str],
    drone_by_id: dict[str, Drone],
    tti: dict[str, float],
    alpha: float,
    base: float,
    falloff: float,
) -> float:
    """Value of targets in ``order`` killed before TTI on this turret (pdd.md 7.2)."""
    cur_aim = turret.aim
    completion = 0.0
    value = 0.0
    for did in order:
        drone = drone_by_id[did]
        completion += _slew(turret, cur_aim, drone) + _dwell(turret, drone, alpha, base, falloff)
        if completion <= tti[did]:
            value += drone.value
        cur_aim = math.atan2(drone.pos.y - turret.pos.y, drone.pos.x - turret.pos.x)
    return value


def _all_partitions(
    targets: list[str], n_turrets: int
) -> Iterable[tuple[list[str], ...]]:
    """Every assignment of targets to turrets (each target to one turret or none).

    Yields a tuple of length n_turrets; entry i is the (unordered) list of target ids
    given to turret i. A target may be left unassigned. We then permute each bucket.
    """
    # Each target chooses a turret index in [0, n_turrets) or -1 (unassigned).
    choices = list(range(n_turrets)) + [-1]
    for combo in itertools.product(choices, repeat=len(targets)):
        buckets: tuple[list[str], ...] = tuple([] for _ in range(n_turrets))
        for did, c in zip(targets, combo):
            if c >= 0:
                buckets[c].append(did)
        yield buckets


def brute_force_optimum(state: WorldState) -> float:
    """Exact optimum of the modeled objective over assignments + per-turret orders."""
    cfg = load_config().physics.track_efficiency
    base, falloff = cfg.base, cfg.range_falloff
    alpha = state.weather_alpha
    drone_by_id = {d.id: d for d in state.drones}
    tti = {d.id: _time_to_impact(d.pos, d.vel, state.asset_pos) for d in state.drones}
    targets = [d.id for d in state.drones]

    best = 0.0
    for buckets in _all_partitions(targets, len(state.turrets)):
        # Skip assignments that violate range/LOS (turret can't reach a target).
        feasible = True
        for turret, bucket in zip(state.turrets, buckets):
            for did in bucket:
                if vdist(turret.pos, drone_by_id[did].pos) > turret.range_max:
                    feasible = False
                    break
            if not feasible:
                break
        if not feasible:
            continue

        # For each turret, pick the best firing order over its bucket.
        total = 0.0
        for turret, bucket in zip(state.turrets, buckets):
            best_turret = 0.0
            for order in itertools.permutations(bucket):
                v = _score_order(
                    turret, list(order), drone_by_id, tti, alpha, base, falloff
                )
                best_turret = max(best_turret, v)
            total += best_turret
        best = max(best, total)
    return best


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_registered():
    assert "greedy_nearest" in available_solvers()
    assert REGISTRY["greedy_nearest"] is GreedyNearestSolver
    assert GreedyNearestSolver.name == "greedy_nearest"


def test_returns_valid_assignment_type():
    state = _tiny_world()
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    assert isinstance(a, Assignment)


def test_at_most_one_turret_per_target():
    """pdd.md 7.3: Sum_i x_ij <= 1 — no target served by two turrets."""
    state = _tiny_world()
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    seen: set[str] = set()
    for ids in a.turret_orders.values():
        for did in ids:
            assert did not in seen, f"target {did} assigned to multiple turrets"
            seen.add(did)


def test_only_in_range_los_targets_assigned():
    """pdd.md 7.3: x_ij <= LOS_ij and 0 if range_ij > R_i."""
    state = _tiny_world()
    turret_by_id = {t.id: t for t in state.turrets}
    drone_by_id = {d.id: d for d in state.drones}
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    for tid, ids in a.turret_orders.items():
        turret = turret_by_id[tid]
        for did in ids:
            drone = drone_by_id[did]
            assert vdist(turret.pos, drone.pos) <= turret.range_max


def test_orders_reference_known_targets():
    state = _tiny_world()
    known = {d.id for d in state.drones}
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    for ids in a.turret_orders.values():
        for did in ids:
            assert did in known


def test_excludes_out_of_range_target():
    """A target beyond every turret's range_max is never assigned."""
    state = _tiny_world()
    # Add a far drone outside both turrets' (5000 m) reach.
    far = _drone("far", Vec2(x=0.0, y=20000.0), Vec2(x=0.0, y=-5.0))
    state = state.model_copy(update={"drones": state.drones + [far]})
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    for ids in a.turret_orders.values():
        assert "far" not in ids


def test_nearest_is_chosen_first():
    """Each turret's first target is its nearest engageable drone."""
    state = _tiny_world()
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    turret_by_id = {t.id: t for t in state.turrets}
    drone_by_id = {d.id: d for d in state.drones}
    for tid, ids in a.turret_orders.items():
        if not ids:
            continue
        turret = turret_by_id[tid]
        first_dist = vdist(turret.pos, drone_by_id[ids[0]].pos)
        # No earlier-claimed-by-this-turret target was nearer than the first.
        for did in ids[1:]:
            assert vdist(turret.pos, drone_by_id[did].pos) >= first_dist - 1e-9


def test_objective_le_brute_force_and_gap_nonnegative():
    """Baseline floor: greedy objective <= exact optimum; gap >= 0 (mvp.md 4)."""
    state = _tiny_world()
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    opt = brute_force_optimum(state)
    eps = 1e-9
    assert a.objective_estimate <= opt + eps, (a.objective_estimate, opt)
    gap = (opt - a.objective_estimate) / max(opt, eps)
    assert gap >= -eps


def test_deterministic_under_seed():
    """Same state -> identical assignment regardless of seed (deterministic policy)."""
    state = _tiny_world()
    a1 = GreedyNearestSolver(seed=1).solve(state, deadline_ms=200)
    a2 = GreedyNearestSolver(seed=999).solve(state, deadline_ms=200)
    assert a1.turret_orders == a2.turret_orders
    assert a1.objective_estimate == a2.objective_estimate


def test_respects_deadline_zero():
    """A zero deadline returns a valid (possibly empty) Assignment, no crash."""
    state = _tiny_world()
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=0)
    assert isinstance(a, Assignment)
    # at-most-one invariant still holds on whatever partial work was produced
    seen: set[str] = set()
    for ids in a.turret_orders.values():
        for did in ids:
            assert did not in seen
            seen.add(did)


def test_empty_world():
    cfg = load_config()
    state = WorldState(
        t=0.0,
        drones=[],
        turrets=[_turret("t1", Vec2(x=0.0, y=0.0))],
        weather_alpha=cfg.weather("clear").alpha,
        asset_pos=Vec2(x=0.0, y=0.0),
    )
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    assert a.turret_orders == {}
    assert a.objective_estimate == 0.0


def test_unengaged_drone_not_scored_when_unkillable():
    """A drone that impacts before any kill can complete contributes 0 to objective."""
    cfg = load_config()
    alpha = cfg.weather("clear").alpha
    # Fast drone, very close to asset -> TTI ~ 0, cannot be killed in time.
    fast = _drone("fast", Vec2(x=10.0, y=0.0), Vec2(x=-1000.0, y=0.0))
    state = WorldState(
        t=0.0,
        drones=[fast],
        turrets=[_turret("t1", Vec2(x=-50.0, y=0.0))],
        weather_alpha=alpha,
        asset_pos=Vec2(x=0.0, y=0.0),
    )
    a = GreedyNearestSolver(seed=1337).solve(state, deadline_ms=200)
    # It may still be assigned (nearest-first commits) but must not be scored.
    assert a.objective_estimate == 0.0
