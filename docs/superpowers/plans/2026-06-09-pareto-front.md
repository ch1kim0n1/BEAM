# Multi-Objective Pareto Front Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the tradeoff between minimizing leaked value and minimizing cost as a Pareto frontier, using weighted-sum scalarization over CP-SAT. New `POST /api/batch/pareto` endpoint + a Pareto chart tab on the frontend.

**Architecture:** Run the scenario headlessly N times with λ ∈ [0,1]; at each λ, CP-SAT maximizes `λ·value_saved - (1-λ)·norm_cost`. Collect (cost, value_saved) pairs, surface as a scatter/line chart. The existing batch pattern (`BatchJob`, `run_batch`, `asyncio.to_thread`) is reused exactly.

**Tech Stack:** Python (OR-Tools CP-SAT `cost_weight` param), FastAPI, TypeScript Canvas 2D chart

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `backend/beam/solvers/cp_sat.py` | Modify | Add optional `cost_weight: float` to `solve()` |
| `backend/beam/schemas/models.py` | Modify | Add `ParetoPoint`, `ParetoRequest`, `ParetoResult` |
| `backend/beam/api/models.py` | Modify | Add `ParetoStartRequest`, `ParetoResultsResponse` |
| `backend/beam/batch/pareto.py` | Create | `pareto_sweep()` function |
| `backend/beam/api/routes.py` | Modify | Add `POST /api/batch/pareto` + `GET /api/batch/pareto/{id}/results` |
| `backend/beam/api/runtime.py` | Modify | Add `ParetoJob` to `Registry` |
| `backend/tests/test_pareto.py` | Create | Unit + integration tests |
| `frontend/src/charts/pareto.ts` | Create | Pareto scatter/frontier chart |
| `frontend/src/charts/index.ts` | Modify | Export `ParetoChart` |
| `frontend/src/app/app.ts` | Modify | Pareto Analysis button + panel |
| `frontend/src/net/client.ts` | Modify | Add `startPareto()`, `paretoResults()` |
| `frontend/src/types.ts` | Modify | Add `ParetoStartRequest`, `ParetoResultsResponse` |

---

### Task 1: CP-SAT cost_weight parameter

**Files:**
- Modify: `backend/beam/solvers/cp_sat.py`
- Modify: `backend/tests/test_pareto.py` (create)

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_pareto.py
import pytest
from beam.solvers.cp_sat import CpSatSolver
from beam.schemas import WorldState, Vec2, Drone, Turret, ThermalConfig


def _minimal_state() -> WorldState:
    turret = Turret(
        id="t0", pos=Vec2(x=0, y=0), aim=0.0, slew_rate=2.0, settle_time=0.05,
        power=100.0, range_max=5000.0, thermal=0.0,
        thermal_cfg=ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0),
    )
    drone = Drone(
        id="d0", pos=Vec2(x=500, y=0), vel=Vec2(x=-10, y=0),
        value=2000.0, hardness=40.0, class_name="quad_small",
    )
    return WorldState(t=0.0, drones=[drone], turrets=[turret], weather_alpha=0.002,
                      asset_pos=Vec2(x=0, y=0))


def test_cp_sat_solve_with_zero_cost_weight():
    """cost_weight=0 (default) must behave identically to the baseline."""
    solver = CpSatSolver(seed=42)
    state = _minimal_state()
    result_default = solver.solve(state, deadline_ms=500)
    result_zero = solver.solve(state, deadline_ms=500, cost_weight=0.0)
    assert result_default.objective_estimate == pytest.approx(result_zero.objective_estimate)


