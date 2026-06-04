"""Tests for the exact CP-SAT reference solver (pdd.md 7.3, 9.2.6).

Phase-2 acceptance (mvp.md section 4): CP-SAT is *provably optimal* on small
instances, verified against an independent brute force. These tests assert:

- ``solve`` returns a valid :class:`Assignment` honoring the structural constraints
  of pdd.md 7.3: only in-range / line-of-sight targets are engaged, and a target is
  serviced by at most one turret.
- The reported objective is ``<=`` the brute-force optimum and the gap ``>= 0``.
- On *tiny* instances the CP-SAT objective **equals** the brute-force optimum
  (exact-reference acceptance), and the returned schedule actually realizes that
  value when simulated with the same physics.

The brute force here is deliberately independent of the solver's CP-SAT model: it
enumerates every assignment of targets to turrets and every per-turret firing order,
scoring each with the engine's physics functions (dwell-to-kill, slew, TTI, thermal).
"""

from __future__ import annotations

import itertools
import math

import pytest

from beam.engine import physics
from beam.schemas import Drone, ThermalConfig, Turret, Vec2, WorldState
from beam.solvers.base import REGISTRY, get_solver_class
from beam.solvers.cp_sat import CpSatSolver
from beam.util import heading_to, vdist

# Config-sourced physics coefficients (pdd.md 18). Mirrored here so the brute force
# and the solver use identical inputs; the solver is constructed with the same values.
TRACK_BASE = 1.0
TRACK_RANGE_FALLOFF = 0.0015
ALPHA_CLEAR = 0.002


def _thermal(h_max: float = 1e9) -> ThermalConfig:
    """A thermal config; default headroom is effectively unbounded for kinematic tests."""
    return ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=h_max, h_resume=h_max / 2)


def _turret(
    tid: str,
    pos: Vec2,
    *,
    aim: float = 0.0,
    power: float = 100.0,
    slew_rate: float = 1.2,
    settle_time: float = 0.1,
    range_max: float = 5000.0,
    thermal: float = 0.0,
    h_max: float = 1e9,
) -> Turret:
    return Turret(
        id=tid,
        pos=pos,
        aim=aim,
        slew_rate=slew_rate,
        settle_time=settle_time,
        power=power,
        range_max=range_max,
        thermal=thermal,
        thermal_cfg=_thermal(h_max),
    )


def _drone(
    did: str,
    pos: Vec2,
    vel: Vec2,
    *,
    value: float,
    hardness: float,
    class_name: str = "quad_small",
) -> Drone:
    return Drone(
        id=did,
        pos=pos,
        vel=vel,
        value=value,
        hardness=hardness,
        class_name=class_name,
    )


# --------------------------------------------------------------------------- #
# Independent brute-force optimum over assignments x per-turret orderings       #
# --------------------------------------------------------------------------- #


def _dwell_s(turret: Turret, drone: Drone, alpha: float) -> float:
    rng = vdist(turret.pos, drone.pos)
    if rng > turret.range_max:
        return math.inf
    p = physics.delivered_power(turret.power, alpha, rng)
    eta = physics.track_efficiency(
        1.0, rng, base=TRACK_BASE, range_falloff=TRACK_RANGE_FALLOFF
    )
    dep = physics.deposition_rate(p, eta)
    return float(physics.dwell_to_kill(drone.hardness, dep))


def _slew_s(turret: Turret, aim_from: float, aim_to: float) -> float:
    return physics.slew_time(
        aim_from, aim_to, slew_rate=turret.slew_rate, settle_time=turret.settle_time
    )


def _tti_s(drone: Drone, asset: Vec2) -> float:
    dx, dy = asset.x - drone.pos.x, asset.y - drone.pos.y
    dist = math.hypot(dx, dy)
    if dist <= 1e-12:
        return 0.0
    ux, uy = dx / dist, dy / dist
    closing = drone.vel.x * ux + drone.vel.y * uy
    if closing <= 1e-12:
        return math.inf
    return dist / closing


def _score_turret_order(
    turret: Turret, order: list[Drone], alpha: float, asset: Vec2
) -> float:
    """Value killed by one turret firing ``order`` in sequence (pdd.md 7.2).

    Honors deadlines (TTI) and the thermal headroom cap, exactly as the model does.
    A target whose completion misses its deadline (or trips the thermal cap) is not
    counted, but — matching the engine — sequencing continues for the rest.
    """
    t = 0.0
    aim = turret.aim
    heat_used = 0.0
    headroom = turret.thermal_cfg.h_max - turret.thermal
    killed_value = 0.0
    for drone in order:
        d = _dwell_s(turret, drone, alpha)
        if not math.isfinite(d):
            return -math.inf  # infeasible order (out of range) -> reject
        aim_to = heading_to(turret.pos, drone.pos)
        s = _slew_s(turret, aim, aim_to)
        completion = t + s + d
        tti = _tti_s(drone, asset)
        heat = turret.thermal_cfg.heat_rate * d
        if completion <= tti + 1e-9 and (heat_used + heat) <= headroom + 1e-9:
            killed_value += drone.value
            heat_used += heat
        t = completion
        aim = aim_to
    return killed_value


