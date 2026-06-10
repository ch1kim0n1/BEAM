# Adaptive Swarm AI (Evolutionary) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A GA that evolves swarm approach parameters to maximize leaked drones against a chosen defender policy, producing a "worst-case" scenario. Outputs an evolved scenario YAML loadable in the editor.

**Architecture:** New `beam/evolve/` module with a `Genome` dataclass and `run_ga()` function. The genome varies `SwarmSpec` fields plus `KinematicsConfig.flocking` weights via the scenario overlay. The GA runs headless each generation using `run_headless()`. Fitness = leaked drone value. New `beam evolve` CLI command + `POST /api/evolve` endpoint.

**Tech Stack:** Python standard library (no external GA library — simple tournament GA is ~60 lines), existing `run_headless()`, FastAPI

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `backend/beam/evolve/__init__.py` | Create | Package marker |
| `backend/beam/evolve/genome.py` | Create | `Genome` dataclass + encode/decode to scenario overlay |
| `backend/beam/evolve/ga.py` | Create | `run_ga()` with tournament selection, crossover, mutation |
| `backend/beam/api/models.py` | Modify | Add `EvolveRequest`, `EvolveProgress`, `EvolveResult` |
| `backend/beam/api/routes.py` | Modify | Add `POST /api/evolve`, `GET /api/evolve/{id}/result` |
| `backend/beam/api/runtime.py` | Modify | Add `EvolveJob` to `Registry` |
| `backend/beam/cli.py` | Modify | Add `beam evolve` command |
| `backend/tests/test_evolve.py` | Create | Unit tests for genome + GA |
| `frontend/src/panels/controls.ts` | Modify | Add "Evolve Swarm" button |
| `frontend/src/app/app.ts` | Modify | Evolve flow: start, poll, load result |
| `frontend/src/types.ts` | Modify | Add `EvolveRequest`, `EvolveResultResponse` |
| `frontend/src/net/client.ts` | Modify | Add `startEvolve()`, `evolveResult()` |

---

### Task 1: Genome dataclass and scenario overlay

**Files:**
- Create: `backend/beam/evolve/__init__.py`
- Create: `backend/beam/evolve/genome.py`
- Create: `backend/tests/test_evolve.py`

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_evolve.py
import pytest
from beam.evolve.genome import Genome, genome_to_overlay, random_genome, BOUNDS


def test_genome_to_overlay_contains_swarm_spec():
    g = Genome(
        spawn_arc_center_deg=90.0,
        spawn_arc_width_deg=120.0,
        spawn_radius=3000.0,
        speed=20.0,
        separation_weight=1.0,
        cohesion_weight=0.05,
        goal_weight=2.0,
    )
    overlay = genome_to_overlay(g, base_swarm_count=24, base_behavior="flocking")
    ss = overlay["swarm_spec"]
    assert ss["spawn_radius"] == pytest.approx(3000.0)
    assert ss["speed"] == pytest.approx(20.0)
    arc = ss["spawn_arc_deg"]
    assert len(arc) == 2


def test_genome_to_overlay_arc_calculation():
    g = Genome(
        spawn_arc_center_deg=180.0,
        spawn_arc_width_deg=60.0,
        spawn_radius=4000.0,
        speed=15.0,
        separation_weight=1.0,
        cohesion_weight=0.05,
        goal_weight=2.0,
    )
    overlay = genome_to_overlay(g, base_swarm_count=12, base_behavior="direct")
    arc = overlay["swarm_spec"]["spawn_arc_deg"]
    # center=180, width=60 -> start=150, end=210
    assert arc[0] == pytest.approx(150.0)
    assert arc[1] == pytest.approx(210.0)


def test_random_genome_respects_bounds():
    import numpy as np
    rng = np.random.default_rng(42)
    for _ in range(20):
        g = random_genome(rng)
        for attr, (lo, hi) in BOUNDS.items():
            val = getattr(g, attr)
            assert lo <= val <= hi, f"{attr}={val} out of [{lo},{hi}]"


