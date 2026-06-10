# backend/beam/batch/pareto.py
"""Pareto sweep: run headless at N lambda values, collect (cost, value_saved) pairs."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from beam.batch.sweeps import SweepSpec, _build_point_config
from beam.engine.loop import LoopConfig, run_headless
from beam.solvers import REGISTRY

log = logging.getLogger(__name__)

__all__ = ["ParetoSpec", "ParetoPoint", "ParetoResult", "pareto_sweep"]


@dataclass(frozen=True)
class ParetoSpec:
    scenario: str
    seed: int = 1337
    solver: str = "cp_sat"
    n_points: int = 20


@dataclass
class ParetoPoint:
    lam: float
    value_saved: float
    total_cost: float
    kills: int
    leaks: int


@dataclass
class ParetoResult:
    points: list[ParetoPoint] = field(default_factory=list)
    scenario: str = ""
    seed: int = 1337
    solver: str = "cp_sat"


def pareto_sweep(
    spec: ParetoSpec,
    *,
    loop_cfg: Optional[LoopConfig] = None,
    on_point: Optional[Callable[[int, int], None]] = None,
) -> ParetoResult:
    if spec.n_points < 2:
        raise ValueError(f"n_points must be >= 2, got {spec.n_points}")

    sweep_spec = SweepSpec(
        id="pareto",
        base_scenario=spec.scenario,
        seed=spec.seed,
        parameter="swarm_spec.count",
        values=[1],
        solvers=[spec.solver],
    )
    base_cfg = _build_point_config(sweep_spec, value=None)

    lambdas = [i / (spec.n_points - 1) for i in range(spec.n_points)]
    points: list[ParetoPoint] = []

    original_solver = REGISTRY.get(spec.solver)

    for idx, lam in enumerate(lambdas):
        cost_weight = 1.0 - lam
        cfg = copy.deepcopy(base_cfg)

        # Wrap the solver to inject cost_weight
        orig = REGISTRY.get(spec.solver)
        if orig is not None:
            class _Wrapped(type(orig)):  # type: ignore[misc]
                def solve(self, state, deadline_ms):  # type: ignore[override]
                    return orig.solve(state, deadline_ms, cost_weight=cost_weight)
            REGISTRY[spec.solver] = _Wrapped.__new__(_Wrapped)
            REGISTRY[spec.solver].__dict__.update(orig.__dict__)
            REGISTRY[spec.solver].__class__ = _Wrapped

        try:
            result = run_headless(
                cfg,
                active_solver=spec.solver,
                enabled_solvers=[spec.solver],
                loop_cfg=loop_cfg,
            )
        finally:
            if original_solver is not None:
                REGISTRY[spec.solver] = original_solver

        ledger = result.summary.final_ledger
        points.append(ParetoPoint(
            lam=lam,
            value_saved=ledger.value_destroyed,
            total_cost=ledger.cumulative_cost,
            kills=result.summary.kills,
            leaks=result.summary.leaks,
        ))
        if on_point:
            on_point(idx + 1, spec.n_points)

    return ParetoResult(
        points=points,
        scenario=spec.scenario,
        seed=spec.seed,
        solver=spec.solver,
    )