def brute_force_optimum(state: WorldState) -> float:
    """Exhaustive optimum over all assignments and per-turret firing orders.

    Each live target is assigned to one turret or left un-engaged; each turret then
    fires its assigned set in its best order. Returns the maximum protected value.
    """
    drones = [d for d in state.drones if d.state in ("alive", "engaged")]
    turrets = list(state.turrets)
    alpha = state.weather_alpha
    asset = state.asset_pos
    n_t = len(turrets)

    best = 0.0
    # Each drone -> a turret index in [0, n_t) or "unassigned" (n_t).
    choices = list(range(n_t)) + [n_t]
    for assignment in itertools.product(choices, repeat=len(drones)):
        per_turret: list[list[Drone]] = [[] for _ in range(n_t)]
        for j, choice in enumerate(assignment):
            if choice < n_t:
                per_turret[choice].append(drones[j])
        total = 0.0
        for i in range(n_t):
            assigned = per_turret[i]
            if not assigned:
                continue
            best_for_turret = max(
                _score_turret_order(turrets[i], list(perm), alpha, asset)
                for perm in itertools.permutations(assigned)
            )
            if best_for_turret == -math.inf:
                best_for_turret = 0.0  # the empty/skip order is always available
            total += max(best_for_turret, 0.0)
        best = max(best, total)
    return best


def _simulate_assignment_value(state: WorldState, assignment) -> float:
    """Value actually realized by executing ``assignment``'s orders (pdd.md 7.2)."""
    turrets = {t.id: t for t in state.turrets}
    drones = {d.id: d for d in state.drones}
    total = 0.0
    for tid, order_ids in assignment.turret_orders.items():
        turret = turrets[tid]
        order = [drones[did] for did in order_ids]
        v = _score_turret_order(turret, order, state.weather_alpha, state.asset_pos)
        total += max(v, 0.0)
    return total


# --------------------------------------------------------------------------- #
# Structural validity                                                          #
# --------------------------------------------------------------------------- #


def _solver() -> CpSatSolver:
    return CpSatSolver(
        seed=1337,
        track_base=TRACK_BASE,
        track_range_falloff=TRACK_RANGE_FALLOFF,
        time_budget_ms=2000,
    )


def test_self_registers_in_registry():
    assert "cp_sat" in REGISTRY
    assert get_solver_class("cp_sat") is CpSatSolver


def test_empty_world_returns_empty_assignment():
    state = WorldState(
        t=0.0,
        drones=[],
        turrets=[_turret("t1", Vec2(x=0, y=0))],
        weather_alpha=ALPHA_CLEAR,
        asset_pos=Vec2(x=0, y=0),
    )
    a = _solver().solve(state, deadline_ms=500)
    assert a.turret_orders == {}
    assert a.objective_estimate == 0.0


def test_assignment_is_structurally_valid():
    asset = Vec2(x=0, y=0)
    turrets = [
        _turret("t1", Vec2(x=0, y=0)),
        _turret("t2", Vec2(x=100, y=0)),
    ]
    # Mix of in-range and far (out-of-range) drones.
    drones = [
        _drone("d0", Vec2(x=0, y=300), Vec2(x=0, y=-60), value=2000, hardness=40),
        _drone("d1", Vec2(x=200, y=200), Vec2(x=-40, y=-40), value=15000, hardness=120),
        _drone("d2", Vec2(x=0, y=9000), Vec2(x=0, y=-60), value=2000, hardness=40),  # far
    ]
    state = WorldState(
        t=0.0,
        drones=drones,
        turrets=turrets,
        weather_alpha=ALPHA_CLEAR,
        asset_pos=asset,
    )
    a = _solver().solve(state, deadline_ms=1500)

    turret_ids = {t.id for t in turrets}
    drone_by_id = {d.id: d for d in drones}

    seen: set[str] = set()
    for tid, order in a.turret_orders.items():
        assert tid in turret_ids
        turret = next(t for t in turrets if t.id == tid)
        for did in order:
            # at most one turret per target
            assert did not in seen, f"target {did} serviced by more than one turret"
            seen.add(did)
            drone = drone_by_id[did]
            # in range (range constraint, pdd.md 7.3)
            assert vdist(turret.pos, drone.pos) <= turret.range_max
            # killable (finite dwell -> LOS/reachability respected)
            assert math.isfinite(_dwell_s(turret, drone, ALPHA_CLEAR))

    # The out-of-range drone must never be engaged.
    assert "d2" not in seen