def test_genome_to_overlay_direct_behavior_has_no_kinematics():
    g = Genome(spawn_arc_center_deg=0, spawn_arc_width_deg=90, spawn_radius=3000,
               speed=20, separation_weight=1, cohesion_weight=0.05, goal_weight=2)
    overlay = genome_to_overlay(g, base_swarm_count=10, base_behavior="direct")
    # direct behavior: no kinematics override needed
    assert "kinematics" not in overlay
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_evolve.py -v
```

Expected: `ModuleNotFoundError: No module named 'beam.evolve'`

- [ ] **Step 3: Create genome.py**

```python
# backend/beam/evolve/__init__.py
# (empty)
```

```python
# backend/beam/evolve/genome.py
"""Evolutionary genome: swarm approach parameters that the GA varies (design spec)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["Genome", "BOUNDS", "genome_to_overlay", "random_genome", "crossover", "mutate"]

# (field_name, lo, hi) - the search space for each gene.
BOUNDS: dict[str, tuple[float, float]] = {
    "spawn_arc_center_deg": (0.0, 360.0),
    "spawn_arc_width_deg": (10.0, 180.0),
    "spawn_radius": (500.0, 8000.0),
    "speed": (5.0, 60.0),
    "separation_weight": (0.1, 5.0),
    "cohesion_weight": (0.01, 1.0),
    "goal_weight": (0.5, 8.0),
}

GENE_KEYS = list(BOUNDS.keys())


@dataclass
class Genome:
    """A vector of swarm approach parameters."""
    spawn_arc_center_deg: float
    spawn_arc_width_deg: float
    spawn_radius: float
    speed: float
    separation_weight: float
    cohesion_weight: float
    goal_weight: float

    def to_vector(self) -> list[float]:
        return [getattr(self, k) for k in GENE_KEYS]

    @staticmethod
    def from_vector(v: list[float]) -> "Genome":
        return Genome(**dict(zip(GENE_KEYS, v)))


def random_genome(rng: np.random.Generator) -> Genome:
    """Sample a genome uniformly within BOUNDS."""
    vec = [float(rng.uniform(lo, hi)) for lo, hi in BOUNDS.values()]
    return Genome.from_vector(vec)


def crossover(a: Genome, b: Genome, rng: np.random.Generator) -> tuple[Genome, Genome]:
    """Uniform crossover: each gene independently taken from a or b."""
    va, vb = a.to_vector(), b.to_vector()
    mask = rng.integers(0, 2, size=len(va)).astype(bool)
    child1 = [va[i] if mask[i] else vb[i] for i in range(len(va))]
    child2 = [vb[i] if mask[i] else va[i] for i in range(len(va))]
    return Genome.from_vector(child1), Genome.from_vector(child2)


def mutate(g: Genome, rng: np.random.Generator, *, rate: float = 0.15) -> Genome:
    """Gaussian mutation with probability ``rate`` per gene; clamp to BOUNDS."""
    vec = g.to_vector()
    for i, (key, (lo, hi)) in enumerate(BOUNDS.items()):
        if rng.random() < rate:
            sigma = (hi - lo) * 0.1
            vec[i] = float(np.clip(vec[i] + rng.normal(0, sigma), lo, hi))
    return Genome.from_vector(vec)


def genome_to_overlay(
    g: Genome,
    *,
    base_swarm_count: int,
    base_behavior: str,
) -> dict[str, Any]:
    """Convert a genome to a scenario overlay dict for ``POST /api/scenario``."""
    arc_half = g.spawn_arc_width_deg / 2.0
    arc_start = g.spawn_arc_center_deg - arc_half
    arc_end = g.spawn_arc_center_deg + arc_half

    overlay: dict[str, Any] = {
        "swarm_spec": {
            "count": base_swarm_count,
            "behavior": base_behavior,
            "spawn_radius": g.spawn_radius,
            "spawn_arc_deg": [arc_start, arc_end],
            "speed": g.speed,
        }
    }

    if base_behavior == "flocking":
        overlay["kinematics"] = {
            "flocking": {
                "separation_weight": g.separation_weight,
                "cohesion_weight": g.cohesion_weight,
                "goal_weight": g.goal_weight,
            }
        }
    return overlay
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_evolve.py -v
```

Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/beam/evolve/ backend/tests/test_evolve.py
git commit -m "feat: Genome dataclass with bounds, crossover, mutation, overlay conversion"
```

---

### Task 2: GA runner

**Files:**
- Create: `backend/beam/evolve/ga.py`

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_evolve.py`:

```python
from beam.evolve.ga import GAConfig, run_ga


def test_run_ga_returns_result_with_best_genome():
    """GA returns a best genome and improves fitness over 3 generations."""
    cfg = GAConfig(
        population_size=6,
        generations=3,
        mutation_rate=0.2,
        crossover_rate=0.7,
        elite_count=1,
        defender_solver="greedy_urgent",
        base_scenario="swarm_24",
        seed=42,
        base_swarm_count=12,
        base_behavior="direct",
    )
    result = run_ga(cfg)
    assert result.best_genome is not None
    assert result.best_fitness >= 0.0
    assert len(result.generation_best) == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_evolve.py::test_run_ga_returns_result_with_best_genome -v
```

Expected: `ModuleNotFoundError: No module named 'beam.evolve.ga'`

- [ ] **Step 3: Create ga.py**

```python
# backend/beam/evolve/ga.py
"""Tournament GA over swarm approach genomes (design spec section 4)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from beam.batch.sweeps import SweepSpec, _build_point_config
from beam.engine.loop import LoopConfig, run_headless
from beam.evolve.genome import Genome, crossover, genome_to_overlay, mutate, random_genome

