"""REST request/response envelopes for the BEAM API (pdd.md section 12.1).

These models are *transport envelopes* for the REST surface only. They never redefine
any contract model from :mod:`beam.schemas` (domain entities, telemetry/control wire
models) - those are imported and reused verbatim. A scenario create request, for
example, carries a raw scenario dict (validated by the engine's ``build_scenario`` /
``load_config`` path) plus the identifiers the REST layer needs to track runs/batches.

All models forbid extra fields so malformed payloads are rejected at the boundary,
mirroring the contract's ``extra="forbid"`` convention.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from beam.schemas import RunSummary, SCHEMA_VERSION

__all__ = [
    "ScenarioCreateRequest",
    "ScenarioCreateResponse",
    "ScenarioGetResponse",
    "RunStartRequest",
    "RunStartResponse",
    "RunControlResponse",
    "RunSummaryResponse",
    "BatchStartRequest",
    "BatchStartResponse",
    "BatchResultsResponse",
    "SolverMeta",
    "SolversListResponse",
    "WeatherMeta",
    "WeatherListResponse",
    "ParetoStartRequest",
    "ParetoPointResponse",
    "ParetoStartResponse",
    "ParetoResultsResponse",
]


# --------------------------------------------------------------------------- #
# Scenario                                                                     #
# --------------------------------------------------------------------------- #


class ScenarioCreateRequest(BaseModel):
    """Create/validate a scenario (pdd.md 12.1 ``POST /api/scenario``).

    Either reference a built-in preset by ``preset`` (e.g. ``"swarm_24"``) and/or
    supply a raw ``scenario`` overlay dict (same shape as ``config/scenarios/*.yaml``).
    When both are given, the overlay is merged on top of the preset. At least one must
    be present.
    """

    model_config = ConfigDict(extra="forbid")

    preset: Optional[str] = None
    scenario: Optional[dict[str, Any]] = None


class ScenarioCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str


class ScenarioGetResponse(BaseModel):
    """The resolved, validated scenario config (raw overlay as stored)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    scenario: dict[str, Any]


# --------------------------------------------------------------------------- #
# Run                                                                          #
# --------------------------------------------------------------------------- #


class RunStartRequest(BaseModel):
    """Start a run (pdd.md 12.1 ``POST /api/run``).

    ``solver`` selects the active policy applied to the battery; the full registered
    suite is still evaluated each epoch for the race unless ``enabled_solvers`` narrows
    it. ``seed`` overrides the scenario seed for this run.
    """

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    solver: Optional[str] = None
    seed: Optional[int] = None
    enabled_solvers: Optional[list[str]] = None


class RunStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    active_solver: str
    enabled_solvers: list[str]


class RunControlResponse(BaseModel):
    """Acknowledgement of a control action (pdd.md 12.1/12.3)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: str
    status: str  # current run status after applying the action
    active_solver: str
    speed: float


class RunSummaryResponse(BaseModel):
    """Final run metrics + artifact links (pdd.md 12.1 ``GET /api/run/{id}/summary``)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    summary: Optional[RunSummary] = None
    telemetry_hash: Optional[str] = None
    artifacts: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Batch                                                                        #
# --------------------------------------------------------------------------- #


class BatchStartRequest(BaseModel):
    """Start a batch sweep (pdd.md 12.1 ``POST /api/batch``).

    Either name a built-in ``sweep`` spec (``config/sweeps/<name>.yaml``) or supply a
    raw ``sweep_spec`` dict of the same shape. ``scenario_id`` optionally overrides the
    sweep's ``base_scenario``.
    """

    model_config = ConfigDict(extra="forbid")

    sweep: Optional[str] = None
    sweep_spec: Optional[dict[str, Any]] = None
    scenario_id: Optional[str] = None


class BatchStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    status: str


class BatchResultsResponse(BaseModel):
    """Breakeven + gap-vs-scale series (pdd.md 12.1 ``GET /api/batch/{id}/results``)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str
    status: str
    parameter: str
    values: list[float] = Field(default_factory=list)
    # series name -> per-point payloads (e.g. net by solver, gap by solver).
    series: dict[str, Any] = Field(default_factory=dict)
    breakeven_crossover: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Catalog endpoints                                                            #
# --------------------------------------------------------------------------- #


class SolverMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    is_reference: bool = False


class SolversListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    reference: str
    solvers: list[SolverMeta] = Field(default_factory=list)


class WeatherMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    alpha: float


class WeatherListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[WeatherMeta] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Pareto                                                                       #
# --------------------------------------------------------------------------- #


class ParetoStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: Optional[str] = None
    preset: Optional[str] = None
    seed: int = 1337
    n_points: int = Field(default=20, ge=2, le=100)
    solver: str = "cp_sat"


class ParetoPointResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lam: float
    value_saved: float
    total_cost: float
    kills: int
    leaks: int


class ParetoStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pareto_id: str
    status: str


class ParetoResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pareto_id: str
    status: str
    points: list[ParetoPointResponse] = Field(default_factory=list)
    error: Optional[str] = None
