"""Configuration loading for BEAM.

Reads ``config/defaults.yaml`` (the full tunable tree, pdd.md section 18) and,
optionally, a scenario YAML, into typed pydantic objects. NOTHING physics- or
cost-related is hard-coded anywhere else in the codebase; every tunable flows from
here.

Typical use::

    cfg = load_config()                         # defaults only
    cfg = load_config(scenario="swarm_24")      # defaults + a scenario preset

The returned ``BeamConfig`` exposes the raw config groups plus helpers that resolve
a ``Scenario`` and per-class drone parameters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from beam.schemas import ThermalConfig, WeatherProfile


# --------------------------------------------------------------------------- #
# Config-tree typed sub-models (mirror pdd.md section 18 exactly)             #
# --------------------------------------------------------------------------- #


class TrackEfficiencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base: float
    range_falloff: float


class PhysicsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weather_profiles: dict[str, WeatherProfile]
    track_efficiency: TrackEfficiencyConfig


class TurretDefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    power: float
    slew_rate: float
    settle_time: float
    range_max: float
    thermal: ThermalConfig


class DroneClassConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hardness: float
    value: float


class CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_per_kwh: float
    system_capex: float
    expected_lifetime_engagements: float
    maintenance_rate: float


class SolverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cp_sat_time_budget_ms: int
    cp_sat_target_threshold: int
    metaheuristic: str  # "ga" or "sa"


class SimConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_period: float
    seed: int
    leak_radius: float  # distance from asset at/below which a drone leaks (pdd.md 8.6)
    max_epochs: int  # safety cap on epochs; natural stop is all drones dead/leaked


class BeamConfig(BaseModel):
    """The full, validated config tree (pdd.md section 18) plus any scenario overlay.

    ``weather_profiles`` are normalized so each profile's ``name`` matches its key.
    ``scenario`` holds the raw scenario YAML dict (or None); the engine resolves it
    into a ``Scenario`` once entities are constructed.
    """

    model_config = ConfigDict(extra="forbid")

    physics: PhysicsConfig
    turret_defaults: TurretDefaultsConfig
    drone_classes: dict[str, DroneClassConfig]
    cost: CostConfig
    solver: SolverConfig
    sim: SimConfig
    scenario: Optional[dict[str, Any]] = Field(default=None)

    def weather(self, name: str) -> WeatherProfile:
        """Resolve a weather profile by name (raises KeyError if unknown)."""
        return self.physics.weather_profiles[name]

    def drone_class(self, name: str) -> DroneClassConfig:
        """Resolve a drone class by name (raises KeyError if unknown)."""
        return self.drone_classes[name]


# --------------------------------------------------------------------------- #
# Path resolution + loading                                                   #
# --------------------------------------------------------------------------- #


def config_dir() -> Path:
    """Absolute path to the repo-root ``config/`` directory.

    Resolved relative to this file: ``backend/beam/config.py`` -> ``../../config``.
    """
    return (Path(__file__).resolve().parent.parent.parent / "config").resolve()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping at top level of {path}, got {type(data)}")
    return data


def _normalize_weather(raw: dict[str, Any]) -> dict[str, Any]:
    """Inject each weather profile's key as its ``name`` field if absent."""
    physics = raw.get("physics", {})
    profiles = physics.get("weather_profiles", {})
    for key, prof in profiles.items():
        if isinstance(prof, dict) and "name" not in prof:
            prof["name"] = key
    return raw


def load_config(
    scenario: Optional[str] = None,
    *,
    defaults_path: Optional[Path] = None,
    scenario_path: Optional[Path] = None,
) -> BeamConfig:
    """Load ``defaults.yaml`` and an optional scenario into a typed ``BeamConfig``.

    Args:
        scenario: name of a preset under ``config/scenarios/<name>.yaml``. Ignored if
            ``scenario_path`` is given.
        defaults_path: override path to the defaults file (default: config/defaults.yaml).
        scenario_path: explicit path to a scenario YAML (overrides ``scenario``).
    """
    cdir = config_dir()
    dpath = defaults_path or (cdir / "defaults.yaml")
    raw = _normalize_weather(_read_yaml(dpath))

    scn_raw: Optional[dict[str, Any]] = None
    spath = scenario_path
    if spath is None and scenario is not None:
        spath = cdir / "scenarios" / f"{scenario}.yaml"
    if spath is not None:
        scn_raw = _read_yaml(spath)

    return BeamConfig(
        physics=raw["physics"],
        turret_defaults=raw["turret_defaults"],
        drone_classes=raw["drone_classes"],
        cost=raw["cost"],
        solver=raw["solver"],
        sim=raw["sim"],
        scenario=scn_raw,
    )


def load_sweep(name: str, *, sweep_path: Optional[Path] = None) -> dict[str, Any]:
    """Load a batch sweep spec from ``config/sweeps/<name>.yaml`` (raw dict).

    The batch agent owns the typed sweep model; the scaffold only provides the reader.
    """
    spath = sweep_path or (config_dir() / "sweeps" / f"{name}.yaml")
    return _read_yaml(spath)
