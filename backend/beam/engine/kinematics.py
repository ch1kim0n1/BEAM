"""Drone motion integrators and turret aim kinematics (pdd.md section 8.1).

2D plane, top-down (z carried implicitly as 0, unused in v1). The engine advances
entities by a fixed ``dt`` each tick; this module owns *how* drones move and how a
turret's aim vector slews toward a desired heading.

Three swarm behaviors (pdd.md section 8.1):

- ``direct``    : straight-line toward the asset at constant speed.
- ``flocking``  : boids separation / alignment / cohesion + goal-seek (Reynolds 1987).
- ``staggered`` : drones spawn in waves on a schedule from configurable arcs.

Hard rules honored here:

- NO hardcoded physics constants. Every tunable comes from ``config/defaults.yaml``
  (``kinematics:`` block) via :class:`KinematicsConfig`. Speeds / spawn geometry come
  from the :class:`~beam.schemas.SwarmSpec`.
- Determinism: all randomness (spawn jitter, optional flocking jitter) is drawn from a
  single seeded ``numpy`` generator created with :func:`beam.util.make_rng`. Iteration
  order over drones is fixed (list order). No hidden global RNG.
- This module reads/writes engine entity objects (:class:`~beam.schemas.Drone`,
  :class:`~beam.schemas.Turret`); it never touches the solver-facing ``WorldState``.

The flocking math is vectorized with numpy for the O(n^2) neighbor pass.
"""

from __future__ import annotations

import math
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel, ConfigDict

from beam.schemas import Drone, SwarmSpec, Turret, Vec2
from beam.util import (
    TWO_PI,
    angular_distance,
    heading_to,
    vsub,
    vunit,
    wrap_angle,
)


# --------------------------------------------------------------------------- #
# Config sub-models (mirror config/defaults.yaml -> kinematics:)              #
# --------------------------------------------------------------------------- #


class FlockingConfig(BaseModel):
    """Boids steering weights and neighborhood radii (Reynolds 1987).

    Values are illustrative and live in ``config/defaults.yaml``; nothing here is
    hard-coded in code.
    """

    model_config = ConfigDict(extra="forbid")

    separation_weight: float
    alignment_weight: float
    cohesion_weight: float
    goal_weight: float
    separation_radius: float
    neighbor_radius: float
    max_force: float
    jitter: float = 0.0  # optional per-step heading noise std (rad)


class StaggeredConfig(BaseModel):
    """Wave-release schedule for the ``staggered`` behavior."""

    model_config = ConfigDict(extra="forbid")

    wave_count: int
    wave_interval: float  # seconds between wave releases
    arc_jitter_deg: float = 0.0  # +/- spawn-angle jitter per drone


class KinematicsConfig(BaseModel):
    """Top-level kinematics tunables (``kinematics:`` block of defaults.yaml)."""

    model_config = ConfigDict(extra="forbid")

    flocking: FlockingConfig
    staggered: StaggeredConfig

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "KinematicsConfig":
        """Build from the raw ``kinematics`` sub-dict of a loaded config YAML.

        The scaffold ``BeamConfig`` does not carry this block (its sub-models forbid
        extra keys), so the engine passes the raw ``kinematics`` mapping straight in.
        """
        return cls(**raw)


# --------------------------------------------------------------------------- #
# Spawning (direct / flocking use a ring; staggered uses scheduled waves)      #
# --------------------------------------------------------------------------- #


def _arc_radians(spawn_arc_deg: tuple[float, float]) -> tuple[float, float]:
    lo, hi = spawn_arc_deg
    return math.radians(lo), math.radians(hi)


def initial_velocity(pos: Vec2, asset_pos: Vec2, speed: float) -> Vec2:
    """Velocity vector of magnitude ``speed`` pointing from ``pos`` to ``asset_pos``.

    Returns the zero vector if already at the asset (degenerate)."""
    direction = vunit(vsub(asset_pos, pos))
    return Vec2(x=direction.x * speed, y=direction.y * speed)


