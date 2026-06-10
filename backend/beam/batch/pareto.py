"""Pareto sweep: run headless at N lambda values, collect (cost, value_saved) pairs."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from beam.batch.sweeps import SweepSpec, _build_point_config
from beam.engine.loop import LoopConfig, run_headless
from beam.solvers.base import REGISTRY
from beam.solvers.cp_sat import CpSatSolver

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
    """Run headless at each lambda value, collecting (cost, value_saved) pairs.

    At each lambda, CP-SAT uses cost_weight = 1 - lambda so the objective blends
    value maximization (lambda=1) and cost minimization (lambda=0).
    """
    if spec.n_points < 2:
        raise ValueError(f"n_points must be >= 2, got {spec.n_points}")

    if spec.solver != "cp_sat":
        raise ValueError("Pareto sweep requires solver='cp_sat' (supports cost_weight)")

    sweep_spec = SweepSpec(
        id="pareto",
        base_scenario=spec.scenario,
        seed=spec.seed,
        parameter="swarm_spec.count",
        values=[spec.seed],  # value unused; we re-use the config as-is
        solvers=[spec.solver],
    )
    base_cfg = _build_point_config(sweep_spec, value=None)

    lambdas = [i / (spec.n_points - 1) for i in range(spec.n_points)]
    points: list[ParetoPoint] = []
    orig_class = REGISTRY.get(spec.solver)

    for idx, lam in enumerate(lambdas):
        cost_weight = 1.0 - lam

        # Temporarily replace the registered class with a subclass that
        # injects cost_weight into every solve() call.
        _cw = cost_weight  # cell variable for closure

        class _WeightedCpSat(CpSatSolver):  # type: ignore[misc]
            def solve(self, state, deadline_ms):  # type: ignore[override]
                return super().solve(state, deadline_ms, cost_weight=_cw)

        REGISTRY[spec.solver] = _WeightedCpSat  # type: ignore[assignment]
        try:
            result = run_headless(
                base_cfg,
                active_solver=spec.solver,
                enabled_solvers=[spec.solver],
                loop_cfg=loop_cfg,
            )
        finally:
            if orig_class is not None:
                REGISTRY[spec.solver] = orig_class  # type: ignore[assignment]

        ledger = result.summary.final_ledger
        points.append(ParetoPoint(
            lam=lam,
            value_saved=ledger.value_destroyed,
            total_cost=ledger.cumulative_cost,
            kills=result.summary.kills,
            leaks=result.summary.leaks,
        ))
        log.debug(
            "pareto lambda=%.2f  value=%.0f  cost=%.0f",
            lam, ledger.value_destroyed, ledger.cumulative_cost,
        )
        if on_point:
            on_point(idx + 1, spec.n_points)

    return ParetoResult(
        points=points,
        scenario=spec.scenario,
        seed=spec.seed,
        solver=spec.solver,
    )