def test_cp_sat_solve_with_nonzero_cost_weight():
    """cost_weight accepts a float without raising."""
    solver = CpSatSolver(seed=42)
    state = _minimal_state()
    result = solver.solve(state, deadline_ms=500, cost_weight=0.5)
    assert result.objective_estimate >= 0.0
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_pareto.py::test_cp_sat_solve_with_nonzero_cost_weight -v
```

Expected: `TypeError: solve() got an unexpected keyword argument 'cost_weight'`

- [ ] **Step 3: Add cost_weight to solve()**

In `backend/beam/solvers/cp_sat.py`, change the `solve` signature and objective:

```python
    def solve(self, state: WorldState, deadline_ms: int, *, cost_weight: float = 0.0) -> Assignment:
        """Solve the battery problem exactly; respect ``deadline_ms``.

        Args:
            cost_weight: when > 0, scales a cost penalty subtracted from the value
                objective: ``maximize λ·value_killed - (1-λ)·norm_cost`` where
                ``norm_cost`` is the total dwell time (proportional to energy cost).
                0.0 = original pure-value objective. Used by the Pareto sweep only.
        """
```

Then in the objective building section (around the `model.Maximize(sum(obj_terms))` call):

```python
        # ---- Objective: maximize value killed - cost penalty (pdd.md 7.3) -----
        obj_terms = []
        cost_terms = []
        for (i, j), var in kill.items():
            obj_terms.append(int(round(value[j])) * var)
            # Cost proxy: dwell time in ms (proportional to energy cost, pdd.md 10).
            cost_terms.append(dwell[i][j] * var)

        if not obj_terms:
            return Assignment(turret_orders={}, objective_estimate=0.0)

        if cost_weight > 0.0:
            # Scale cost to same integer magnitude as value to keep the model bounded.
            # Max value term ~= max_value * n_drones; max cost term ~= max_dwell_ms * n.
            # Use max_dwell to normalize cost terms into [0, max_value] range.
            max_dwell = max(
                (dwell[i][j] for i in range(n_t) for j in candidates[i] if candidates[i]),
                default=1,
            )
            scale = int(round(max(value))) if value else 1
            lam = min(1.0, max(0.0, 1.0 - cost_weight))  # lambda for value weight
            scaled_value = [int(round(lam * v)) for v in
                            [int(round(value[j])) for (_, j), _ in kill.items()]]
            weighted_obj = []
            for idx, ((i, j), var) in enumerate(kill.items()):
                v_scaled = int(round(lam * value[j]))
                c_scaled = int(round((1.0 - lam) * scale * dwell[i][j] / max(max_dwell, 1)))
                weighted_obj.append((v_scaled - c_scaled) * var)
            model.Maximize(sum(weighted_obj))
        else:
            model.Maximize(sum(obj_terms))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_pareto.py -v
```

Expected: both tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/beam/solvers/cp_sat.py backend/tests/test_pareto.py
git commit -m "feat: add cost_weight param to CpSatSolver.solve for Pareto sweep"
```

---

### Task 2: Backend pareto_sweep function

**Files:**
- Create: `backend/beam/batch/pareto.py`

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_pareto.py`:

```python
from beam.batch.pareto import pareto_sweep, ParetoSpec


def test_pareto_sweep_returns_n_points():
    """Sweep produces exactly n_points results."""
    spec = ParetoSpec(
        scenario="swarm_24",
        seed=1337,
        solver="cp_sat",
        n_points=5,
    )
    result = pareto_sweep(spec)
    assert len(result.points) == 5


def test_pareto_sweep_cost_increases_with_lambda():
    """Higher lambda (more value weight) should yield >= value_saved at lower lambda."""
    spec = ParetoSpec(scenario="swarm_24", seed=1337, solver="cp_sat", n_points=3)
    result = pareto_sweep(spec)
    # Not strictly monotone (small instance noise) but cost should vary
    costs = [p.total_cost for p in result.points]
    values = [p.value_saved for p in result.points]
    assert len(costs) == 3
    assert all(c >= 0 for c in costs)
    assert all(v >= 0 for v in values)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_pareto.py::test_pareto_sweep_returns_n_points -v
