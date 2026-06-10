# BEAM Roadmap - Phase 4 + Extensions Design Spec

| Field | Value |
|---|---|
| Date | 2026-06-09 |
| Status | Approved |
| Author | Vlad |
| Scope | All remaining roadmap items: shareable links, scenario editor, Pareto multi-obj, adaptive swarm AI, 3D view, Rust hot loop |

---

## Build order

Dependencies flow top to bottom. Each item is independently shippable.

| # | Feature | Effort | Depends on |
|---|---|---|---|
| 1 | Shareable run links | ~hours | nothing |
| 2 | Scenario editor UI | ~2 days | nothing |
| 3 | Multi-objective / Pareto front | ~3 days | nothing |
| 4 | Adaptive swarm AI (evolutionary) | ~1 week | scenario editor (nice-to-have) |
| 5 | 3D view (Three.js toggle) | ~2 weeks | nothing |
| 6 | Rust hot loop (PyO3) | ~2 weeks | nothing |

---

## 1. Shareable run links

### Goal
A user can copy a URL that, when opened, auto-loads and starts the exact same run configuration.

### Approach
Pure URL encoding - no backend persistence required.

**Encoding:** serialize `{ scenario_id: string, seed: number, solver: string, speed?: number }` as JSON, base64url-encode, append as `?run=<token>`.

- Named preset scenarios (e.g. `swarm_24`) encode as `scenario_id` only - server resolves them.
- Custom (editor-built) scenarios encode the full scenario config JSON inside the token.
- Token size stays under ~2KB for realistic scenarios (turrets + drone spec + weather).

**Frontend flow:**
1. On page load, check `window.location.search` for `?run=`.
2. Decode and validate. If invalid, silently ignore and show normal UI.
3. Pre-populate controls and auto-start the run.
4. "Share" button in the scoreboard generates the token from current run state and writes to clipboard.

**Backend change:** none. `POST /api/scenario` already accepts full scenario config; the share link can POST it on load if it's a custom scenario.

### Files to change
- `frontend/src/app/app.ts` - read param on init, expose share action
- `frontend/src/panels/controls.ts` - add Share button
- `frontend/src/app/runController.ts` - accept pre-loaded config path
- `frontend/src/net/client.ts` - encode/decode helpers

### Contract
- Valid token: run starts automatically, controls show loaded state.
- Invalid/expired token: silent fallback to default UI (no error modal).
- Custom scenario tokens are self-contained; no server state required.

---

## 2. Scenario editor UI

### Goal
Users can build and save custom scenarios without editing YAML directly.

### Approach
Slide-out drawer panel triggered by "Edit Scenario" button in the control panel. Two tabs: **Form** (structured fields) and **YAML** (read-only preview of generated config, with copy button).

**Form sections:**

| Section | Fields |
|---|---|
| Battery | Turret count (1-8), per-turret: position (x,y), power, slew rate, range max, thermal heat/cool rates |
| Swarm | Drone count, spawn geometry (arc/ring/wave), behavior (direct/flocking/staggered), drone class mix (slider: % quad_small vs fixed_wing), stagger interval |
| Environment | Weather (dropdown: clear/haze/rain/fog/dust), seed (number input + "randomize" button) |
| Timing | Decision period, sim speed default |

**Validation:** on submit, POST to `POST /api/scenario`. Backend returns validation errors (pydantic). Display inline. On success, close drawer and load the new scenario into controls.

**YAML tab:** generate the config YAML client-side from form state for preview. Does not call backend. Read-only; includes a "Copy YAML" button for power users.

**Persistence:** scenarios are not stored server-side beyond the current run. To persist, user uses the share link (feature 1) or copies the YAML.

### Files to change
- `frontend/src/panels/controls.ts` - add "Edit Scenario" button + drawer trigger
- `frontend/src/panels/editor.ts` - new file, the drawer form component
- `frontend/src/panels/index.ts` - export editor

### Schema alignment
Form fields map 1:1 to the existing `Scenario` pydantic model. No backend schema changes.

---

## 3. Multi-objective / Pareto front

