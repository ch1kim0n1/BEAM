"""Focused tests for the greedy_threat solver (pdd.md sections 7, 9.2.2; mvp.md 4).

We verify the three correctness properties the task and mvp.md section 4 require of a
heuristic policy:

1. The returned ``Assignment`` is *valid*: it respects line-of-sight/range, schedule
   feasibility (kill before TTI), and the at-most-one-turret-per-target constraint
   (pdd.md section 7.3).
2. The reported objective is ``<=`` the brute-force optimum on a TINY instance, and the
   optimality gap is ``>= 0`` (mvp.md section 4: heuristic gaps are non-negative; the
   exact CP-SAT reference would *equal* the brute force, but greedy need not).
3. Determinism: identical input -> identical output (pdd.md section 21).

The brute-force optimum here is computed with the *same* dwell/slew/thermal/TTI model
the solver uses (delegated to ``beam.engine.physics``), so the comparison is honest:
both optimize "value of drones killed before TTI" (pdd.md section 7.3) over the same
feasibility rules. This is the bound a heuristic's reported objective must not beat.
"""

from __future__ import annotations

import math
from itertools import permutations, product

import pytest

from beam.engine import physics
from beam.schemas import Assignment, Drone, ThermalConfig, Turret, Vec2, WorldState
from beam.solvers import REGISTRY, available_solvers, get_solver_class
from beam.solvers.greedy_threat import (
    GreedyThreat,
    _DEFAULT_RANGE_FALLOFF,
    _DEFAULT_TRACK_BASE,
    physics_time_to_impact,
)
from beam.util import angular_distance, heading_to, vdist

DEADLINE_MS = 250


# --------------------------------------------------------------------------- #
# Builders                                                                     #
# --------------------------------------------------------------------------- #


def _thermal(h_max: float = 1e9) -> ThermalConfig:
    """Thermal config with an effectively-unbounded cap (thermal not the constraint
    under test) unless a caller lowers ``h_max``."""
    return ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=h_max, h_resume=h_max / 2.0)


def _turret(
    tid: str,
    pos: Vec2,
    *,
    aim: float = 0.0,
    slew_rate: float = 1.2,
    settle_time: float = 0.1,
    power: float = 100.0,
    range_max: float = 5000.0,
    thermal: float = 0.0,
    thermal_cfg: ThermalConfig | None = None,
    state: str = "idle",
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
        thermal_cfg=thermal_cfg or _thermal(),
        state=state,
    )


def _drone_toward_asset(
    did: str,
    pos: Vec2,
    asset: Vec2,
    *,
    speed: float,
    value: float,
    hardness: float,
    class_name: str = "quad_small",
) -> Drone:
    """A drone heading straight at the asset at ``speed`` (so TTI is finite)."""
    dx, dy = asset.x - pos.x, asset.y - pos.y
    dist = math.hypot(dx, dy)
    vx, vy = (dx / dist) * speed, (dy / dist) * speed
    return Drone(
        id=did,
        pos=pos,
        vel=Vec2(x=vx, y=vy),
        value=value,
        hardness=hardness,
        class_name=class_name,
    )


# --------------------------------------------------------------------------- #
# Reference model (mirrors the solver's feasibility/objective, pdd.md 7.2/7.3) #
# --------------------------------------------------------------------------- #


def _dwell(turret: Turret, drone: Drone, alpha: float) -> float:
    rng_m = vdist(turret.pos, drone.pos)
    p_del = float(physics.delivered_power(turret.power, alpha, rng_m))
    eta = float(
        physics.track_efficiency(
            1.0, rng_m, base=_DEFAULT_TRACK_BASE, range_falloff=_DEFAULT_RANGE_FALLOFF
        )
    )
    dep = float(physics.deposition_rate(p_del, eta))
    return float(physics.dwell_to_kill(drone.hardness, dep))


def _schedule_value(turret: Turret, order: list[Drone], state: WorldState) -> float:
    """Value of targets in ``order`` that ``turret`` kills before their TTI, honoring
    range, schedule (slew+dwell), and thermal headroom (pdd.md 7.2)."""
    running_c = 0.0
    aim = turret.aim
    heat = 0.0
    cfg = turret.thermal_cfg
    headroom = math.inf
    if cfg.heat_rate > 0.0:
        headroom = max(0.0, (cfg.h_max - turret.thermal) / cfg.heat_rate)
    value = 0.0
    for d in order:
        rng_m = vdist(turret.pos, d.pos)
        if rng_m > turret.range_max:
            continue
        dwell = _dwell(turret, d, state.weather_alpha)
        if not math.isfinite(dwell):
            continue
        if heat + dwell > headroom:
            continue
        target_aim = heading_to(turret.pos, d.pos)
        slew = angular_distance(aim, target_aim) / turret.slew_rate + turret.settle_time
        completion = running_c + slew + dwell
        tti = physics_time_to_impact(d, state.asset_pos)
        if tti <= 0.0 or not math.isfinite(tti) or completion > tti:
            continue
        value += d.value
        running_c = completion
        aim = target_aim
        heat += dwell
    return value