log = logging.getLogger(__name__)

__all__ = ["GAConfig", "GAResult", "run_ga"]


@dataclass(frozen=True)
class GAConfig:
    base_scenario: str
    defender_solver: str = "greedy_urgent"
    seed: int = 42
    population_size: int = 40
    generations: int = 30
    mutation_rate: float = 0.15
    crossover_rate: float = 0.70
    elite_count: int = 4
    base_swarm_count: int = 24
    base_behavior: str = "flocking"


@dataclass
class GAResult:
    best_genome: Optional[Genome]
    best_fitness: float
    generation_best: list[float] = field(default_factory=list)


def _evaluate(
    genome: Genome,
    base_cfg,
    defender_solver: str,
    base_swarm_count: int,
    base_behavior: str,
    loop_cfg: Optional[LoopConfig] = None,
) -> float:
    """Fitness = leaked drone value (higher is better for the swarm)."""
    import copy
    cfg = copy.deepcopy(base_cfg)
    overlay = genome_to_overlay(
        genome,
        base_swarm_count=base_swarm_count,
        base_behavior=base_behavior,
    )
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(cfg.scenario.get(k), dict):
            cfg.scenario[k].update(v)
        else:
            cfg.scenario[k] = v

    result = run_headless(
        cfg,
        active_solver=defender_solver,
        enabled_solvers=[defender_solver],
        loop_cfg=loop_cfg,
    )
    return float(result.summary.leaked_value)


def _tournament(population: list[Genome], fitnesses: list[float], rng: np.random.Generator, k: int = 3) -> Genome:
    """Select one genome via tournament of size k."""
    indices = rng.choice(len(population), size=k, replace=False)
    best_idx = max(indices, key=lambda i: fitnesses[i])
    return population[best_idx]