### Goal
Surface the tradeoff between minimizing leaked value and minimizing cost as a Pareto frontier, making the cost-exchange story quantitatively rigorous.

### Approach
**Batch sweep mode** with weighted-sum scalarization.

**Backend:**
- New endpoint: `POST /api/batch/pareto` - accepts `{ scenario_id, seed, solver, n_points, lambda_range }`.
- Runs the scenario headlessly N times with different λ values (default N=20, λ ∈ [0.0, 1.0]).
- At each λ, the CP-SAT objective becomes: `maximize λ·value_saved - (1-λ)·total_cost` (normalized to comparable scales via config constants).
- Returns array of `{ lambda, value_saved, total_cost, leaked_value, kills }` - the Pareto curve data.
- Streams progress via WebSocket `type: "pareto_progress"` messages.

**Frontend:**
- New "Pareto Analysis" tab in the right dashboard panel (alongside Gap and Breakeven).
- "Run Pareto Analysis" button triggers the sweep.
- Chart: scatter plot of (total_cost, value_saved) points, connected as a frontier curve. Hover tooltip shows λ, kills, leaks. A star marks the current solver's operating point from the live run.
- "Efficient frontier" region shaded.

**Normalization:** value_saved is in drone-value units (dollars); total_cost is in operating cost units. Scale factor exposed in config (`cost.pareto_value_scale`, default 1.0) so the λ sweep sweeps meaningful tradeoffs.

**CP-SAT objective extension:** the existing CP-SAT solver in `solvers/cp_sat.py` gets a `cost_weight: float = 0.0` parameter. When non-zero, the model subtracts a cost term from the objective. Default 0.0 = existing behavior unchanged.

### Files to change
- `backend/beam/solvers/cp_sat.py` - add `cost_weight` parameter
- `backend/beam/api/routes.py` - add `POST /api/batch/pareto` endpoint
- `backend/beam/batch/sweeps.py` - add `pareto_sweep()` function
- `backend/beam/schemas/models.py` - add `ParetoPoint`, `ParetoRequest`, `ParetoResult`
- `frontend/src/charts/pareto.ts` - new chart
- `frontend/src/charts/index.ts` - export pareto
- `frontend/src/panels/controls.ts` - Pareto Analysis button

---

## 4. Adaptive swarm AI (evolutionary)

### Goal
A GA-based optimizer that evolves swarm approach parameters to maximize leaks against a chosen defender policy, producing a "worst-case" scenario YAML.

### Approach
**Offline evolutionary search** - new `beam evolve` CLI command and `POST /api/evolve` endpoint.

**Genome:** vector of swarm approach parameters:
```
[ spawn_arc_center_deg, spawn_arc_width_deg, spawn_radius,
  approach_jitter, velocity_spread, stagger_interval,
  flocking_separation, flocking_cohesion, flocking_alignment ]
```

**Fitness function:** run the scenario headlessly with the genome's swarm params against the specified defender solver. Fitness = total leaked value. Higher = better for the swarm.

**GA config (in `config/defaults.yaml`):**
```yaml
evolve:
  population_size: 40
  generations: 30
  mutation_rate: 0.15
  crossover_rate: 0.7
  elite_fraction: 0.1
  defender_solver: greedy_urgent
```

**Engine hook:** `SwarmSpec` already carries behavior profile params. The evolver replaces these with the genome values per evaluation. No engine changes needed beyond ensuring `SwarmSpec` fields accept the genome params (verify and add any missing fields).

**CLI:** `beam evolve --scenario config/scenarios/swarm_24.yaml --solver greedy_urgent --generations 30 --out runs/evolved_swarm.yaml`

**API:** `POST /api/evolve { scenario_id, solver, generations }` -> `evolve_id`. WebSocket streams `type: "evolve_progress" { generation, best_fitness, mean_fitness }`. `GET /api/evolve/{id}/result` returns the evolved scenario config.

**Frontend:** "Evolve Swarm" button in control panel (below scenario controls). Progress bar and fitness chart stream during evolution. "Load Evolved Scenario" button appears on completion. Evolved scenario goes directly into the scenario editor drawer for review before running.