def _brute_force_optimum(state: WorldState) -> float:
    """Exact best 'value killed before TTI' over all assignments + per-turret orders.

    Enumerates every way to assign each live drone to one of the turrets or to 'none'
    (at-most-one-turret-per-target is structural), then the best firing order per
    turret. Tiny instances only — this is the optimal the heuristic is graded against
    (a CP-SAT reference would match this exactly; pdd.md section 9.2.6, mvp.md 4)."""
    live = [d for d in state.drones if d.state in ("alive", "engaged")]
    turrets = state.turrets
    tids = [t.id for t in turrets]
    by_id = {t.id: t for t in turrets}

    best = 0.0
    # Each drone -> a turret id or None.
    for combo in product([None, *tids], repeat=len(live)):
        per_turret: dict[str, list[Drone]] = {tid: [] for tid in tids}
        for d, choice in zip(live, combo):
            if choice is not None:
                per_turret[choice].append(d)
        total = 0.0
        for tid, drones in per_turret.items():
            if not drones:
                continue
            # Best order for this turret's assigned set.
            best_t = 0.0
            for perm in permutations(drones):
                best_t = max(best_t, _schedule_value(by_id[tid], list(perm), state))
            total += best_t
        best = max(best, total)
    return best


# --------------------------------------------------------------------------- #
# Assignment validation                                                        #
# --------------------------------------------------------------------------- #


def _assert_valid_assignment(assignment: Assignment, state: WorldState) -> None:
    live_ids = {d.id for d in state.drones if d.state in ("alive", "engaged")}
    turret_ids = {t.id for t in state.turrets}
    by_id = {d.id: d for d in state.drones}
    turret_by_id = {t.id: t for t in state.turrets}

    seen: set[str] = set()
    for tid, order in assignment.turret_orders.items():
        assert tid in turret_ids, f"unknown turret {tid}"
        turret = turret_by_id[tid]
        for did in order:
            # At-most-one-turret-per-target (pdd.md 7.3).
            assert did not in seen, f"target {did} assigned to two turrets"
            seen.add(did)
            assert did in live_ids, f"target {did} not a live target"
            d = by_id[did]
            # Range / LOS gate (pdd.md 7.3): assigned targets must be in range.
            assert vdist(turret.pos, d.pos) <= turret.range_max, "out-of-range assign"

        # Per-turret schedule feasibility: every committed target killed before TTI.
        committed = [by_id[did] for did in order]
        killed_value = _schedule_value(turret, committed, state)
        # Every committed target should be feasibly killable in this order.
        full_value = sum(by_id[did].value for did in order)
        assert killed_value == pytest.approx(full_value), (
            "solver committed a target it cannot kill before TTI"
        )


# --------------------------------------------------------------------------- #
# Tiny instances                                                               #
# --------------------------------------------------------------------------- #


def _tiny_state() -> WorldState:
    """One turret at origin, three closing drones of mixed value/urgency."""
    asset = Vec2(x=0.0, y=0.0)
    turret = _turret("t1", Vec2(x=0.0, y=0.0), range_max=5000.0)
    drones = [
        # Close, high value, urgent (high v/TTI) -> should be engaged first.
        _drone_toward_asset("d0", Vec2(x=400.0, y=0.0), asset, speed=60.0, value=15000.0, hardness=40.0),
        # Mid range, low value.
        _drone_toward_asset("d1", Vec2(x=900.0, y=300.0), asset, speed=60.0, value=2000.0, hardness=40.0),
        # Far, low value.
        _drone_toward_asset("d2", Vec2(x=1500.0, y=-200.0), asset, speed=60.0, value=2000.0, hardness=40.0),
    ]
    return WorldState(t=0.0, drones=drones, turrets=[turret], weather_alpha=0.002, asset_pos=asset)


