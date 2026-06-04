"""Tests for beam.engine.kinematics (pdd.md section 8.1).

Covered:
- direct drone reaches the asset in the expected straight-line time;
- TTI decreases monotonically as a drone approaches the asset;
- flocking trajectories stay bounded (no blow-up) and keep advancing;
- determinism: same seed -> identical trajectory; different seed -> different.

All physics/kinematics tunables come from config (config/defaults.yaml); the tests
read them via beam.config + KinematicsConfig.from_raw, never hard-coding constants.
"""

from __future__ import annotations

import math

import pytest
import yaml

from beam.config import config_dir, load_config
from beam.engine.kinematics import (
    KinematicsConfig,
    aim_at,
    initial_velocity,
    slew_aim,
    spawn_swarm,
    staggered_release_time,
    step_direct,
    step_flocking,
    step_swarm,
    time_to_impact,
    update_turret_aim,
)
from beam.schemas import Drone, SwarmSpec, Turret, ThermalConfig, Vec2
from beam.util import make_rng, vdist


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


def _raw_kinematics() -> dict:
    raw = yaml.safe_load((config_dir() / "defaults.yaml").open(encoding="utf-8"))
    return raw["kinematics"]


@pytest.fixture()
def kin() -> KinematicsConfig:
    return KinematicsConfig.from_raw(_raw_kinematics())


def _drone(did: str, pos: Vec2, vel: Vec2, hardness: float = 40.0, value: float = 2000.0) -> Drone:
    return Drone(id=did, pos=pos, vel=vel, value=value, hardness=hardness, class_name="quad_small")


def _turret(aim: float = 0.0, slew_rate: float = 1.2) -> Turret:
    return Turret(
        id="t1",
        pos=Vec2(x=0.0, y=0.0),
        aim=aim,
        slew_rate=slew_rate,
        settle_time=0.1,
        power=100.0,
        range_max=5000.0,
        thermal=0.0,
        thermal_cfg=ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0),
    )


# --------------------------------------------------------------------------- #
# Config wiring                                                               #
# --------------------------------------------------------------------------- #


def test_kinematics_config_loads_from_defaults(kin: KinematicsConfig) -> None:
    assert kin.flocking.separation_radius > 0
    assert kin.flocking.neighbor_radius >= kin.flocking.separation_radius
    assert kin.staggered.wave_count >= 1
    # load_config tolerates the extra top-level kinematics block.
    cfg = load_config()
    assert cfg.sim.seed == 1337


# --------------------------------------------------------------------------- #
# Direct behavior                                                            #
# --------------------------------------------------------------------------- #


def test_direct_drone_reaches_asset_in_expected_time() -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 100.0
    start = Vec2(x=1000.0, y=0.0)  # straight line, distance 1000
    d = _drone("d0", start, initial_velocity(start, asset, speed))

    expected_time = 1000.0 / speed  # 10.0 s
    dt = 0.1
    steps = int(round(expected_time / dt))
    for _ in range(steps):
        step_direct([d], asset, dt)

    # After exactly distance/speed seconds it should be essentially at the asset.
    assert vdist(d.pos, asset) == pytest.approx(0.0, abs=1e-6)


def test_direct_drone_moves_straight_toward_asset_off_axis() -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 50.0
    start = Vec2(x=300.0, y=400.0)  # distance 500
    d = _drone("d0", start, initial_velocity(start, asset, speed))
    dt = 0.5
    # Each step should reduce distance by ~speed*dt and stay on the start->asset line.
    prev = vdist(d.pos, asset)
    for _ in range(5):
        step_direct([d], asset, dt)
        cur = vdist(d.pos, asset)
        assert cur < prev
        # Collinearity: cross product of pos with start direction stays ~0.
        cross = d.pos.x * start.y - d.pos.y * start.x
        assert cross == pytest.approx(0.0, abs=1e-6)
        prev = cur


def test_dead_drones_do_not_move() -> None:
    asset = Vec2(x=0.0, y=0.0)
    d = _drone("d0", Vec2(x=500.0, y=0.0), Vec2(x=-100.0, y=0.0))
    d.state = "dead"
    before = (d.pos.x, d.pos.y)
    step_direct([d], asset, 1.0)
    assert (d.pos.x, d.pos.y) == before


# --------------------------------------------------------------------------- #
# Time-to-impact                                                             #
# --------------------------------------------------------------------------- #


def test_tti_decreases_as_drone_approaches() -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 80.0
    start = Vec2(x=2000.0, y=0.0)
    d = _drone("d0", start, initial_velocity(start, asset, speed))

    ttis = [time_to_impact(d, asset)]
    dt = 0.5
    for _ in range(10):
        step_direct([d], asset, dt)
        ttis.append(time_to_impact(d, asset))

    # Strictly decreasing while approaching.
    for a, b in zip(ttis, ttis[1:]):
        assert b < a
    # And each step shrinks TTI by ~dt for constant-speed straight-line motion.
    assert ttis[0] - ttis[1] == pytest.approx(dt, abs=1e-6)