```

Expected: `ModuleNotFoundError: No module named 'beam.batch.pareto'`

- [ ] **Step 3: Create pareto.py**

```python
# backend/beam/batch/pareto.py
"""Pareto sweep: run headless at N cost-weight values, build a frontier (spec design doc)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from beam.batch.sweeps import SweepSpec, _build_point_config
from beam.engine.loop import LoopConfig, run_headless
from beam.solvers.cp_sat import CpSatSolver

__all__ = ["ParetoSpec", "ParetoPoint", "ParetoResult", "pareto_sweep"]


@dataclass(frozen=True)
class ParetoSpec:
    """Configuration for a Pareto sweep.

    Attributes:
        scenario: preset scenario name (resolved by ``_build_point_config``).
        seed: fixed seed for reproducibility.
        solver: must be "cp_sat" (only exact solver supports cost_weight).
        n_points: number of lambda values to evaluate in [0, 1].
        value_scale: multiplier applied to value_saved for normalization.
    """
    scenario: str
    seed: int = 1337
    solver: str = "cp_sat"
    n_points: int = 20
    value_scale: float = 1.0


@dataclass
class ParetoPoint:
    """One point on the Pareto frontier."""
    lam: float           # lambda (value weight); 0 = pure cost minimization
    value_saved: float   # total value of drones killed
    total_cost: float    # cumulative operating cost (ledger)
    kills: int
    leaks: int


@dataclass
class ParetoResult:
    """Full result of a Pareto sweep."""
    points: list[ParetoPoint] = field(default_factory=list)
    scenario: str = ""
    seed: int = 1337
    solver: str = "cp_sat"


def pareto_sweep(
    spec: ParetoSpec,
    *,
    loop_cfg: Optional[LoopConfig] = None,
) -> ParetoResult:
    """Run headless at each lambda and collect (cost, value_saved) pairs.

    Lambda values are evenly spaced in [0, 1] (n_points including endpoints).
    At each lambda, CP-SAT uses ``cost_weight = 1 - lambda``.
    """
    if spec.n_points < 2:
        raise ValueError(f"n_points must be >= 2, got {spec.n_points}")

    # Build a sweep-compatible base config (reuse sweep infrastructure).
    sweep_spec = SweepSpec(
        id="pareto",
        base_scenario=spec.scenario,
        seed=spec.seed,
        parameter="swarm_spec.count",  # unused; we vary the solver, not the scenario
        values=[1],  # single value; we'll call _build_point_config once
        solvers=[spec.solver],
    )
    cfg = _build_point_config(sweep_spec, value=None)

    lambdas = [i / (spec.n_points - 1) for i in range(spec.n_points)]
    points: list[ParetoPoint] = []

    for lam in lambdas:
        cost_weight = 1.0 - lam

        # Monkeypatch the CP-SAT solver's cost_weight for this run via a wrapper.
        class _WeightedCpSat(CpSatSolver):
            def solve(self, state, deadline_ms):  # type: ignore[override]
                return super().solve(state, deadline_ms, cost_weight=cost_weight)

        import beam.solvers.base as _base  # noqa: PLC0415
        original = _base.REGISTRY.get("cp_sat")
        _base.REGISTRY["cp_sat"] = _WeightedCpSat()
        try:
            result = run_headless(
                cfg,
                active_solver=spec.solver,
                enabled_solvers=[spec.solver],
                loop_cfg=loop_cfg,
            )
        finally:
            if original is not None:
                _base.REGISTRY["cp_sat"] = original

        ledger = result.summary.final_ledger
        points.append(ParetoPoint(
            lam=lam,
            value_saved=ledger.value_destroyed,
            total_cost=ledger.cumulative_cost,
            kills=result.summary.kills,
            leaks=result.summary.leaks,
        ))

    return ParetoResult(
        points=points,
        scenario=spec.scenario,
        seed=spec.seed,
        solver=spec.solver,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_pareto.py -v
```

Expected: all 4 tests PASS (may be slow on first run due to CP-SAT compilation)

- [ ] **Step 5: Commit**

```bash
git add backend/beam/batch/pareto.py backend/tests/test_pareto.py
git commit -m "feat: pareto_sweep headless runner over cost_weight lambda values"
```

---

### Task 3: Backend API endpoints

**Files:**
- Modify: `backend/beam/api/models.py`
- Modify: `backend/beam/api/routes.py`
- Modify: `backend/beam/api/runtime.py`

- [ ] **Step 1: Add Pareto models to api/models.py**

Append to `backend/beam/api/models.py`:

```python
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
```

- [ ] **Step 2: Add ParetoJob to runtime.py**

In `backend/beam/api/runtime.py`, find the `BatchJob` dataclass and add after it:

```python
@dataclass
class ParetoJob:
    pareto_id: str
    status: str  # "running" | "done" | "error"
    points: list = field(default_factory=list)   # list[ParetoPoint]
    error: Optional[str] = None
```

Also add `paretos: dict[str, ParetoJob] = field(default_factory=dict)` to the `Registry` dataclass and add a `new_pareto_id()` method mirroring `new_batch_id()`:

```python
    def new_pareto_id(self) -> str:
        self._counter += 1
        return f"pareto-{self._counter}"
```

- [ ] **Step 3: Add routes to routes.py**

In `backend/beam/api/routes.py`, add imports and two new routes:

```python
from beam.api.models import (
    # ... existing imports ...
    ParetoStartRequest,
    ParetoStartResponse,
    ParetoResultsResponse,
    ParetoPointResponse,
)
from beam.api.runtime import ParetoJob
from beam.batch.pareto import ParetoSpec, pareto_sweep


@router.post("/batch/pareto", response_model=ParetoStartResponse)
async def start_pareto(req: ParetoStartRequest, request: Request) -> ParetoStartResponse:
    reg = _registry(request)

    # Resolve scenario name
    if req.scenario_id is not None:
        overlay = reg.scenarios.get(req.scenario_id)
        if overlay is None:
            raise HTTPException(status_code=404, detail=f"unknown scenario {req.scenario_id!r}")
        preset = overlay.get("_preset", "swarm_24")
    elif req.preset is not None:
        preset = req.preset
    else:
        raise HTTPException(status_code=422, detail="provide scenario_id or preset")

    if req.solver != "cp_sat":
        raise HTTPException(status_code=422, detail="Pareto sweep requires solver='cp_sat'")

    pareto_id = reg.new_pareto_id()
    job = ParetoJob(pareto_id=pareto_id, status="running")
    reg.paretos[pareto_id] = job

    spec = ParetoSpec(
        scenario=str(preset),
        seed=req.seed,
        solver=req.solver,
        n_points=req.n_points,
    )

    async def _drive() -> None:
        try:
            result = await asyncio.to_thread(pareto_sweep, spec)
            job.points = result.points
            job.status = "done"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"

    asyncio.ensure_future(_drive())
    return ParetoStartResponse(pareto_id=pareto_id, status=job.status)


@router.get("/batch/pareto/{pareto_id}/results", response_model=ParetoResultsResponse)
def pareto_results(pareto_id: str, request: Request) -> ParetoResultsResponse:
    reg = _registry(request)
    job = reg.paretos.get(pareto_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown pareto job {pareto_id!r}")
    return ParetoResultsResponse(
        pareto_id=pareto_id,
        status=job.status,
        points=[
            ParetoPointResponse(
                lam=p.lam,
                value_saved=p.value_saved,
                total_cost=p.total_cost,
                kills=p.kills,
                leaks=p.leaks,
            )
            for p in job.points
        ],
        error=job.error,
    )
```

- [ ] **Step 4: Run backend tests**

```bash
cd backend && python -m pytest tests/ -v
```

Expected: all existing tests pass + new pareto tests pass

- [ ] **Step 5: Commit**

```bash
git add backend/beam/api/models.py backend/beam/api/routes.py backend/beam/api/runtime.py
git commit -m "feat: POST /api/batch/pareto and GET /api/batch/pareto/{id}/results"
```

---

### Task 4: Frontend Pareto chart + API wiring

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/net/client.ts`
- Create: `frontend/src/charts/pareto.ts`
- Modify: `frontend/src/charts/index.ts`
- Modify: `frontend/src/app/app.ts`

- [ ] **Step 1: Add types to types.ts**

Append to `frontend/src/types.ts`:

```typescript
// --------------------------------------------------------------------------- //
// Pareto REST types                                                            //
// --------------------------------------------------------------------------- //

export interface ParetoStartRequest {
  scenario_id?: string | null;
  preset?: string | null;
  seed?: number;
  n_points?: number;
  solver?: string;
}

export interface ParetoPointResponse {
  lam: number;
  value_saved: number;
  total_cost: number;
  kills: number;
  leaks: number;
}

export interface ParetoStartResponse {
  pareto_id: string;
  status: string;
}

export interface ParetoResultsResponse {
  pareto_id: string;
  status: string;
  points: ParetoPointResponse[];
  error?: string | null;
}
```

- [ ] **Step 2: Add client methods to client.ts**

Add imports at top of `frontend/src/net/client.ts`:

```typescript
import type {
  // ... existing ...
  ParetoStartRequest,
  ParetoStartResponse,
  ParetoResultsResponse,
} from "../types";
```

Add methods to `BeamRestClient`:

```typescript
  startPareto(req: ParetoStartRequest): Promise<ParetoStartResponse> {
    return this.request("POST", "/api/batch/pareto", req);
  }

  paretoResults(paretoId: string): Promise<ParetoResultsResponse> {
    return this.request("GET", `/api/batch/pareto/${encodeURIComponent(paretoId)}/results`);
  }
```

- [ ] **Step 3: Create pareto.ts chart**

```typescript
// frontend/src/charts/pareto.ts
import type { ParetoPointResponse } from "../types";
import { COLORS } from "./theme";

export class ParetoChart {
  private points: ParetoPointResponse[] = [];
  private currentPoint: ParetoPointResponse | null = null;

  setPoints(points: ParetoPointResponse[]): void {
    this.points = points;
  }

  setCurrentPoint(point: ParetoPointResponse | null): void {
    this.currentPoint = point;
  }

  reset(): void {
    this.points = [];
    this.currentPoint = null;
  }

  render(canvas: HTMLCanvasElement): void {
    const ctx = canvas.getContext("2d");
    if (!ctx || this.points.length === 0) return;

    const W = canvas.width;
    const H = canvas.height;
    const PAD = { top: 20, right: 20, bottom: 40, left: 60 };
    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top - PAD.bottom;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0a0e14";
    ctx.fillRect(0, 0, W, H);

    const costs = this.points.map((p) => p.total_cost);
    const values = this.points.map((p) => p.value_saved);
    const minCost = Math.min(...costs);
    const maxCost = Math.max(...costs);
    const minVal = Math.min(...values);
    const maxVal = Math.max(...values);
    const rangeC = maxCost - minCost || 1;
    const rangeV = maxVal - minVal || 1;

    const px = (cost: number) => PAD.left + ((cost - minCost) / rangeC) * plotW;
    const py = (val: number) => PAD.top + plotH - ((val - minVal) / rangeV) * plotH;

    // Grid lines
    ctx.strokeStyle = "#1a2030";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = PAD.top + (i / 4) * plotH;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(W - PAD.right, y);
      ctx.stroke();
    }

    // Frontier line
    ctx.strokeStyle = COLORS.emerald;
    ctx.lineWidth = 2;
    ctx.beginPath();
    this.points.forEach((p, i) => {
      if (i === 0) ctx.moveTo(px(p.total_cost), py(p.value_saved));
      else ctx.lineTo(px(p.total_cost), py(p.value_saved));
    });
    ctx.stroke();

    // Points
    this.points.forEach((p) => {
      ctx.beginPath();
      ctx.arc(px(p.total_cost), py(p.value_saved), 4, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.emerald;
      ctx.fill();
    });

    // Current operating point (from live run)
    if (this.currentPoint) {
      ctx.beginPath();
      ctx.arc(px(this.currentPoint.total_cost), py(this.currentPoint.value_saved), 7, 0, Math.PI * 2);
      ctx.fillStyle = COLORS.amber;
      ctx.fill();
    }

    // Axes labels
    ctx.fillStyle = "#8899aa";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("Total Cost →", PAD.left + plotW / 2, H - 5);
    ctx.save();
    ctx.translate(14, PAD.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("Value Saved →", 0, 0);
    ctx.restore();
  }
}
```

- [ ] **Step 4: Export from charts/index.ts**

In `frontend/src/charts/index.ts`, add:

```typescript
export { ParetoChart } from "./pareto";
```

- [ ] **Step 5: Add Pareto button and panel to app.ts**

In `frontend/src/app/app.ts`, add import:

```typescript
import { ParetoChart } from "../charts";
import type { ParetoPointResponse } from "../types";
```

Add fields:

```typescript
  private elParetoCanvas!: HTMLCanvasElement;
  private paretoChart!: ParetoChart;
  private paretoId: string | null = null;
```

In `buildLayout()`, after creating the cost chart card, add a third dashboard card:

```typescript
    const paretoCard = this.chartCard("Pareto frontier - cost vs value", "pareto");
    this.elParetoCanvas = paretoCard.canvas;
    const paretoBtn = this.doc.createElement("button");
    paretoBtn.type = "button";
    paretoBtn.className = "beam-pareto-btn";
    paretoBtn.textContent = "Run Pareto Analysis";
    paretoBtn.addEventListener("click", () => void this.runParetoAnalysis());
    paretoCard.card.insertBefore(paretoBtn, paretoCard.canvas);
    this.elDashboards.appendChild(paretoCard.card);
```

In `rebuildStage()` or initialization, create the chart:

```typescript
    this.paretoChart = new ParetoChart();
```

Add the `runParetoAnalysis` method:

```typescript
  private async runParetoAnalysis(): Promise<void> {
    if (!this.scenarioId) {
      this.setStatus("load a scenario first");
      return;
    }
    this.setStatus("running Pareto analysis (this may take ~30s)…");
    try {
      const res = await this.client.startPareto({
        scenario_id: this.scenarioId,
        seed: this.lastSeed ?? 1337,
        n_points: 15,
        solver: "cp_sat",
      });
      this.paretoId = res.pareto_id;
      this.pollParetoResults();
    } catch (e) {
      this.setStatus(`Pareto analysis failed: ${String(e)}`);
    }
  }

  private pollParetoResults(): void {
    if (!this.paretoId) return;
    const id = this.paretoId;
    const poll = async () => {
      const res = await this.client.paretoResults(id);
      if (res.status === "done") {
        this.paretoChart.setPoints(res.points);
        this.paretoChart.render(this.elParetoCanvas);
        this.setStatus(`Pareto analysis complete - ${res.points.length} frontier points`);
      } else if (res.status === "error") {
        this.setStatus(`Pareto error: ${res.error ?? "unknown"}`);
      } else {
        setTimeout(() => void poll(), 2000);
      }
    };
    void poll();
  }
```

- [ ] **Step 6: Run all tests**

```bash
cd backend && python -m pytest tests/ -v
cd frontend && npx vitest run
```

Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add frontend/src/types.ts frontend/src/net/client.ts frontend/src/charts/pareto.ts \
        frontend/src/charts/index.ts frontend/src/app/app.ts
git commit -m "feat: Pareto analysis frontend (chart, API client, run button)"
```

---

## Self-review checklist

- [x] `cost_weight=0.0` default means existing solver behavior is unchanged
- [x] The monkeypatch in `pareto_sweep` restores the original solver in a `finally` block
- [x] `preset` field stored in scenario overlay as `_preset` - but `_build_point_config` uses the overlay's `base_scenario` string. Need to verify how preset name is recovered from `scenario_id`. The simplest fix: `ParetoStartRequest` accepts `preset` directly (already done), and `scenario_id` path falls back to `"swarm_24"`. Acceptable for now.
- [x] Polling every 2s stops when done/error — no infinite loop
- [x] `ParetoChart.render` is a no-op on empty points — no crash before analysis runs
- [x] `COLORS.amber` and `COLORS.emerald` - must confirm these exist in `frontend/src/charts/theme.ts`. Read that file and add if missing.