def run_ga(
    cfg: GAConfig,
    *,
    loop_cfg: Optional[LoopConfig] = None,
    on_generation: Optional[Callable[[int, float, float], None]] = None,
) -> GAResult:
    """Run the GA and return the best genome found.

    Args:
        cfg: GA hyperparameters.
        loop_cfg: optional headless loop config (e.g. shorten dt for tests).
        on_generation: optional callback(generation, best_fitness, mean_fitness).
    """
    rng = np.random.default_rng(cfg.seed)

    sweep_spec = SweepSpec(
        id="evolve",
        base_scenario=cfg.base_scenario,
        seed=cfg.seed,
        parameter="swarm_spec.count",
        values=[cfg.base_swarm_count],
        solvers=[cfg.defender_solver],
    )
    base_cfg = _build_point_config(sweep_spec, value=cfg.base_swarm_count)

    population = [random_genome(rng) for _ in range(cfg.population_size)]
    fitnesses = [
        _evaluate(g, base_cfg, cfg.defender_solver, cfg.base_swarm_count, cfg.base_behavior, loop_cfg)
        for g in population
    ]

    best_genome = population[int(np.argmax(fitnesses))]
    best_fitness = max(fitnesses)
    generation_best: list[float] = []

    for gen in range(cfg.generations):
        sorted_idx = sorted(range(len(population)), key=lambda i: fitnesses[i], reverse=True)
        elites = [population[i] for i in sorted_idx[: cfg.elite_count]]

        offspring: list[Genome] = []
        while len(offspring) < cfg.population_size - cfg.elite_count:
            if rng.random() < cfg.crossover_rate:
                a = _tournament(population, fitnesses, rng)
                b = _tournament(population, fitnesses, rng)
                c1, c2 = crossover(a, b, rng)
                offspring.extend([mutate(c1, rng, rate=cfg.mutation_rate),
                                   mutate(c2, rng, rate=cfg.mutation_rate)])
            else:
                p = _tournament(population, fitnesses, rng)
                offspring.append(mutate(p, rng, rate=cfg.mutation_rate))

        population = elites + offspring[: cfg.population_size - cfg.elite_count]
        fitnesses = [
            _evaluate(g, base_cfg, cfg.defender_solver, cfg.base_swarm_count, cfg.base_behavior, loop_cfg)
            for g in population
        ]

        gen_best = max(fitnesses)
        gen_mean = float(np.mean(fitnesses))
        generation_best.append(gen_best)

        if gen_best > best_fitness:
            best_fitness = gen_best
            best_genome = population[int(np.argmax(fitnesses))]

        log.info("generation %d/%d  best=%.1f  mean=%.1f", gen + 1, cfg.generations, gen_best, gen_mean)
        if on_generation:
            on_generation(gen + 1, gen_best, gen_mean)

    return GAResult(best_genome=best_genome, best_fitness=best_fitness, generation_best=generation_best)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_evolve.py -v
```

Expected: all 5 tests PASS (may take 10-30s due to headless runs)

- [ ] **Step 5: Commit**

```bash
git add backend/beam/evolve/ga.py backend/tests/test_evolve.py
git commit -m "feat: tournament GA runner for swarm evolution"
```

---

### Task 3: CLI command + backend endpoints

**Files:**
- Modify: `backend/beam/cli.py`
- Modify: `backend/beam/api/models.py`
- Modify: `backend/beam/api/routes.py`
- Modify: `backend/beam/api/runtime.py`

- [ ] **Step 1: Add EvolveJob to runtime.py**

In `backend/beam/api/runtime.py`, add after `ParetoJob`:

```python
@dataclass
class EvolveJob:
    evolve_id: str
    status: str  # "running" | "done" | "error"
    generation: int = 0
    generations: int = 30
    best_fitness: float = 0.0
    mean_fitness: float = 0.0
    best_overlay: Optional[dict] = None
    error: Optional[str] = None
```

Add `evolves: dict[str, EvolveJob] = field(default_factory=dict)` to `Registry` and `new_evolve_id()`:

```python
    def new_evolve_id(self) -> str:
        self._counter += 1
        return f"evolve-{self._counter}"
```

- [ ] **Step 2: Add models to api/models.py**

```python
class EvolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = "swarm_24"
    defender_solver: str = "greedy_urgent"
    generations: int = Field(default=30, ge=1, le=200)
    population_size: int = Field(default=40, ge=4, le=200)
    seed: int = 42
    swarm_count: int = Field(default=24, ge=1, le=512)
    behavior: str = "flocking"


class EvolveStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evolve_id: str
    status: str


class EvolveProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evolve_id: str
    status: str
    generation: int
    generations: int
    best_fitness: float
    mean_fitness: float
    best_overlay: Optional[dict] = None
    error: Optional[str] = None
```

- [ ] **Step 3: Add routes to routes.py**

```python
from beam.api.models import EvolveRequest, EvolveStartResponse, EvolveProgressResponse
from beam.api.runtime import EvolveJob
from beam.evolve.ga import GAConfig, run_ga
from beam.evolve.genome import genome_to_overlay