def _spawn_one(
    asset_pos: Vec2,
    spawn_radius: float,
    angle: float,
    speed: float,
) -> tuple[Vec2, Vec2]:
    """Position on the spawn ring at ``angle`` and an inward initial velocity."""
    pos = Vec2(
        x=asset_pos.x + spawn_radius * math.cos(angle),
        y=asset_pos.y + spawn_radius * math.sin(angle),
    )
    vel = initial_velocity(pos, asset_pos, speed)
    return pos, vel


def spawn_swarm(
    spec: SwarmSpec,
    asset_pos: Vec2,
    class_assignments: list[tuple[str, float, float]],
    rng: np.random.Generator,
    *,
    kin: Optional[KinematicsConfig] = None,
) -> list[Drone]:
    """Create the swarm's drones on the spawn ring with inward initial velocities.

    Args:
        spec: swarm spawn geometry / behavior / speed.
        asset_pos: the defended point (spawn arc is measured around it).
        class_assignments: per-drone ``(class_name, hardness, value)`` triples,
            resolved by the engine from ``spec.class_mix`` and the drone-class config.
            Its length defines how many drones are created (must equal ``spec.count``).
        rng: the single seeded run RNG (used only for arc jitter on ``staggered``).
        kin: kinematics config (only consulted for ``staggered`` arc jitter).

    Returns:
        Drones in deterministic order, evenly distributed across the spawn arc. For
        ``staggered`` behavior, drones spawn at the same geometry but the engine
        releases them per :func:`staggered_release_time`; they are returned in
        wave-major order so wave grouping is stable.

    Note:
        The drone ``state`` is left at its default ``"alive"``. For staggered runs the
        engine should treat a drone as dormant (not yet integrated) until its release
        time; this function does not stamp release times onto the model (the contract
        ``Drone`` has no such field) — use :func:`staggered_release_time` per index.
    """
    n = len(class_assignments)
    if n == 0:
        return []

    arc_lo, arc_hi = _arc_radians(spec.spawn_arc_deg)
    span = arc_hi - arc_lo
    # Evenly place n drones across the arc. A full 360 arc must not double-place the
    # endpoint, so use n slots; a partial arc spans endpoints inclusively.
    full_circle = abs((span % TWO_PI)) < 1e-9 and abs(span) > 1e-9
    drones: list[Drone] = []

    jitter_rad = 0.0
    if spec.behavior == "staggered" and kin is not None:
        jitter_rad = math.radians(kin.staggered.arc_jitter_deg)

    for i in range(n):
        if n == 1:
            frac = 0.5
        elif full_circle:
            frac = i / n
        else:
            frac = i / (n - 1)
        angle = arc_lo + frac * span
        if jitter_rad > 0.0:
            # Symmetric jitter in [-jitter_rad, +jitter_rad]; RNG-driven, deterministic.
            angle += float(rng.uniform(-jitter_rad, jitter_rad))
        pos, vel = _spawn_one(asset_pos, spec.spawn_radius, angle, spec.speed)
        class_name, hardness, value = class_assignments[i]
        drones.append(
            Drone(
                id=f"d{i}",
                pos=pos,
                vel=vel,
                value=value,
                hardness=hardness,
                class_name=class_name,
            )
        )
    return drones


