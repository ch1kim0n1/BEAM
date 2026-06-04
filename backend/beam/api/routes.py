"""REST routes for the BEAM API (pdd.md section 12.1).

Implements every endpoint in the contract:

    POST /api/scenario            create/validate a scenario -> scenario_id
    GET  /api/scenario/{id}       fetch a scenario config
    POST /api/run                 start a run {scenario_id, solver, seed} -> run_id
    POST /api/run/{id}/control    {action: pause|resume|step|stop|set_solver|set_speed}
    GET  /api/run/{id}/summary    final metrics + artifact links
    POST /api/batch               batch sweep {scenario_id, sweep_spec} -> batch_id
    GET  /api/batch/{id}/results  breakeven + gap-vs-scale series
    GET  /api/solvers             list available solvers + metadata
    GET  /api/weather             list weather profiles

Every payload is validated against pydantic models — request bodies via the
:mod:`beam.api.models` envelopes (which reuse, never redefine, the contract schemas),
and control messages via the contract's :class:`~beam.schemas.ControlMessage`. The
process-wide :class:`~beam.api.runtime.Registry` is read from ``request.app.state``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from beam.api.models import (
    BatchResultsResponse,
    BatchStartRequest,
    BatchStartResponse,
    RunControlResponse,
    RunStartRequest,
    RunStartResponse,
    RunSummaryResponse,
    ScenarioCreateRequest,
    ScenarioCreateResponse,
    ScenarioGetResponse,
    SolverMeta,
    SolversListResponse,
    WeatherListResponse,
    WeatherMeta,
)
from beam.api.runtime import (
    DEFAULT_REFERENCE_SOLVER,
    BatchJob,
    Registry,
    RunController,
    resolve_config,
    run_batch,
)
from beam.config import load_config, load_sweep
from beam.schemas import ControlMessage
from beam.solvers import REGISTRY, available_solvers

router = APIRouter(prefix="/api")


def _registry(request: Request) -> Registry:
    reg = getattr(request.app.state, "registry", None)
    if reg is None:  # pragma: no cover — app factory always installs it
        raise HTTPException(status_code=500, detail="registry not initialised")
    return reg


def _pick_active_solver(requested: str | None) -> str:
    """Resolve the active solver, defaulting to the reference if available."""
    if requested is not None:
        if requested not in REGISTRY:
            raise HTTPException(
                status_code=422,
                detail=f"unknown solver {requested!r}; available={available_solvers()}",
            )
        return requested
    if DEFAULT_REFERENCE_SOLVER in REGISTRY:
        return DEFAULT_REFERENCE_SOLVER
    return available_solvers()[0]


# --------------------------------------------------------------------------- #
# Scenario                                                                    #
# --------------------------------------------------------------------------- #


@router.post("/scenario", response_model=ScenarioCreateResponse)
def create_scenario(req: ScenarioCreateRequest, request: Request) -> ScenarioCreateResponse:
    if req.preset is None and req.scenario is None:
        raise HTTPException(status_code=422, detail="provide a preset name or a scenario overlay")
    try:
        cfg = resolve_config(req.scenario, preset=req.preset)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown preset {req.preset!r}")
    except Exception as exc:  # noqa: BLE001 — surface validation failure to client
        raise HTTPException(status_code=422, detail=f"invalid scenario: {exc}")

    reg = _registry(request)
    assert cfg.scenario is not None
    scenario_id = reg.scenarios.create(cfg.scenario)
    return ScenarioCreateResponse(scenario_id=scenario_id)


@router.get("/scenario/{scenario_id}", response_model=ScenarioGetResponse)
def get_scenario(scenario_id: str, request: Request) -> ScenarioGetResponse:
    reg = _registry(request)
    overlay = reg.scenarios.get(scenario_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id!r}")
    return ScenarioGetResponse(scenario_id=scenario_id, scenario=overlay)


# --------------------------------------------------------------------------- #
# Run                                                                         #
# --------------------------------------------------------------------------- #


@router.post("/run", response_model=RunStartResponse)
def start_run(req: RunStartRequest, request: Request) -> RunStartResponse:
    reg = _registry(request)
    overlay = reg.scenarios.get(req.scenario_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail=f"unknown scenario {req.scenario_id!r}")

    active = _pick_active_solver(req.solver)

    enabled = req.enabled_solvers
    if enabled is not None:
        unknown = [s for s in enabled if s not in REGISTRY]
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown solvers {unknown}")

    overlay = dict(overlay)
    if req.seed is not None:
        overlay["seed"] = int(req.seed)

    try:
        cfg = resolve_config(overlay)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"invalid scenario: {exc}")

    run_id = reg.new_run_id()
    controller = RunController(
        run_id,
        cfg,
        active_solver=active,
        enabled_solvers=enabled,
    )
    reg.runs[run_id] = controller
    return RunStartResponse(
        run_id=run_id,
        active_solver=controller.active_solver,
        enabled_solvers=controller.enabled_solvers,
    )


def _controller(reg: Registry, run_id: str) -> RunController:
    controller = reg.runs.get(run_id)
    if controller is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return controller


@router.post("/run/{run_id}/control", response_model=RunControlResponse)
def control_run(run_id: str, msg: ControlMessage, request: Request) -> RunControlResponse:
    """Apply a control action (pdd.md 12.3). Validated by :class:`ControlMessage`."""
    reg = _registry(request)
    controller = _controller(reg, run_id)

    action = msg.action
    try:
        if action == "pause":
            controller.pause()
        elif action == "resume":
            # Lazily start the driver task on first resume if it hasn't run yet and an
            # event loop is available (HTTP-only control without a WS attached).
            if controller.status in ("created", "running", "paused"):
                _ensure_started(controller)
            controller.resume()
        elif action == "step":
            _ensure_started(controller)
            controller.step(msg.epochs if msg.epochs is not None else 1)
        elif action == "stop":
            controller.stop()
        elif action == "set_solver":
            if msg.solver is None:
                raise HTTPException(status_code=422, detail="set_solver requires 'solver'")
            controller.set_solver(msg.solver)
        elif action == "set_speed":
            if msg.multiplier is None:
                raise HTTPException(status_code=422, detail="set_speed requires 'multiplier'")
            controller.set_speed(msg.multiplier)
        else:  # pragma: no cover — Literal already constrains this
            raise HTTPException(status_code=422, detail=f"unknown action {action!r}")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return RunControlResponse(
        run_id=run_id,
        action=action,
        status=controller.status,
        active_solver=controller.active_solver,
        speed=controller.speed,
    )


def _ensure_started(controller: RunController) -> None:
    """Start the controller's driver if an event loop is running and it hasn't begun."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover — only outside an async context
        return
    if controller._task is None:
        controller.start()