**Determinism:** evolution uses the scenario seed as the base RNG seed, offset per generation to avoid correlated evaluations.

### Files to change
- `backend/beam/evolve/` - new module: `ga.py`, `genome.py`, `__init__.py`
- `backend/beam/api/routes.py` - add evolve endpoints
- `backend/beam/schemas/models.py` - add `EvolveRequest`, `EvolveProgress`, `EvolveResult`
- `backend/beam/cli.py` - add `beam evolve` command
- `backend/beam/engine/kinematics.py` - verify SwarmSpec fields cover genome params, add if missing
- `frontend/src/panels/controls.ts` - Evolve button + progress display
- `frontend/src/charts/fitness.ts` - new fitness-over-generations chart

---

## 5. 3D view (Three.js, alongside 2D)

### Goal
An optional 3D battlefield view that renders the same telemetry with elevation, 3D geometry, and orbit camera controls. Toggleable; 2D remains the default.

### Approach
**Three.js r165+** added alongside existing Pixi.js. Both renderers subscribe to the same telemetry stream. Toggle button in the scoreboard bar switches the visible canvas element; the hidden renderer pauses updates to save GPU work.

**Scene:**
| Element | 3D representation |
|---|---|
| Terrain | `PlaneGeometry(10000, 10000)` with grid texture, dark tactical color |
| Asset | `CylinderGeometry` with emissive emerald glow |
| Turret | Base `BoxGeometry` + barrel `CylinderGeometry` on pivot; barrel rotates to aim azimuth + elevation |
| Drone | `ConeGeometry` pointing in velocity direction; colored by value class (amber=high, white=low); hp_frac drives opacity |
| Beam | `Line2` (thick line via `LineMaterial`) from turret tip to drone; power_frac drives intensity and color |
| Kill | Particle burst at drone position (instanced quads, 20 particles, 0.5s lifetime) |
| Leak | Red flash at asset + screen-edge vignette |

**Drone elevation:** add optional `z` field to the `DroneFrame` telemetry schema (default `0`). Scenario spec gets optional `drone_altitude: float` (default `200`). Drones spawn at that altitude and descend linearly to 0 as they approach the asset (simple linear descent over TTI). This adds no physics complexity but gives the 3D scene depth.

**Camera:** `OrbitControls` with default isometric preset (elevation 45deg, azimuth 45deg). Reset-to-default button in 3D mode.

**Performance:** instanced meshes for drones (single draw call for all drones of same class). Beams use `InstancedMesh` for the line segments. Target: 500+ drones at 60fps on a mid-range GPU.

**No terrain LOS masking** - flat terrain only. LOS masking deferred (requires raycasting against terrain heightmap; the 2D model has no terrain obstacles).

**Telemetry schema change:** `z` is optional, backward-compatible. 2D renderer ignores it.

### Files to change
- `frontend/src/render/battlefield3d.ts` - new Three.js renderer
- `frontend/src/render/index.ts` - export both renderers, toggle logic
- `frontend/src/panels/scoreboard.ts` - 2D/3D toggle button
- `frontend/src/main.ts` - init both renderers, wire toggle
- `backend/beam/schemas/models.py` - add optional `z` to `DroneFrame`
- `backend/beam/engine/kinematics.py` - compute drone z from altitude config
- `backend/beam/engine/telemetry.py` - include z in frame output
- `frontend/package.json` - add `three`, `@types/three`

---

## 6. Rust hot loop (PyO3)

### Goal
Replace the numpy physics hot path with a compiled Rust extension for 10-50x speedup at 1000+ drone scale.

### Approach
**PyO3 extension** (`maturin`-based), opt-in install. Python falls back to pure-Python/numpy if Rust extension is not compiled.

**Crate:** `backend/beam_physics/` - a Rust crate using `pyo3` and `ndarray`.

**Functions to Rust-ify** (the tight inner loops that dominate at scale):