def test_tti_matches_distance_over_speed() -> None:
    asset = Vec2(x=0.0, y=0.0)
    d = _drone("d0", Vec2(x=600.0, y=800.0), Vec2(x=0.0, y=0.0))
    # Stationary -> never impacts.
    assert math.isinf(time_to_impact(d, asset))
    # Moving straight in: distance 1000, speed 100 -> 10s.
    d.vel = initial_velocity(d.pos, asset, 100.0)
    assert time_to_impact(d, asset) == pytest.approx(10.0, rel=1e-9)


def test_tti_infinite_when_moving_away() -> None:
    asset = Vec2(x=0.0, y=0.0)
    d = _drone("d0", Vec2(x=100.0, y=0.0), Vec2(x=50.0, y=0.0))  # heading away
    assert math.isinf(time_to_impact(d, asset))


def test_tti_zero_at_asset() -> None:
    asset = Vec2(x=0.0, y=0.0)
    d = _drone("d0", Vec2(x=0.0, y=0.0), Vec2(x=-10.0, y=0.0))
    assert time_to_impact(d, asset) == 0.0


# --------------------------------------------------------------------------- #
# Flocking                                                                   #
# --------------------------------------------------------------------------- #


def _make_flock(
    n: int,
    asset: Vec2,
    speed: float,
    seed: int,
    *,
    arc: tuple[float, float] = (0.0, 90.0),
) -> list[Drone]:
    # Use a partial arc by default so the swarm is not perfectly radially symmetric
    # (a full 360 ring with identical speed and zero jitter is a degenerate fixed
    # point where every drone moves identically). A partial arc exercises the real
    # neighbor interactions.
    spec = SwarmSpec(
        count=n,
        behavior="flocking",
        spawn_radius=2000.0,
        spawn_arc_deg=arc,
        speed=speed,
    )
    assignments = [("quad_small", 40.0, 2000.0)] * n
    return spawn_swarm(spec, asset, assignments, make_rng(seed))


def test_flocking_stays_bounded(kin: KinematicsConfig) -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 60.0
    drones = _make_flock(20, asset, speed, seed=1337)
    rng = make_rng(1337)

    spawn_radius = 2000.0
    arrival = 30.0  # engine marks a drone leaked once it reaches the asset
    max_radius = 0.0
    dt = 0.5
    total = 400
    for _ in range(total):
        step_flocking(drones, asset, dt, kin.flocking, rng, cruise_speed=speed)
        for d in drones:
            r = vdist(d.pos, asset)
            assert math.isfinite(d.pos.x) and math.isfinite(d.pos.y)
            max_radius = max(max_radius, r)
            # Mirror the engine: a drone that reaches the asset is frozen (leaked).
            if r <= arrival and d.state == "alive":
                d.state = "leaked"

    # Bounded: steering is force-clamped, speed is capped at cruise, and goal-seek is a
    # proper Reynolds seek (it decelerates + turns a fleeing drone around). The swarm
    # never escapes beyond its spawn ring.
    assert max_radius <= spawn_radius + 1e-6
    # The swarm closes in rather than orbiting forever: most drones reach the asset.
    leaked = sum(1 for d in drones if d.state == "leaked")
    assert leaked >= len(drones) // 2


def test_flocking_speed_capped_at_cruise(kin: KinematicsConfig) -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 60.0
    drones = _make_flock(12, asset, speed, seed=42)
    rng = make_rng(42)
    for _ in range(30):
        step_flocking(drones, asset, dt=0.5, cfg=kin.flocking, rng=rng, cruise_speed=speed)
        for d in drones:
            s = math.hypot(d.vel.x, d.vel.y)
            # Never exceeds the cruise speed (it may dip below while turning).
            assert s <= speed + 1e-6


def test_flocking_advances_toward_asset(kin: KinematicsConfig) -> None:
    asset = Vec2(x=0.0, y=0.0)
    speed = 60.0
    drones = _make_flock(15, asset, speed, seed=7)
    rng = make_rng(7)
    mean_r0 = sum(vdist(d.pos, asset) for d in drones) / len(drones)
    for _ in range(100):
        step_flocking(drones, asset, 0.5, kin.flocking, rng, cruise_speed=speed)
    mean_r1 = sum(vdist(d.pos, asset) for d in drones) / len(drones)
    assert mean_r1 < mean_r0  # goal-seek makes the swarm close in on average


# --------------------------------------------------------------------------- #
# Determinism                                                                #
# --------------------------------------------------------------------------- #


def _run_flock_trajectory(seed: int, kin: KinematicsConfig, steps: int = 50) -> list[tuple[float, float]]:
    asset = Vec2(x=0.0, y=0.0)
    drones = _make_flock(16, asset, 60.0, seed=seed)
    rng = make_rng(seed)
    for _ in range(steps):
        step_swarm(drones, asset, 0.5, "flocking", kin, rng, cruise_speed=60.0)
    return [(d.pos.x, d.pos.y) for d in drones]