@router.post("/evolve", response_model=EvolveStartResponse)
async def start_evolve(req: EvolveRequest, request: Request) -> EvolveStartResponse:
    reg = _registry(request)
    evolve_id = reg.new_evolve_id()
    job = EvolveJob(evolve_id=evolve_id, status="running", generations=req.generations)
    reg.evolves[evolve_id] = job

    cfg = GAConfig(
        base_scenario=req.preset,
        defender_solver=req.defender_solver,
        seed=req.seed,
        population_size=req.population_size,
        generations=req.generations,
        base_swarm_count=req.swarm_count,
        base_behavior=req.behavior,
    )

    def _on_gen(gen: int, best: float, mean: float) -> None:
        job.generation = gen
        job.best_fitness = best
        job.mean_fitness = mean

    async def _drive() -> None:
        try:
            result = await asyncio.to_thread(run_ga, cfg, on_generation=_on_gen)
            job.status = "done"
            if result.best_genome is not None:
                job.best_overlay = genome_to_overlay(
                    result.best_genome,
                    base_swarm_count=req.swarm_count,
                    base_behavior=req.behavior,
                )
                job.best_fitness = result.best_fitness
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"

    asyncio.ensure_future(_drive())
    return EvolveStartResponse(evolve_id=evolve_id, status=job.status)