| Python function | Location | What it does |
|---|---|---|
| `beer_lambert_batch` | `physics.py` | Vectorized attenuation over all (turret, drone) pairs |
| `dwell_to_kill_matrix` | `physics.py` | N_turrets × N_drones dwell time matrix |
| `slew_time_matrix` | `physics.py` | N_turrets × N_targets × N_targets slew time tensor |
| `update_positions` | `kinematics.py` | Vectorized drone position update (direct + flocking) |
| `compute_tti` | `kinematics.py` | Time-to-impact for all live drones |

**Rust interface:**
```rust
#[pyfunction]
fn beer_lambert_batch(ranges: PyReadonlyArray2<f64>, alpha: f64) -> PyResult<PyArray2<f64>>;

#[pyfunction]
fn dwell_to_kill_matrix(ranges: PyReadonlyArray2<f64>, alpha: f64, power: f64,
                         hardness: PyReadonlyArray1<f64>, eta_base: f64,
                         eta_falloff: f64) -> PyResult<PyArray2<f64>>;
// etc.
```

**Python import pattern:**
```python
try:
    from beam_physics import beer_lambert_batch, dwell_to_kill_matrix, slew_time_matrix
    _RUST = True
except ImportError:
    _RUST = False
    # pure numpy fallbacks defined below
```

**Build integration:**
- `backend/pyproject.toml` gets a `[rust]` optional dependency group.
- `maturin develop` in the `beam_physics/` crate builds the `.pyd` / `.so` into the venv.
- CI builds both paths: with and without Rust, tests must pass on both.
- `beam_physics/Cargo.toml` pins `pyo3`, `ndarray`, `numpy` (ndarray bridge).

**Performance target:** at 1000 drones, epoch physics time drops from ~50ms (numpy) to ~2-5ms (Rust SIMD via `ndarray`).

**SIMD:** `ndarray` with the `blas` feature or manual `std::simd` for the tight multiply-accumulate loops. Start with safe ndarray; add explicit SIMD only if profiling shows it's needed.

### Files to create
- `backend/beam_physics/Cargo.toml`
- `backend/beam_physics/src/lib.rs`
- `backend/beam_physics/src/physics.rs`
- `backend/beam_physics/src/kinematics.rs`

### Files to change
- `backend/beam/engine/physics.py` - add try/except import + fallback wrappers
- `backend/beam/engine/kinematics.py` - same pattern
- `backend/pyproject.toml` - add `[project.optional-dependencies] rust = [...]`, maturin build config
- `.github/workflows/` - add Rust build step, test both paths

---

## Testing strategy

| Feature | Tests |
|---|---|
| Shareable links | Unit: encode→decode round-trip; edge: max-size custom scenario token; e2e: load page with `?run=`, verify run starts |
| Scenario editor | Unit: form→schema mapping; integration: POST to backend, validate errors surface |
| Pareto | Unit: pareto_sweep returns N points; property: all points on or inside the frontier; regression: deterministic given seed |
| Adaptive swarm AI | Unit: GA produces valid genomes; integration: fitness increases over generations on swarm_24; property: evolved scenario is valid YAML |
| 3D view | Unit: renderer initializes without crash; visual: screenshot comparison (optional); integration: telemetry updates move drone meshes |
| Rust hot loop | Unit: Rust output matches numpy output to float tolerance; benchmark: Rust >5x faster at 500 drones; CI: both import paths pass full test suite |

---

## Config additions

```yaml
# config/defaults.yaml additions

sim:
  drone_altitude: 200.0        # AGL meters for 3D view, ignored in 2D

evolve:
  population_size: 40
  generations: 30
  mutation_rate: 0.15
  crossover_rate: 0.70
  elite_fraction: 0.10
  defender_solver: greedy_urgent

cost:
  pareto_value_scale: 1.0      # scale factor to normalize value vs cost for lambda sweep
  pareto_n_points: 20          # number of Pareto curve points
```

---

## Non-goals (explicitly out of scope)

- Terrain LOS masking in 3D (flat terrain only)
- Multiplayer / accounts / server-side scenario persistence
- RL-based swarm AI (evolutionary only)
- True multi-objective CP-SAT (Pareto via weighted-sum scalarization only)
- Rust sidecar / IPC (PyO3 inline only)
- Video export
