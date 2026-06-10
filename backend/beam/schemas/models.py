"""BEAM pydantic v2 data models - the shared contract.

Source of truth: ``pdd.md`` sections 12 (interface contracts) and 13 (data models).

Conventions:
- All physics / cost numbers are illustrative and come from config, never hard-coded
  here. These models carry values; they do not define defaults for tunables.
- Determinism: containers preserve insertion order; never rely on dict/set ordering
  beyond what is explicitly recorded.
- ``SCHEMA_VERSION`` versions the wire protocol (telemetry + control). Bump it when a
  wire-facing model changes shape; mirrored client-side via zod.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Version of the telemetry / control wire protocol (pdd.md section 12.2 / 12.3).
SCHEMA_VERSION: str = "1.0"


# --------------------------------------------------------------------------- #
# Enumerated states (string literals - stable across the wire)                #
# --------------------------------------------------------------------------- #

DroneState = Literal["alive", "engaged", "dead", "leaked"]
TurretState = Literal["idle", "slewing", "firing", "cooldown"]
BehaviorProfile = Literal["direct", "flocking", "staggered"]


# --------------------------------------------------------------------------- #
# Primitive geometry                                                          #
# --------------------------------------------------------------------------- #


class Vec2(BaseModel):
    """2D vector. z is carried implicitly as 0 in v1 (3D-ready, unused)."""

    model_config = ConfigDict(extra="forbid")

    x: float = 0.0
    y: float = 0.0


# --------------------------------------------------------------------------- #
# Configuration-backed value objects                                          #
# --------------------------------------------------------------------------- #


class ThermalConfig(BaseModel):
    """Per-turret thermal parameters (pdd.md section 8.5). Values from config."""

    model_config = ConfigDict(extra="forbid")

    heat_rate: float  # heat units added per second while firing
    cool_rate: float  # heat units removed per second while idle (floored at 0)
    h_max: float  # cap; at/above this the turret is forced into COOLDOWN
    h_resume: float  # turret may resume firing once thermal drops to/below this


class WeatherProfile(BaseModel):
    """Atmospheric profile (pdd.md section 8.2). ``alpha`` is Beer-Lambert extinction."""

    model_config = ConfigDict(extra="forbid")

    name: str
    alpha: float  # extinction coefficient; clear < haze < rain < fog < dust


# --------------------------------------------------------------------------- #
# Domain entities (pdd.md section 13)                                          #
# --------------------------------------------------------------------------- #


class Drone(BaseModel):
    """A hostile target (pdd.md section 13)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pos: Vec2
    vel: Vec2
    value: float  # cost of the drone; also the leak penalty
    hardness: float  # energy-to-kill (E_kill)
    class_name: str  # drone class key, e.g. "quad_small"
    state: DroneState = "alive"
    energy_absorbed: float = 0.0  # cumulative delivered energy on this target


class Turret(BaseModel):
    """A single laser emitter - one beam (pdd.md section 13)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    pos: Vec2
    aim: float  # current aim heading, radians
    slew_rate: float  # rad/s
    settle_time: float  # seconds of settle after a slew
    power: float  # emitted power (illustrative units)
    range_max: float  # max effective range
    thermal: float  # current accumulated heat
    thermal_cfg: ThermalConfig
    state: TurretState = "idle"
    current_target: Optional[str] = None


class SwarmSpec(BaseModel):
    """Spawn geometry + behavior + class mix for a swarm (pdd.md section 13).

    ``class_mix`` maps drone-class key -> relative weight; the engine resolves it
    against ``count`` deterministically under the scenario seed.
    """

    model_config = ConfigDict(extra="forbid")

    count: int
    behavior: BehaviorProfile = "direct"
    class_mix: dict[str, float] = Field(default_factory=dict)
    spawn_radius: float = 0.0  # distance from asset at which drones appear
    spawn_arc_deg: tuple[float, float] = (0.0, 360.0)  # [start, end] degrees
    speed: float = 0.0  # nominal approach speed (units/s)


class Scenario(BaseModel):
    """A fully specified, reproducible engagement (pdd.md section 13)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    asset_pos: Vec2
    battery: list[Turret]
    swarm_spec: SwarmSpec
    weather: str  # key into the configured weather_profiles
    seed: int
    decision_period: float  # seconds of sim-time between decision epochs


# --------------------------------------------------------------------------- #
# Solver contract types (pdd.md section 9.1 / 12)                              #
# --------------------------------------------------------------------------- #


class WorldState(BaseModel):
    """Immutable snapshot handed to a solver each epoch (pdd.md sections 7.4, 9.1).

    A solver reads only this; it must not mutate engine state. ``weather_alpha`` is
    the resolved Beer-Lambert coefficient for the active weather profile, so solvers
    need no config access to reason about dwell-to-kill.
    """

    model_config = ConfigDict(extra="forbid")

    t: float  # current sim time (s)
    drones: list[Drone]  # live + detected targets only
    turrets: list[Turret]  # full battery, with current thermal/aim state
    weather_alpha: float  # resolved extinction coefficient for active weather
    asset_pos: Vec2  # the defended point


class Assignment(BaseModel):
    """A solver's output for one epoch (pdd.md sections 9.1, 13).

    ``turret_orders`` maps turret_id -> ordered list of target ids that turret should
    service, in firing order. A target id appears under at most one turret. Targets
    omitted everywhere are left un-engaged this epoch.
    """

    model_config = ConfigDict(extra="forbid")

    turret_orders: dict[str, list[str]] = Field(default_factory=dict)
    objective_estimate: float = 0.0  # solver's self-reported objective


