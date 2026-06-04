"""BEAM shared data-model contract (pydantic v2).

This package is the single source of truth for every data structure that crosses
a module boundary: the domain entities (``Drone``, ``Turret``, ...), the solver
contract types (``WorldState``, ``Assignment``), the per-run records
(``EpochRecord``, ``RunSummary``), and the wire messages streamed to the frontend
(``FrameMessage``, ``EpochMessage``).

Mirrors ``pdd.md`` sections 12 (interface contracts) and 13 (data models).
Downstream agents import names from here; they must not redefine them.
"""

from __future__ import annotations

from beam.schemas.models import (
    SCHEMA_VERSION,
    Vec2,
    ThermalConfig,
    WeatherProfile,
    Drone,
    Turret,
    SwarmSpec,
    Scenario,
    WorldState,
    Assignment,
    SolverResult,
    LedgerSnapshot,
    EpochRecord,
    RunSummary,
    DroneState,
    TurretState,
    BehaviorProfile,
    # telemetry wire models (section 12.2)
    DroneFrame,
    TurretFrame,
    BeamFrame,
    FrameMessage,
    EpochSolverEntry,
    EpochLedger,
    EpochMessage,
    # control wire models (section 12.3)
    ControlMessage,
)

__all__ = [
    "SCHEMA_VERSION",
    "Vec2",
    "ThermalConfig",
    "WeatherProfile",
    "Drone",
    "Turret",
    "SwarmSpec",
    "Scenario",
    "WorldState",
    "Assignment",
    "SolverResult",
    "LedgerSnapshot",
    "EpochRecord",
    "RunSummary",
    "DroneState",
    "TurretState",
    "BehaviorProfile",
    "DroneFrame",
    "TurretFrame",
    "BeamFrame",
    "FrameMessage",
    "EpochSolverEntry",
    "EpochLedger",
    "EpochMessage",
    "ControlMessage",
]