def _two_turret_state() -> WorldState:
    asset = Vec2(x=0.0, y=0.0)
    turrets = [
        _turret("t1", Vec2(x=-100.0, y=0.0), range_max=5000.0),
        _turret("t2", Vec2(x=100.0, y=0.0), range_max=5000.0),
    ]
    drones = [
        _drone_toward_asset("d0", Vec2(x=500.0, y=200.0), asset, speed=80.0, value=15000.0, hardness=40.0),
        _drone_toward_asset("d1", Vec2(x=-500.0, y=200.0), asset, speed=80.0, value=2000.0, hardness=40.0),
        _drone_toward_asset("d2", Vec2(x=300.0, y=-600.0), asset, speed=80.0, value=2000.0, hardness=120.0),
    ]
    return WorldState(t=0.0, drones=drones, turrets=turrets, weather_alpha=0.010, asset_pos=asset)


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


def test_registered_in_registry():
    assert "greedy_threat" in available_solvers()
    assert get_solver_class("greedy_threat") is GreedyThreat
    assert REGISTRY["greedy_threat"] is GreedyThreat


def test_implements_solver_protocol():
    from beam.solvers import Solver

    s = GreedyThreat(seed=1337)
    assert isinstance(s, Solver)
    assert s.name == "greedy_threat"


def test_returns_valid_assignment_single_turret():
    state = _tiny_state()
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert isinstance(out, Assignment)
    _assert_valid_assignment(out, state)


def test_returns_valid_assignment_two_turrets():
    state = _two_turret_state()
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    _assert_valid_assignment(out, state)


def test_threat_priority_engages_highest_value_density_first():
    """The most urgent, high-value drone (highest v/TTI) is engaged first."""
    state = _tiny_state()
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    # d0 is closest + highest value -> highest v/TTI -> first target of some turret.
    first_targets = {order[0] for order in out.turret_orders.values() if order}
    assert "d0" in first_targets


@pytest.mark.parametrize("builder", [_tiny_state, _two_turret_state])
def test_objective_le_brute_force_and_gap_nonnegative(builder):
    state = builder()
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    optimum = _brute_force_optimum(state)

    # Heuristic objective must not exceed the true optimum (mvp.md section 4).
    assert out.objective_estimate <= optimum + 1e-6

    # Optimality gap is non-negative (pdd.md section 9.3).
    eps = 1e-9
    gap = (optimum - out.objective_estimate) / max(optimum, eps)
    assert gap >= -1e-9


def test_objective_estimate_matches_assignment_value():
    """The reported objective equals the value the assignment actually kills in time."""
    state = _two_turret_state()
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    realized = 0.0
    by_id = {d.id: d for d in state.drones}
    turret_by_id = {t.id: t for t in state.turrets}
    for tid, order in out.turret_orders.items():
        realized += _schedule_value(turret_by_id[tid], [by_id[d] for d in order], state)
    assert out.objective_estimate == pytest.approx(realized)


def test_deterministic_repeat():
    state = _tiny_state()
    a = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    b = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert a.model_dump() == b.model_dump()


def test_does_not_mutate_state():
    state = _tiny_state()
    before = state.model_dump()
    GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert state.model_dump() == before


def test_out_of_range_targets_are_not_engaged():
    asset = Vec2(x=0.0, y=0.0)
    turret = _turret("t1", Vec2(x=0.0, y=0.0), range_max=500.0)
    drones = [
        _drone_toward_asset("d0", Vec2(x=2000.0, y=0.0), asset, speed=60.0, value=15000.0, hardness=40.0),
    ]
    state = WorldState(t=0.0, drones=drones, turrets=[turret], weather_alpha=0.002, asset_pos=asset)
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert out.turret_orders == {}
    assert out.objective_estimate == 0.0


def test_thermal_headroom_limits_engagement():
    """A turret with almost no thermal headroom cannot fit a long dwell -> no kill."""
    asset = Vec2(x=0.0, y=0.0)
    # Tiny headroom: thermal already near h_max so dwell cannot fit.
    cfg = ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0)
    turret = _turret("t1", Vec2(x=0.0, y=0.0), thermal=99.999, thermal_cfg=cfg)
    drones = [
        _drone_toward_asset("d0", Vec2(x=800.0, y=0.0), asset, speed=40.0, value=15000.0, hardness=40.0),
    ]
    state = WorldState(t=0.0, drones=drones, turrets=[turret], weather_alpha=0.002, asset_pos=asset)
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert out.turret_orders == {}


def test_cooldown_turret_unavailable():
    state = _tiny_state()
    state.turrets[0].state = "cooldown"
    out = GreedyThreat(seed=1337).solve(state, DEADLINE_MS)
    assert out.turret_orders == {}
    assert out.objective_estimate == 0.0