class SolverResult(BaseModel):
    """One solver's evaluation on a single epoch snapshot (pdd.md sections 9.3, 12.2).

    ``gap`` is ``(obj_optimal - obj_policy) / max(obj_optimal, eps)`` vs the exact
    reference. For the reference itself, ``is_optimal``/``bound`` are set and ``gap``
    is None. When the reference only proved a bound, downstream marks the gap as
    bound-based via ``gap_is_bound_based``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    objective: float
    solve_ms: float
    assignment: Optional[Assignment] = None
    gap: Optional[float] = None
    is_optimal: Optional[bool] = None  # True only for a proven-optimal reference
    bound: Optional[float] = None  # best bound from the reference solver
    gap_is_bound_based: bool = False  # gap measured vs a bound, not a proven optimum


# --------------------------------------------------------------------------- #
# Ledger + run records (pdd.md sections 10, 13)                                #
# --------------------------------------------------------------------------- #


class LedgerSnapshot(BaseModel):
    """Cost ledger state at an epoch (pdd.md section 10). All inputs from config."""

    model_config = ConfigDict(extra="forbid")

    cumulative_cost: float = 0.0
    value_destroyed: float = 0.0
    net: float = 0.0  # value_destroyed - cumulative_cost
    shot_energy_cost: float = 0.0  # cumulative energy cost component
    maintenance_cost: float = 0.0  # cumulative maintenance component
    capex_amortized: float = 0.0  # cumulative amortized capex component
    engagements: int = 0


class EpochRecord(BaseModel):
    """The canonical per-epoch record (pdd.md section 13). Headless + replay source."""

    model_config = ConfigDict(extra="forbid")

    epoch: int
    t: float
    solver_results: list[SolverResult]
    active_solver: str
    ledger: LedgerSnapshot


class RunSummary(BaseModel):
    """Final metrics for a completed run (pdd.md section 13)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    kills: int
    leaks: int
    leaked_value: float
    final_ledger: LedgerSnapshot
    avg_gap_by_solver: dict[str, float] = Field(default_factory=dict)
    avg_solve_ms_by_solver: dict[str, float] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Telemetry wire models - server -> client (pdd.md section 12.2)               #
# --------------------------------------------------------------------------- #


class DroneFrame(BaseModel):
    """Per-drone render payload (pdd.md section 12.2 frame message)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    x: float
    y: float
    v: tuple[float, float]  # velocity [vx, vy]
    value: float
    hp_frac: float  # 1.0 - energy_absorbed / hardness, clamped
    state: DroneState
    tti: float  # time-to-impact (s)


class TurretFrame(BaseModel):
    """Per-turret render payload (pdd.md section 12.2 frame message)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    x: float
    y: float
    aim: float  # radians
    target: Optional[str] = None  # currently engaged target id, if any
    state: TurretState
    thermal_frac: float  # thermal / h_max, clamped


class BeamFrame(BaseModel):
    """An active beam being drawn this frame (pdd.md section 12.2)."""

    # "from" is a reserved word in Python; expose it on the wire via alias.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")  # source turret id
    to: str  # target drone id
    power_frac: float  # delivered/emitted fraction


class FrameMessage(BaseModel):
    """Render-cadence telemetry message (pdd.md section 12.2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["frame"] = "frame"
    schema_version: str = SCHEMA_VERSION
    t: float
    drones: list[DroneFrame] = Field(default_factory=list)
    turrets: list[TurretFrame] = Field(default_factory=list)
    beams: list[BeamFrame] = Field(default_factory=list)
    leaks: int = 0
    kills: int = 0


class EpochSolverEntry(BaseModel):
    """One solver's row in an epoch message (pdd.md section 12.2).

    Wire-shaped projection of ``SolverResult`` (no embedded assignment on the wire).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    objective: float
    solve_ms: float
    gap: Optional[float] = None
    is_optimal: Optional[bool] = None
    bound: Optional[float] = None
    gap_is_bound_based: bool = False


class EpochLedger(BaseModel):
    """Compact ledger block on the epoch message (pdd.md section 12.2)."""

    model_config = ConfigDict(extra="forbid")

    cumulative_cost: float
    value_destroyed: float
    net: float


class EpochMessage(BaseModel):
    """Decision-cadence telemetry message (pdd.md section 12.2)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["epoch"] = "epoch"
    schema_version: str = SCHEMA_VERSION
    t: float
    epoch: int
    solvers: list[EpochSolverEntry] = Field(default_factory=list)
    active_solver: str
    ledger: EpochLedger


# --------------------------------------------------------------------------- #
# Control wire model - client -> server (pdd.md section 12.3)                  #
# --------------------------------------------------------------------------- #


class ControlMessage(BaseModel):
    """Run control command (pdd.md section 12.3).

    Fields are optional and interpreted per ``action``:
      - set_solver -> ``solver``
      - set_speed  -> ``multiplier``
      - step       -> ``epochs``
      - pause/resume/stop -> no extra fields
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "step", "stop", "set_solver", "set_speed"]
    schema_version: str = SCHEMA_VERSION
    solver: Optional[str] = None
    multiplier: Optional[float] = None
    epochs: Optional[int] = None