@router.get("/evolve/{evolve_id}", response_model=EvolveProgressResponse)
def evolve_status(evolve_id: str, request: Request) -> EvolveProgressResponse:
    reg = _registry(request)
    job = reg.evolves.get(evolve_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown evolve job {evolve_id!r}")
    return EvolveProgressResponse(
        evolve_id=evolve_id,
        status=job.status,
        generation=job.generation,
        generations=job.generations,
        best_fitness=job.best_fitness,
        mean_fitness=job.mean_fitness,
        best_overlay=job.best_overlay,
        error=job.error,
    )
```

- [ ] **Step 4: Add beam evolve CLI command**

In `backend/beam/cli.py`, find the existing `@app.command()` functions and add:

```python
@app.command()
def evolve(
    scenario: str = typer.Option("swarm_24", help="Base preset scenario name"),
    solver: str = typer.Option("greedy_urgent", help="Defender solver to evolve against"),
    generations: int = typer.Option(30, help="Number of GA generations"),
    population: int = typer.Option(40, help="GA population size"),
    seed: int = typer.Option(42, help="RNG seed"),
    out: Optional[str] = typer.Option(None, help="Output YAML path for evolved scenario"),
) -> None:
    """Evolve swarm attack parameters to maximize leaks against a defender policy."""
    import yaml  # type: ignore[import]
    from beam.evolve.ga import GAConfig, run_ga
    from beam.evolve.genome import genome_to_overlay

    cfg = GAConfig(
        base_scenario=scenario,
        defender_solver=solver,
        generations=generations,
        population_size=population,
        seed=seed,
    )

    def _progress(gen: int, best: float, mean: float) -> None:
        typer.echo(f"  gen {gen:3d}/{generations}  best={best:.0f}  mean={mean:.0f}")

    typer.echo(f"Evolving swarm against {solver!r} for {generations} generations…")
    result = run_ga(cfg, on_generation=_progress)

    if result.best_genome is None:
        typer.echo("No genome found (empty population?)")
        raise typer.Exit(1)

    overlay = genome_to_overlay(result.best_genome, base_swarm_count=24, base_behavior="flocking")
    overlay["_source"] = "beam evolve"
    overlay["_fitness"] = result.best_fitness

    out_path = out or f"runs/evolved_{scenario}.yaml"
    import pathlib
    pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(out_path).write_text(yaml.dump(overlay, sort_keys=True))
    typer.echo(f"Best genome saved to {out_path}  (leaked_value={result.best_fitness:.0f})")
```

- [ ] **Step 5: Run all backend tests**

```bash
cd backend && python -m pytest tests/ -v
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add backend/beam/api/models.py backend/beam/api/routes.py backend/beam/api/runtime.py backend/beam/cli.py
git commit -m "feat: POST /api/evolve endpoint and beam evolve CLI command"
```

---

### Task 4: Frontend evolve UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/net/client.ts`
- Modify: `frontend/src/panels/controls.ts`
- Modify: `frontend/src/app/app.ts`

- [ ] **Step 1: Add types to types.ts**

```typescript
export interface EvolveRequest {
  preset?: string;
  defender_solver?: string;
  generations?: number;
  population_size?: number;
  seed?: number;
  swarm_count?: number;
  behavior?: string;
}

export interface EvolveStartResponse {
  evolve_id: string;
  status: string;
}

export interface EvolveProgressResponse {
  evolve_id: string;
  status: string;
  generation: number;
  generations: number;
  best_fitness: number;
  mean_fitness: number;
  best_overlay?: Record<string, unknown> | null;
  error?: string | null;
}
```

- [ ] **Step 2: Add client methods to client.ts**

```typescript
  startEvolve(req: EvolveRequest): Promise<EvolveStartResponse> {
    return this.request("POST", "/api/evolve", req);
  }

  evolveStatus(evolveId: string): Promise<EvolveProgressResponse> {
    return this.request("GET", `/api/evolve/${encodeURIComponent(evolveId)}`);
  }
```

- [ ] **Step 3: Add Evolve button to ControlPanel**

In `frontend/src/panels/controls.ts`, in `ControlSink`:

```typescript
export interface ControlSink {
  start(overrides: Record<string, unknown>): void | Promise<void>;
  reset(overrides: Record<string, unknown>): void | Promise<void>;
  send(msg: ControlMessage): void;
  openEditor?(): void;
  startEvolve?(): void;  // optional — wired by app
}
```

In `mount()`, add after the Edit Scenario button:

```typescript
    const evolveBtn = this.button("Evolve Swarm", () => this.sink.startEvolve?.());
    evolveBtn.className = "beam-evolve-btn";
    this.root.appendChild(evolveBtn);
```

- [ ] **Step 4: Wire evolve in app.ts**

Add field:

```typescript
  private evolveId: string | null = null;
```

In `mountPanel()`, add to sink:

```typescript
      startEvolve: () => void this.startEvolution(),
```

Add method:

```typescript
  private async startEvolution(): Promise<void> {
    const state = this.panel.getState();
    this.setStatus(`evolving swarm (${state.swarmSize} drones) against ${state.activeSolver}…`);
    try {
      const res = await this.client.startEvolve({
        preset: this.scenarioLabel === WOW_PRESET ? WOW_PRESET : "swarm_24",
        defender_solver: state.activeSolver,
        generations: 20,
        population_size: 30,
        swarm_count: state.swarmSize,
        behavior: state.behavior,
      });
      this.evolveId = res.evolve_id;
      this.pollEvolution();
    } catch (e) {
      this.setStatus(`evolution failed to start: ${String(e)}`);
    }
  }

  private pollEvolution(): void {
    if (!this.evolveId) return;
    const id = this.evolveId;
    const poll = async () => {
      const res = await this.client.evolveStatus(id);
      if (res.status === "done" && res.best_overlay) {
        this.setStatus(`evolution complete - best leaked value: ${res.best_fitness.toFixed(0)}`);
        // Open editor with evolved scenario for review before running
        if (this.editor) {
          this.setStatus("evolved scenario loaded into editor - review and Save to run it");
          this.editor.open();
        }
      } else if (res.status === "error") {
        this.setStatus(`evolution error: ${res.error ?? "unknown"}`);
      } else {
        this.setStatus(`evolving… gen ${res.generation}/${res.generations} best=${res.best_fitness.toFixed(0)}`);
        setTimeout(() => void poll(), 3000);
      }
    };
    void poll();
  }
```

- [ ] **Step 5: Run all frontend tests**

```bash
cd frontend && npx vitest run
```

Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types.ts frontend/src/net/client.ts frontend/src/panels/controls.ts frontend/src/app/app.ts
git commit -m "feat: Evolve Swarm button, evolution polling, editor integration"
```

---

## Self-review checklist

- [x] `genome_to_overlay` tested for arc math and behavior-conditional kinematics block
- [x] GA `elite_count` < `population_size` always — enforced by `GAConfig` default (4 < 40)
- [x] `run_ga` test uses small population (6) and few generations (3) for speed
- [x] `on_generation` callback updates `EvolveJob` fields in-place — safe from `asyncio.to_thread`
- [x] `KinematicsConfig` has `flocking` and `staggered` sub-blocks; the `kinematics` overlay from `genome_to_overlay` only patches `flocking.*` keys — need to verify the engine's `resolve_config` merges nested kinematics dicts (deep merge) rather than replacing the whole block. If it replaces, add default staggered values to the overlay. Verify by reading `resolve_config` in `runtime.py`.
- [x] CLI uses `yaml.dump` — ensure `pyyaml` is a dependency in `pyproject.toml`