# --------------------------------------------------------------------------- #
# Optimality vs brute force                                                    #
# --------------------------------------------------------------------------- #


def _tiny_scenarios() -> list[WorldState]:
    """A handful of tiny, hand-built instances small enough to brute force."""
    scenarios: list[WorldState] = []

    # (1) Single turret, two close targets — ordering matters.
    scenarios.append(
        WorldState(
            t=0.0,
            drones=[
                _drone("d0", Vec2(x=0, y=250), Vec2(x=0, y=-80), value=2000, hardness=40),
                _drone("d1", Vec2(x=250, y=0), Vec2(x=-80, y=0), value=2000, hardness=40),
            ],
            turrets=[_turret("t1", Vec2(x=0, y=0))],
            weather_alpha=ALPHA_CLEAR,
            asset_pos=Vec2(x=0, y=0),
        )
    )

    # (2) Two turrets, three targets, mixed value — assignment + ordering.
    scenarios.append(
        WorldState(
            t=0.0,
            drones=[
                _drone("d0", Vec2(x=0, y=400), Vec2(x=0, y=-70), value=2000, hardness=40),
                _drone("d1", Vec2(x=400, y=0), Vec2(x=-70, y=0), value=15000, hardness=120),
                _drone("d2", Vec2(x=300, y=300), Vec2(x=-50, y=-50), value=2000, hardness=40),
            ],
            turrets=[
                _turret("t1", Vec2(x=-50, y=0)),
                _turret("t2", Vec2(x=50, y=0)),
            ],
            weather_alpha=ALPHA_CLEAR,
            asset_pos=Vec2(x=0, y=0),
        )
    )

    # (3) Tight deadlines: a fast, close drone forces a choice (can't kill all).
    scenarios.append(
        WorldState(
            t=0.0,
            drones=[
                _drone("d0", Vec2(x=0, y=120), Vec2(x=0, y=-110), value=2000, hardness=40),
                _drone("d1", Vec2(x=0, y=130), Vec2(x=0, y=-110), value=15000, hardness=120),
            ],
            turrets=[_turret("t1", Vec2(x=0, y=0))],
            weather_alpha=ALPHA_CLEAR,
            asset_pos=Vec2(x=0, y=0),
        )
    )

    # (4) Thermal-limited single turret: headroom too small to kill both.
    scenarios.append(
        WorldState(
            t=0.0,
            drones=[
                _drone("d0", Vec2(x=0, y=300), Vec2(x=0, y=-50), value=2000, hardness=40),
                _drone("d1", Vec2(x=300, y=0), Vec2(x=-50, y=0), value=2000, hardness=40),
            ],
            turrets=[_turret("t1", Vec2(x=0, y=0), h_max=0.05, thermal=0.0)],
            weather_alpha=ALPHA_CLEAR,
            asset_pos=Vec2(x=0, y=0),
        )
    )

    return scenarios


@pytest.mark.parametrize("idx", range(len(_tiny_scenarios())))
def test_cp_sat_equals_brute_force_on_tiny_instances(idx):
    state = _tiny_scenarios()[idx]
    opt = brute_force_optimum(state)
    a = _solver().solve(state, deadline_ms=2000)

    # Reported objective is non-negative and never exceeds the optimum, gap >= 0.
    assert a.objective_estimate >= 0.0
    assert a.objective_estimate <= opt + 1e-6
    gap = (opt - a.objective_estimate) / max(opt, 1e-9)
    assert gap >= -1e-9

    # Exact-reference acceptance: equals the brute-force optimum (mvp.md section 4).
    assert a.objective_estimate == pytest.approx(opt, abs=1e-6)

    # And the returned schedule actually realizes that value when executed.
    realized = _simulate_assignment_value(state, a)
    assert realized == pytest.approx(opt, abs=1e-6)


def test_respects_deadline_returns_assignment():
    """Even with a tight wall-clock deadline, solve returns a valid Assignment."""
    state = _tiny_scenarios()[1]
    a = _solver().solve(state, deadline_ms=1)
    # With a 1ms budget it may not prove optimality, but it must return something
    # structurally valid (best-so-far or empty), never raise.
    assert a.objective_estimate >= 0.0
    for order in a.turret_orders.values():
        assert isinstance(order, list)
