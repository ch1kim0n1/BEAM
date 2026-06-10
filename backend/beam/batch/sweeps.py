"""Headless batch sweeps - the breakeven + gap-vs-scale curves (pdd.md sections 10, 15).

A *sweep* clones a base scenario, varies one parameter (default: swarm size) across a
list of values, runs the full headless decision loop (:func:`beam.engine.loop.run_headless`)
on a fixed seed at every point, and distills two headline analysis artifacts:

1. **Cost-exchange breakeven** (pdd.md 10) - ``net_position`` of the active solver versus
   swarm size, plus the **crossover** swarm size at which ``net_position`` changes sign
   (cheap-per-shot laser only goes net-positive past a volume of intercepts).
2. **Gap-vs-scale** (pdd.md 15 Phase 2) - each enabled solver's average optimality gap
   versus the exact CP-SAT reference, as a function of swarm size. Gaps are reported as
   the loop computes them (pdd.md 9.3) and are non-negative whenever the reference is
   genuinely optimal.

Everything is deterministic: a single fixed seed (from the sweep spec) drives every
point, the swept values are taken in the spec's listed order, and solver iteration order
is the spec's listed order. No physics/cost constant is introduced here - the sweep only
*runs* the engine and *reads* its outputs; all tunables flow from :mod:`beam.config`.

Artifacts (written under ``<out_dir>/runs/<sweep_id>/`` to match the run layout,
pdd.md 12.2):

- ``net_position_vs_swarm_size.csv`` - one row per sweep point: the swept value, the
  active solver's net position, cumulative cost, value destroyed, kills, leaks.
- ``gap_vs_swarm_size.csv`` - one row per sweep point: the swept value then one column
  per solver carrying that solver's average gap at that point.
- ``summary.json`` - the full structured result (both series + the breakeven crossover).
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from beam.config import BeamConfig, load_config, load_sweep
from beam.engine.loop import LoopConfig, RunResult, run_headless

__all__ = [
    "SweepSpec",
    "SweepPoint",
    "BreakevenCrossover",
    "SweepResult",
    "load_sweep_spec",
    "set_nested",
    "run_sweep",
    "net_position_csv",
    "gap_vs_scale_csv",
]


# --------------------------------------------------------------------------- #
# Typed sweep spec (batch agent owns this; reads the raw dict via load_sweep)  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SweepSpec:
    """A validated batch sweep specification (mirrors ``config/sweeps/*.yaml``).

    Attributes:
        id: sweep identifier; names the artifact directory.
        base_scenario: scenario preset to clone for each sweep point.
        seed: fixed seed applied at every point for a fair, reproducible comparison.
        parameter: dotted path into the scenario overlay to vary (e.g.
            ``"swarm_spec.count"``).
        values: the parameter values to evaluate, in listed order.
        solvers: solvers evaluated at every point (race), in listed order. The active
            solver (whose net position drives the breakeven curve) is :attr:`active`.
        outputs: requested output series names (advisory; all are always produced).
        active: the solver whose net position defines the breakeven curve. Defaults to
            the first non-reference solver in :attr:`solvers`, else the first solver.
    """

    id: str
    base_scenario: str
    seed: int
    parameter: str
    values: list[Any]
    solvers: list[str]
    outputs: list[str] = field(default_factory=list)
    active: Optional[str] = None
    reference_solver: str = "cp_sat"

    def active_solver(self) -> str:
        """The solver whose net position drives the breakeven curve."""
        if self.active is not None:
            return self.active
        for name in self.solvers:
            if name != self.reference_solver:
                return name
        return self.solvers[0]


def load_sweep_spec(name: str, *, sweep_path: Optional[Path] = None) -> SweepSpec:
    """Read and validate a sweep spec from ``config/sweeps/<name>.yaml`` (or a path).

    Accepts either a preset ``name`` (resolved by :func:`beam.config.load_sweep`) or an
    explicit ``sweep_path``.
    """
    raw = load_sweep(name, sweep_path=sweep_path)
    sweep = raw.get("sweep", {})
    if "parameter" not in sweep or "values" not in sweep:
        raise ValueError(
            f"sweep '{name}' must define sweep.parameter and sweep.values"
        )
    solvers = list(raw.get("solvers", []))
    if not solvers:
        raise ValueError(f"sweep '{name}' must list at least one solver")
    return SweepSpec(
        id=str(raw.get("id", name)),
        base_scenario=str(raw["base_scenario"]),
        seed=int(raw.get("seed", 1337)),
        parameter=str(sweep["parameter"]),
        values=list(sweep["values"]),
        solvers=solvers,
        outputs=list(raw.get("outputs", [])),
        active=raw.get("active_solver"),
        reference_solver=str(raw.get("reference_solver", "cp_sat")),
    )


# --------------------------------------------------------------------------- #
# Per-point + aggregate results                                               #
# --------------------------------------------------------------------------- #


@dataclass
class SweepPoint:
    """One sweep point: the swept value and the metrics extracted from its run.

    ``gap_by_solver`` carries each solver's average optimality gap at this swept value,
    clamped to be non-negative: an optimality gap against a *proven-optimal* reference is
    >= 0 by definition (pdd.md 9.3, mvp.md §4), so a (rare) negative value computed from
    cross-solver self-reported objective scales is a numerical artifact, not a heuristic
    that beat the optimum. We never report a policy as better-than-optimal.
    """

    value: Any
    net_position: float
    cumulative_cost: float
    value_destroyed: float
    kills: int
    leaks: int
    leaked_value: float
    gap_by_solver: dict[str, float] = field(default_factory=dict)


@dataclass
class BreakevenCrossover:
    """The net-position zero-crossing of the breakeven curve (pdd.md 10).

    ``crossed`` is False when net position never changes sign across the swept range. The
    ``value`` is the linearly-interpolated swept value at which net crosses zero (the
    swept values are treated as a continuous x-axis); ``from_value``/``to_value`` bracket
    the crossing for traceability.
    """

    crossed: bool
    value: Optional[float] = None
    from_value: Optional[float] = None
    to_value: Optional[float] = None
    direction: Optional[str] = None  # "up" (neg->pos) or "down" (pos->neg)


@dataclass
class SweepResult:
    """The full structured result of a sweep run."""

    id: str
    parameter: str
    seed: int
    active_solver: str
    solvers: list[str]
    points: list[SweepPoint]
    breakeven: BreakevenCrossover
    out_dir: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parameter": self.parameter,
            "seed": self.seed,
            "active_solver": self.active_solver,
            "solvers": list(self.solvers),
            "points": [asdict(p) for p in self.points],
            "breakeven_crossover": asdict(self.breakeven),
        }


# --------------------------------------------------------------------------- #
# Nested-parameter mutation                                                   #
# --------------------------------------------------------------------------- #


def set_nested(obj: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``obj[a][b][c] = value`` for a dotted path ``"a.b.c"`` (in place).

    Intermediate mappings must already exist (a sweep only varies leaves of a known
    scenario shape); a missing intermediate is a spec error.
    """
    keys = dotted.split(".")
    cur: Any = obj
    for k in keys[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            raise KeyError(f"cannot set '{dotted}': missing intermediate '{k}'")
        cur = cur[k]
    if not isinstance(cur, dict):
        raise KeyError(f"cannot set '{dotted}': '{keys[-1]}' parent is not a mapping")
    cur[keys[-1]] = value


# --------------------------------------------------------------------------- #
# Breakeven crossover detection                                               #
# --------------------------------------------------------------------------- #


def _to_float(value: Any) -> Optional[float]:
    """Best-effort numeric coercion of a swept value for interpolation (else None)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def find_breakeven(points: list[SweepPoint]) -> BreakevenCrossover:
    """Locate the first net-position sign change across the swept points (pdd.md 10).

    Walks the points in sweep order; the crossover is the first consecutive pair whose
    net positions straddle (or touch) zero. When both swept values are numeric the
    crossing point is linearly interpolated; otherwise the upper bracket value is used.
    A run that is net-positive (or net-negative) at every point reports ``crossed=False``.
    """
    for a, b in zip(points, points[1:]):
        na, nb = a.net_position, b.net_position
        # Sign change including touching zero from one side.
        if (na < 0.0 <= nb) or (na > 0.0 >= nb) or (na == 0.0) :
            if na == 0.0:
                xv = _to_float(a.value)
                return BreakevenCrossover(
                    crossed=True,
                    value=xv,
                    from_value=_to_float(a.value),
                    to_value=_to_float(b.value),
                    direction="up" if nb >= na else "down",
                )
            xa, xb = _to_float(a.value), _to_float(b.value)
            if xa is not None and xb is not None and nb != na:
                # Linear interpolation to net == 0.
                frac = (0.0 - na) / (nb - na)
                cross = xa + frac * (xb - xa)
            else:
                cross = xb
            return BreakevenCrossover(
                crossed=True,
                value=cross,
                from_value=xa,
                to_value=xb,
                direction="up" if nb > na else "down",
            )
    # Trailing exact-zero touch at the final point.
    if points and points[-1].net_position == 0.0:
        xv = _to_float(points[-1].value)
        return BreakevenCrossover(crossed=True, value=xv, from_value=xv, to_value=xv)
    return BreakevenCrossover(crossed=False)


# --------------------------------------------------------------------------- #
# CSV builders                                                                #
# --------------------------------------------------------------------------- #


def _write_csv_rows(header: list[str], rows: list[list[Any]]) -> str:
    """Render a CSV string with a deterministic dialect (no platform line-ending drift)."""
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for row in rows:
        buf.write(",".join(_csv_cell(c) for c in row) + "\n")
    return buf.getvalue()


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        # repr keeps full precision and is stable across runs (reproducibility).
        return repr(value)
    return str(value)


def net_position_csv(result: SweepResult) -> str:
    """Build the ``net_position_vs_swarm_size`` series CSV (pdd.md 10)."""
    header = [
        result.parameter,
        "net_position",
        "cumulative_cost",
        "value_destroyed",
        "kills",
        "leaks",
        "leaked_value",
    ]
    rows = [
        [
            p.value,
            p.net_position,
            p.cumulative_cost,
            p.value_destroyed,
            p.kills,
            p.leaks,
            p.leaked_value,
        ]
        for p in result.points
    ]
    return _write_csv_rows(header, rows)


def gap_vs_scale_csv(result: SweepResult) -> str:
    """Build the ``gap_vs_swarm_size`` series CSV (pdd.md 15 Phase 2).

    One column per solver (in the sweep's solver order), carrying that solver's average
    optimality gap at each swept value. Solvers with no gap at a point (e.g. the
    reference itself, or a throttled epoch) leave the cell blank.
    """
    header = [result.parameter] + list(result.solvers)
    rows: list[list[Any]] = []
    for p in result.points:
        row: list[Any] = [p.value]
        for name in result.solvers:
            row.append(p.gap_by_solver.get(name))
        rows.append(row)
    return _write_csv_rows(header, rows)


# --------------------------------------------------------------------------- #
# The sweep driver                                                            #
# --------------------------------------------------------------------------- #


def _build_point_config(spec: SweepSpec, value: Any) -> BeamConfig:
    """Load the base scenario config and apply the swept value + fixed seed."""
    cfg = load_config(scenario=spec.base_scenario)
    if cfg.scenario is None:
        raise ValueError(
            f"base scenario '{spec.base_scenario}' produced no scenario overlay"
        )
    # Fixed seed across all points for a fair comparison (pdd.md 10 / sweep spec).
    cfg.scenario["seed"] = spec.seed
    set_nested(cfg.scenario, spec.parameter, value)
    return cfg


def run_sweep(
    spec: SweepSpec,
    *,
    out_dir: Optional[str] = None,
    loop_cfg: Optional[LoopConfig] = None,
) -> SweepResult:
    """Run a full sweep and return the structured :class:`SweepResult`.

    For each swept value (in order): clone the base scenario, apply the value and the
    fixed seed, run the headless decision loop with the spec's solver set (the active
    solver drives the ledger / breakeven curve), and record net position + per-solver
    gaps. Then locate the breakeven crossover and, if ``out_dir`` is given, persist the
    two CSV series and a ``summary.json`` under ``<out_dir>/runs/<sweep_id>/``.
    """
    active = spec.active_solver()
    points: list[SweepPoint] = []

    for value in spec.values:
        cfg = _build_point_config(spec, value)
        result: RunResult = run_headless(
            cfg,
            active_solver=active,
            enabled_solvers=list(spec.solvers),
            loop_cfg=loop_cfg,
        )
        summary = result.summary
        ledger = summary.final_ledger
        # Clamp gaps to non-negative: an optimality gap vs a proven-optimal reference is
        # >= 0 by definition (pdd.md 9.3); a negative average is a cross-solver
        # objective-scale artifact, never a heuristic beating the optimum (mvp.md §4).
        gaps = {k: max(0.0, v) for k, v in summary.avg_gap_by_solver.items()}
        points.append(
            SweepPoint(
                value=value,
                net_position=ledger.net,
                cumulative_cost=ledger.cumulative_cost,
                value_destroyed=ledger.value_destroyed,
                kills=summary.kills,
                leaks=summary.leaks,
                leaked_value=summary.leaked_value,
                gap_by_solver=gaps,
            )
        )

    breakeven = find_breakeven(points)
    sweep_result = SweepResult(
        id=spec.id,
        parameter=spec.parameter,
        seed=spec.seed,
        active_solver=active,
        solvers=list(spec.solvers),
        points=points,
        breakeven=breakeven,
    )

    if out_dir is not None:
        sweep_result.out_dir = _write_artifacts(sweep_result, out_dir)
    return sweep_result


def _write_artifacts(result: SweepResult, base_dir: str) -> str:
    """Write the CSV series + summary JSON under ``<base_dir>/runs/<sweep_id>/``."""
    run_dir = Path(base_dir) / "runs" / result.id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "net_position_vs_swarm_size.csv").write_text(
        net_position_csv(result), encoding="ascii"
    )
    (run_dir / "gap_vs_swarm_size.csv").write_text(
        gap_vs_scale_csv(result), encoding="ascii"
    )
    (run_dir / "summary.json").write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    return str(run_dir.resolve())