@router.get("/run/{run_id}/summary", response_model=RunSummaryResponse)
def run_summary(run_id: str, request: Request) -> RunSummaryResponse:
    reg = _registry(request)
    controller = _controller(reg, run_id)
    artifacts: dict[str, str] = {"telemetry_ws": f"/ws/run/{run_id}"}
    return RunSummaryResponse(
        run_id=run_id,
        status=controller.status,
        summary=controller.summary,
        telemetry_hash=controller.telemetry_hash,
        artifacts=artifacts,
    )


# --------------------------------------------------------------------------- #
# Batch                                                                       #
# --------------------------------------------------------------------------- #


@router.post("/batch", response_model=BatchStartResponse)
async def start_batch(req: BatchStartRequest, request: Request) -> BatchStartResponse:
    reg = _registry(request)

    if req.sweep_spec is not None:
        spec: dict[str, Any] = dict(req.sweep_spec)
    elif req.sweep is not None:
        try:
            spec = load_sweep(req.sweep)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"unknown sweep {req.sweep!r}")
    else:
        raise HTTPException(status_code=422, detail="provide a sweep name or a sweep_spec")

    if req.scenario_id is not None:
        spec = dict(spec)
        spec["base_scenario"] = req.scenario_id

    batch_id = reg.new_batch_id()
    job = BatchJob(batch_id=batch_id, spec=spec, status="running")
    reg.batches[batch_id] = job

    # Run the (blocking) sweep off the event loop so the request returns promptly while
    # the job completes; tests await completion via GET /results once status flips.
    async def _drive() -> None:
        try:
            await asyncio.to_thread(run_batch, job, reg.scenarios)
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"

    asyncio.ensure_future(_drive())
    return BatchStartResponse(batch_id=batch_id, status=job.status)


@router.get("/batch/{batch_id}/results", response_model=BatchResultsResponse)
def batch_results(batch_id: str, request: Request) -> BatchResultsResponse:
    reg = _registry(request)
    job = reg.batches.get(batch_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown batch {batch_id!r}")
    return BatchResultsResponse(
        batch_id=batch_id,
        status=job.status,
        parameter=job.parameter,
        values=job.values,
        series=job.series,
        breakeven_crossover=job.breakeven_crossover,
    )


# --------------------------------------------------------------------------- #
# Catalog                                                                     #
# --------------------------------------------------------------------------- #


@router.get("/solvers", response_model=SolversListResponse)
def list_solvers() -> SolversListResponse:
    return SolversListResponse(
        reference=DEFAULT_REFERENCE_SOLVER,
        solvers=[
            SolverMeta(name=name, is_reference=(name == DEFAULT_REFERENCE_SOLVER))
            for name in available_solvers()
        ],
    )


@router.get("/weather", response_model=WeatherListResponse)
def list_weather() -> WeatherListResponse:
    cfg = load_config()
    profiles = [
        WeatherMeta(name=name, alpha=prof.alpha)
        for name, prof in cfg.physics.weather_profiles.items()
    ]
    return WeatherListResponse(profiles=profiles)