def staggered_release_time(index: int, count: int, cfg: StaggeredConfig) -> float:
    """Sim-time (s) at which drone ``index`` is released, for ``staggered`` behavior.

    Drones are split into ``wave_count`` contiguous, near-equal waves (by index); wave
    ``w`` releases at ``w * wave_interval``. Deterministic, no RNG.
    """
    waves = max(1, cfg.wave_count)
    count = max(1, count)
    per_wave = math.ceil(count / waves)
    wave = min(index // per_wave, waves - 1)
    return wave * cfg.wave_interval


# --------------------------------------------------------------------------- #
# Per-step integration                                                         #
# --------------------------------------------------------------------------- #


def _is_movable(d: Drone) -> bool:
    """Only live/engaged drones are integrated; dead/leaked are frozen."""
    return d.state in ("alive", "engaged")


def step_direct(drones: list[Drone], asset_pos: Vec2, dt: float) -> None:
    """Advance ``direct`` drones straight toward the asset at their current speed.

    Velocity is re-pointed at the asset each tick (constant speed, straight line);
    position integrates with explicit Euler. Mutates drones in place.
    """
    for d in drones:
        if not _is_movable(d):
            continue
        speed = math.hypot(d.vel.x, d.vel.y)
        direction = vunit(vsub(asset_pos, d.pos))
        d.vel = Vec2(x=direction.x * speed, y=direction.y * speed)
        d.pos = Vec2(x=d.pos.x + d.vel.x * dt, y=d.pos.y + d.vel.y * dt)


def step_flocking(
    drones: list[Drone],
    asset_pos: Vec2,
    dt: float,
    cfg: FlockingConfig,
    rng: Optional[np.random.Generator] = None,
    *,
    cruise_speed: Optional[float] = None,
) -> None:
    """Advance ``flocking`` drones with boids steering + goal-seek (Reynolds 1987).

    Three local rules (separation, alignment, cohesion) over neighbors within the
    configured radii, blended with a Reynolds *seek* toward the asset (desired velocity
    = cruise toward the asset, steering = desired - current). The combined steering
    acceleration is clamped to ``cfg.max_force`` and integrated; the resulting speed is
    capped at ``cruise_speed`` (so goal-seek can decelerate and turn a fleeing drone
    around — a pure constant-speed re-normalization would let the swarm escape to
    infinity after overshooting the asset). Mutates drones in place.

    Args:
        cruise_speed: the swarm's nominal speed (``SwarmSpec.speed``). When ``None`` it
            is taken per-drone from the current velocity magnitude (a stable reference
            only while drones cruise). The engine should pass ``spec.speed`` explicitly.

    Vectorized with numpy. Iteration is over the fixed drone list order; the optional
    ``rng`` adds deterministic heading jitter when ``cfg.jitter > 0``.
    """
    movable = [d for d in drones if _is_movable(d)]
    m = len(movable)
    if m == 0:
        return

    pos = np.array([[d.pos.x, d.pos.y] for d in movable], dtype=np.float64)
    vel = np.array([[d.vel.x, d.vel.y] for d in movable], dtype=np.float64)
    if cruise_speed is not None:
        max_speed = np.full(m, float(cruise_speed))
    else:
        max_speed = np.hypot(vel[:, 0], vel[:, 1])  # fall back to current speed

    # Pairwise displacement r_ij = pos_j - pos_i  (shape m,m,2) and distances.
    diff = pos[None, :, :] - pos[:, None, :]
    dist = np.sqrt(np.sum(diff * diff, axis=2))
    np.fill_diagonal(dist, np.inf)  # exclude self

    sep_mask = dist < cfg.separation_radius
    nbr_mask = dist < cfg.neighbor_radius

    # Separation: steer away from close neighbors, weighted by 1/dist (stronger when
    # closer). Sum of -unit(r_ij)/dist over separation neighbors.
    safe = np.where(np.isfinite(dist) & (dist > 0), dist, np.inf)
    inv = np.where(sep_mask, 1.0 / safe, 0.0)  # 1/dist for close neighbors
    # away direction = -r_ij / dist  -> -diff / dist; scale by inv (=> /dist^2 overall)
    away = -diff / safe[:, :, None]
    separation = np.sum(away * inv[:, :, None], axis=1)

    # Alignment: match average velocity of neighbors within neighbor_radius.
    nbr_count = np.sum(nbr_mask, axis=1)
    has_nbr = nbr_count > 0
    avg_vel = np.zeros_like(vel)
    avg_vel[has_nbr] = (
        np.einsum("ij,jk->ik", nbr_mask.astype(np.float64), vel)[has_nbr]
        / nbr_count[has_nbr, None]
    )
    alignment = np.zeros_like(vel)
    alignment[has_nbr] = avg_vel[has_nbr] - vel[has_nbr]

    # Cohesion: steer toward centroid of neighbors.
    centroid = np.zeros_like(pos)
    centroid[has_nbr] = (
        np.einsum("ij,jk->ik", nbr_mask.astype(np.float64), pos)[has_nbr]
        / nbr_count[has_nbr, None]
    )
    cohesion = np.zeros_like(pos)
    cohesion[has_nbr] = centroid[has_nbr] - pos[has_nbr]

    # Goal-seek toward the asset, Reynolds "seek": steer = desired_velocity - velocity,
    # where desired_velocity points at the asset at cruise speed. Unlike a unit pull,
    # this opposes outward velocity directly, so a drone that overshoots the asset is
    # decelerated and turned back instead of escaping.
    goal_vec = np.array([asset_pos.x, asset_pos.y], dtype=np.float64)[None, :] - pos
    goal_unit = _normalize_rows(goal_vec)
    desired = goal_unit * max_speed[:, None]
    goal_steer = _normalize_rows(desired - vel)

    steer = (
        cfg.separation_weight * _normalize_rows(separation)
        + cfg.alignment_weight * _normalize_rows(alignment)
        + cfg.cohesion_weight * _normalize_rows(cohesion)
        + cfg.goal_weight * goal_steer
    )

    # Clamp steering magnitude to max_force.
    steer = _clamp_rows(steer, cfg.max_force)

    new_vel = vel + steer * dt

    # Optional deterministic heading jitter.
    if cfg.jitter > 0.0 and rng is not None:
        angles = rng.normal(0.0, cfg.jitter, size=m)
        cos_a = np.cos(angles)
        sin_a = np.sin(angles)
        vx = new_vel[:, 0] * cos_a - new_vel[:, 1] * sin_a
        vy = new_vel[:, 0] * sin_a + new_vel[:, 1] * cos_a
        new_vel = np.stack([vx, vy], axis=1)

    # Cap speed at the cruise speed (allow slower while turning; never faster).
    nv_norm = np.hypot(new_vel[:, 0], new_vel[:, 1])
    over = nv_norm > max_speed
    scale = np.ones(m)
    nz_over = over & (nv_norm > 1e-12)
    scale[nz_over] = max_speed[nz_over] / nv_norm[nz_over]
    new_vel = new_vel * scale[:, None]

    new_pos = pos + new_vel * dt

    for k, d in enumerate(movable):
        d.vel = Vec2(x=float(new_vel[k, 0]), y=float(new_vel[k, 1]))
        d.pos = Vec2(x=float(new_pos[k, 0]), y=float(new_pos[k, 1]))


def _normalize_rows(v: np.ndarray) -> np.ndarray:
    """Row-wise unit vectors; zero rows stay zero."""
    n = np.hypot(v[:, 0], v[:, 1])
    out = np.zeros_like(v)
    nz = n > 1e-12
    out[nz] = v[nz] / n[nz, None]
    return out


def _clamp_rows(v: np.ndarray, max_mag: float) -> np.ndarray:
    """Clamp each row's magnitude to ``max_mag`` (no-op for already-shorter rows)."""
    n = np.hypot(v[:, 0], v[:, 1])
    out = v.copy()
    over = n > max_mag
    out[over] = v[over] * (max_mag / n[over, None])
    return out


def step_swarm(
    drones: list[Drone],
    asset_pos: Vec2,
    dt: float,
    behavior: str,
    kin: KinematicsConfig,
    rng: Optional[np.random.Generator] = None,
    *,
    cruise_speed: Optional[float] = None,
) -> None:
    """Dispatch one integration step for ``drones`` by behavior profile.

    ``staggered`` drones move straight (like ``direct``) once released; their wave
    schedule is enforced by the engine via :func:`staggered_release_time` (which gates
    whether a drone is movable this tick), so the per-step motion is straight-line.

    ``cruise_speed`` (``SwarmSpec.speed``) caps flocking speed; the engine should pass
    it so goal-seek can decelerate and turn drones around rather than letting them
    escape after overshooting the asset.
    """
    if behavior == "direct" or behavior == "staggered":
        step_direct(drones, asset_pos, dt)
    elif behavior == "flocking":
        step_flocking(drones, asset_pos, dt, kin.flocking, rng, cruise_speed=cruise_speed)
    else:  # pragma: no cover - guarded by the BehaviorProfile literal upstream
        raise ValueError(f"unknown behavior profile: {behavior!r}")


# --------------------------------------------------------------------------- #
# Time-to-impact (the target's hard deadline, pdd.md section 8 glossary)       #
# --------------------------------------------------------------------------- #


def time_to_impact(drone: Drone, asset_pos: Vec2) -> float:
    """Time (s) until ``drone`` reaches ``asset_pos`` (its hard deadline / TTI).

    Uses the closing speed = component of velocity along the line to the asset. If the
    drone is stationary or moving away (non-positive closing speed), TTI is ``+inf``
    (it never impacts under current motion). Already-at-asset returns ``0.0``.
    """
    to_asset = vsub(asset_pos, drone.pos)
    dist = math.hypot(to_asset.x, to_asset.y)
    if dist <= 1e-12:
        return 0.0
    # Closing speed: project velocity onto the unit vector toward the asset.
    ux, uy = to_asset.x / dist, to_asset.y / dist
    closing = drone.vel.x * ux + drone.vel.y * uy
    if closing <= 1e-12:
        return math.inf
    return dist / closing


# --------------------------------------------------------------------------- #
# Turret aim kinematics (stationary turret; only the aim vector slews)         #
# --------------------------------------------------------------------------- #


def slew_aim(current_aim: float, desired_aim: float, slew_rate: float, dt: float) -> float:
    """Rotate ``current_aim`` toward ``desired_aim``, bounded by ``slew_rate`` (rad/s).

    Takes the shortest angular path (pdd.md section 8.4). Returns the new aim wrapped
    to (-pi, pi]. Pure function; does not mutate.
    """
    delta = wrap_angle(desired_aim - current_aim)
    max_step = slew_rate * dt
    if abs(delta) <= max_step:
        return wrap_angle(desired_aim)
    return wrap_angle(current_aim + math.copysign(max_step, delta))


def update_turret_aim(turret: Turret, desired_aim: float, dt: float) -> bool:
    """Slew ``turret.aim`` toward ``desired_aim`` in place, bounded by its slew rate.

    Returns ``True`` once the aim has reached ``desired_aim`` (within this step's
    bound), else ``False`` (still slewing). Does not change ``turret.state`` — state
    transitions are the engine's responsibility.
    """
    new_aim = slew_aim(turret.aim, desired_aim, turret.slew_rate, dt)
    turret.aim = new_aim
    return angular_distance(new_aim, desired_aim) <= 1e-9


def aim_at(turret: Turret, target_pos: Vec2) -> float:
    """Desired aim heading (rad) for ``turret`` to point at ``target_pos``."""
    return heading_to(turret.pos, target_pos)


def slew_time(turret: Turret, target_aim: float) -> float:
    """Setup time (s) to slew this turret to ``target_aim`` (pdd.md section 8.4).

        s_i(a, b) = angular_distance(aim_a, aim_b) / slew_rate_i + settle_time_i
    """
    return angular_distance(turret.aim, target_aim) / turret.slew_rate + turret.settle_time