def test_determinism_same_seed_identical_trajectory(kin: KinematicsConfig) -> None:
    a = _run_flock_trajectory(2024, kin)
    b = _run_flock_trajectory(2024, kin)
    assert a == b  # exact bit-for-bit equality under the same seed


def test_determinism_spawn_same_seed_identical(kin: KinematicsConfig) -> None:
    asset = Vec2(x=0.0, y=0.0)
    spec = SwarmSpec(
        count=24,
        behavior="staggered",
        spawn_radius=4000.0,
        spawn_arc_deg=(0.0, 360.0),
        speed=60.0,
    )
    assignments = [("quad_small", 40.0, 2000.0)] * 24
    s1 = spawn_swarm(spec, asset, assignments, make_rng(99), kin=kin)
    s2 = spawn_swarm(spec, asset, assignments, make_rng(99), kin=kin)
    assert [(d.pos.x, d.pos.y) for d in s1] == [(d.pos.x, d.pos.y) for d in s2]


def test_determinism_different_seed_differs() -> None:
    kin = KinematicsConfig.from_raw(_raw_kinematics())
    a = _run_flock_trajectory(1, kin)
    b = _run_flock_trajectory(2, kin)
    # Jitter defaults to 0 so flocking alone is seed-independent in motion; the spawn
    # ring is deterministic regardless of seed for a full circle. We assert the
    # *with-jitter* case differs to prove RNG actually drives variation.
    kin_j = KinematicsConfig.from_raw(_raw_kinematics())
    kin_j.flocking.jitter = 0.05
    aj = _run_flock_trajectory(1, kin_j)
    bj = _run_flock_trajectory(2, kin_j)
    assert aj != bj


# --------------------------------------------------------------------------- #
# Staggered waves                                                            #
# --------------------------------------------------------------------------- #


def test_staggered_release_schedule(kin: KinematicsConfig) -> None:
    count = 24
    cfg = kin.staggered
    times = [staggered_release_time(i, count, cfg) for i in range(count)]
    # Release times are non-decreasing in index and span exactly wave_count waves.
    assert times == sorted(times)
    distinct = sorted(set(times))
    assert len(distinct) == cfg.wave_count
    assert distinct[0] == 0.0
    assert distinct[1] == pytest.approx(cfg.wave_interval)


def test_staggered_moves_straight(kin: KinematicsConfig) -> None:
    asset = Vec2(x=0.0, y=0.0)
    start = Vec2(x=500.0, y=0.0)
    d = _drone("d0", start, initial_velocity(start, asset, 50.0))
    step_swarm([d], asset, 0.5, "staggered", kin, make_rng(1))
    # Straight-line: moved inward by speed*dt = 25.
    assert vdist(d.pos, asset) == pytest.approx(475.0, abs=1e-6)


# --------------------------------------------------------------------------- #
# Turret aim                                                                 #
# --------------------------------------------------------------------------- #


def test_slew_aim_bounded_by_rate() -> None:
    # Need to rotate by pi, slew_rate 1.0 rad/s, dt 0.1 -> at most 0.1 rad per step.
    aim = 0.0
    new = slew_aim(aim, math.pi, slew_rate=1.0, dt=0.1)
    assert abs(new - aim) == pytest.approx(0.1, abs=1e-9)


def test_slew_aim_snaps_when_within_step() -> None:
    new = slew_aim(0.0, 0.05, slew_rate=1.0, dt=0.1)  # step cap 0.1 > 0.05
    assert new == pytest.approx(0.05, abs=1e-12)


def test_slew_aim_takes_shortest_path() -> None:
    # From +3.0 rad, target -3.0 rad: shortest path wraps through pi, not back through 0.
    new = slew_aim(3.0, -3.0, slew_rate=10.0, dt=1.0)  # huge step -> snaps to target
    assert new == pytest.approx(-3.0, abs=1e-9)
    # Small step in the wrap direction increases magnitude past pi (toward +pi side).
    step = slew_aim(3.0, -3.0, slew_rate=0.05, dt=1.0)
    # Moving the short way (delta ~ +0.283) -> aim increases toward pi.
    assert step > 3.0


def test_update_turret_aim_reaches_target() -> None:
    t = _turret(aim=0.0, slew_rate=10.0)
    target = Vec2(x=0.0, y=100.0)  # heading pi/2
    desired = aim_at(t, target)
    reached = update_turret_aim(t, desired, dt=1.0)  # big step -> reaches
    assert reached
    assert t.aim == pytest.approx(math.pi / 2, abs=1e-9)


def test_update_turret_aim_still_slewing() -> None:
    t = _turret(aim=0.0, slew_rate=0.1)
    desired = math.pi  # far away
    reached = update_turret_aim(t, desired, dt=0.1)
    assert not reached
    assert t.aim == pytest.approx(0.01, abs=1e-9)
