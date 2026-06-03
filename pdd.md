# BEAM — Product Development Document

> **Project name:** `BEAM` — **B**attle **E**ngagement & **A**erial **M**itigation.

| Field | Value |
|---|---|
| Document type | Product Development Document (PDD) / Engineering Ground Truth |
| Status | Draft v1.0 — authoritative for development |
| Owner | Vlad (founder / architect) |
| Audience | Engineers implementing the system |
| Last updated | 2026-06-03 |
| Repo location | `/PRODUCT_DEVELOPMENT_DOCUMENT.md` (root) |

---

## 0. How to use this document

This is the single source of truth for what we are building and why. If code and this document disagree, that is a bug in one of them, and it must be reconciled, not ignored. Engineers should treat sections 6–10 (the model) as the contract: those define correctness. Sections 11–18 define structure and interfaces. Section 15 defines the build order and the acceptance criteria that gate each phase.

When in doubt, optimize for the three audiences this project must impress simultaneously: hackathon judges (live, legible demo), recruiters (visible engineering depth), and technically literate reviewers (defensible research grounding). A decision that serves all three wins. A decision that serves only flash loses.

---

## 1. Executive summary

BEAM is a simulation and analysis platform that models a **multi-turret high-energy laser battery defending a fixed asset against an incoming drone swarm**, and treats the targeting problem as what it actually is in the operations-research literature: a **Dynamic Weapon-Target Assignment (DWTA)** problem, layered on top of a **single-machine scheduling problem with sequence-dependent setup times and hard deadlines** at the per-turret level.

The platform does two headline things at once from one engine:

1. **Solver race with optimality gap.** Multiple assignment policies (greedy baselines, auction, metaheuristic, and an exact CP-SAT reference) run against the identical seeded scenario. The system streams, per decision epoch, each policy's objective value, its gap from the exact optimum, and its solve time. This visualizes the central real-time tradeoff of the field: good-enough-and-fast versus optimal-but-too-slow as the swarm scales.

2. **Cost-exchange breakeven curve.** A ledger tracks per-shot energy cost, amortized system capital cost, and maintenance, against cumulative value of drones destroyed. The output is the crossover point at which an expensive laser battery becomes net-positive against cheap drones.

This is explicitly a **software simulation and analysis tool**. It does not design, specify, or build laser hardware. All physical parameters are illustrative, configurable, and drawn from publicly available textbook relationships. The intellectual product is the model, the optimizer suite, and the analysis, not any weapon.

---

## 2. Background and research grounding

### 2.1 Real-world motivation

Directed-energy counter-drone systems are an active, deployed technology. The economic thesis behind them is a low marginal cost per engagement (publicly reported figures for fielded 100 kW-class systems are on the order of cents per shot) versus interceptor missiles costing hundreds of thousands to millions per shot. The constraints that make them interesting rather than trivially dominant are line-of-sight operation, atmospheric degradation in rain/fog/dust, the serial nature of a single beam (dwell time per kill), and sustainment. BEAM models exactly these tensions.

### 2.2 The named research problem

The targeting decision is the **Weapon-Target Assignment (WTA)** problem, a foundational problem in defense operations research. Key facts that anchor our design:

- WTA was **proved NP-complete by Lloyd and Witsenhausen (1986)**. There is no efficient exact algorithm at scale; the literature is built around this fact.
- The relevant variant is **Dynamic WTA (DWTA)**: weapons fire asynchronously and the outcome of each engagement is observed before the next assignment is committed. This maps exactly to a laser firing one target at a time and re-deciding as kills land.
- Because exact solutions do not scale (reported exact instances top out around 80×80 with multi-hour solve times), practical work uses **heuristics and metaheuristics**: genetic algorithms, simulated annealing, tabu search, particle swarm, ant colony, auction methods.
- The defining real-time constraint in the literature is producing an engagement solution **before the oncoming targets reach their goal**. This is the clock our solver race runs against.

### 2.3 Our novel framing

Standard WTA assumes a pool of weapons assigned in parallel. A **single laser is not parallel**: it is one beam that must service targets in sequence, each requiring a dwell time (processing time), with slew time between targets (sequence-dependent setup time), and each target carrying a deadline equal to its time-to-impact on the asset. That is precisely a **single-machine scheduling problem with sequence-dependent setup times and deadlines**, where the objective is to maximize the value of jobs completed before their deadlines. A multi-turret battery is then **parallel machines**, each running this sequencing problem, with assignment of targets to machines on top. This framing is the conceptual spine of BEAM and is what makes it defensible and distinctive.

---

## 3. Goals and non-goals

### 3.1 Goals

- Model a physically believable laser-vs-swarm engagement with the four constraints that make targeting hard (dwell, slew, thermal, atmosphere).
- Implement a pluggable suite of assignment solvers including an exact reference.
- Compute and stream optimality gap and solve time per epoch, live.
- Compute and stream the cost-exchange ledger and produce breakeven curves.
- Deliver a frontend that makes all of the above legible and compelling in real time.
- Be fully deterministic under a seed so any run is reproducible and any policy comparison is fair.

### 3.2 Non-goals

- **No hardware.** We do not design or specify laser hardware, optics, power systems, or beam directors.
- **No classified or real weapon-engineering parameters.** All physics constants are illustrative and configurable for simulation.
- No multiplayer, no accounts, no persistence backend beyond run artifacts (initially).
- No real sensor/radar modeling beyond an abstract detection range and track-quality parameter.
- Adaptive/learning swarm AI is explicitly out of scope for v1 (it is the lowest-ranked priority); the swarm uses scripted/flocking behavior. Hooks are left for it (section 17).

### 3.3 Scope priority order (from product decisions)

1. Laser targeting optimization (the solver suite + optimality gap) — primary.
2. Physics realism (the four constraints, atmospheric model) — secondary.
3. Visualization / demo (frontend carries) — tertiary but high-visibility.
4. Adaptive swarm AI — deferred to future work.

---

## 4. Success criteria

The project is successful when:

- **Demo:** A reviewer can, in under 60 seconds, watch a swarm attack, see turrets slew/dwell/kill, see one solver beat another, and read the breakeven crossover, with no explanation needed.
- **Depth:** The exact CP-SAT solver provably produces the optimal assignment for small instances, and the gap metric for heuristics is mathematically correct against it.
- **Rigor:** Every number on screen traces to a documented model in this PDD. No magic constants without a config entry and a rationale.
- **Reproducibility:** Re-running a scenario with the same seed and same solver yields byte-identical telemetry.
- **Performance:** The decision loop sustains the target epoch rate at balanced scale (section 16) without dropping frames in the frontend.

---

## 5. Personas and usage

- **The judge / reviewer (primary viewer):** Runs a preset scenario, toggles weather and swarm size, watches the solver race, leaves impressed. Needs zero setup.
- **The analyst (Vlad / power user):** Configures custom scenarios, runs batch sweeps to generate breakeven and gap-vs-scale curves, exports artifacts.
- **The engineer (contributor):** Adds a new solver by implementing one interface, adds a new weather profile by adding one config block.

---

## 6. Domain model and glossary

| Term | Definition |
|---|---|
| **Asset** | The fixed point being defended. If a drone reaches it, that is a leak (failure). |
| **Drone / target** | A hostile entity with position, velocity, value, and hardness (energy-to-kill). |
| **Swarm** | The full set of drones in a scenario, with a spawn geometry and behavior profile. |
| **Turret** | A single laser emitter. One beam. Has position, current aim, slew rate, power, thermal state. |
| **Battery** | The set of turrets defending the asset. |
| **Dwell** | Continuous time a beam must remain on a target to accumulate enough energy to kill it. |
| **Dwell-to-kill** | Required dwell duration for a given target at a given range and weather. |
| **Slew** | Angular repositioning of a turret between aim points; consumes slew time. |
| **Slew time** | Sequence-dependent setup cost between consecutive targets for one turret. |
| **Thermal budget** | Accumulated heat in a turret; firing adds heat, idle removes it; over a cap forces cooldown. |
| **Duty cycle** | Fraction of time a turret can fire before thermal limits force a pause. |
| **Attenuation** | Loss of delivered power over distance through atmosphere (Beer-Lambert). |
| **Time-to-impact (TTI)** | Time until a drone reaches the asset; serves as the target's hard deadline. |
| **Epoch / decision epoch** | A discrete moment at which the assignment problem is (re)solved. |
| **Assignment** | A mapping of turrets to targets (and per-turret target ordering) for the current epoch. |
| **Leak** | A drone that reaches the asset. The objective penalizes leaks by target value. |
| **Optimality gap** | `(optimal_objective - policy_objective) / optimal_objective`, per epoch. |
| **Engagement** | A turret committing to a target through to kill or abort. |

---

## 7. The optimization model (CONTRACT — correctness is defined here)

### 7.1 Notation

```
Targets    j ∈ T          (drones currently live and detected)
Turrets    i ∈ W          (laser emitters in the battery)
v_j        value of target j (cost of the drone; also penalty if it leaks)
TTI_j      time-to-impact of target j (its hard deadline)
d_ij(t)    dwell-to-kill time for turret i on target j at time t
                (depends on range and weather; see section 8)
s_i(a,b)   slew time for turret i moving aim from target a to target b
H_i        thermal state of turret i; H_max cap; cooldown rate
R_i        max effective range of turret i
LOS_ij     1 if turret i has line of sight to target j, else 0
```

### 7.2 Per-turret problem (single-machine scheduling)

For one turret `i`, given a set of assigned targets, choose an **order** π over those targets. Starting at the current aim, the completion time of the k-th target in the order is:

```
C(π_1) = s_i(aim, π_1) + d_i,π_1
C(π_k) = C(π_{k-1}) + s_i(π_{k-1}, π_k) + d_i,π_k
```

A target `j` is **successfully killed** iff `C(j) <= TTI_j` and the turret's thermal budget permits firing through `C(j)`. Maximize the value of targets completed before their deadlines:

```
maximize  Σ_j  v_j · [ C(j) <= TTI_j  AND  thermally_feasible(i, j, π) ]
```

This is single-machine weighted throughput / weighted number of on-time jobs with sequence-dependent setup times — NP-hard in general; we solve it exactly for small assigned sets and heuristically otherwise.

### 7.3 Battery problem (parallel machines + assignment)

Across the battery, choose both the assignment `x_ij ∈ {0,1}` (target j handled by turret i) and each turret's order, to maximize total protected value:

```
maximize   Σ_i Σ_j  v_j · kill_ij(x, π)

subject to
  Σ_i x_ij <= 1            for all j      (a target handled by at most one turret)
  x_ij <= LOS_ij           for all i,j    (only engage targets in line of sight)
  x_ij = 0 if range_ij > R_i              (only engage targets in range)
  per-turret schedule feasibility (7.2) including thermal budget
  kill_ij = 1 only if C_i(j) <= TTI_j
```

The **objective we report** is total value of drones killed before they leak. Equivalently we minimize leaked value `Σ_j v_j·(leaked_j)`. The cost ledger (section 10) is tracked alongside but is **not** part of the assignment objective in v1 (kills-before-leak is the optimization target; cost is an analysis output). A v2 multi-objective mode is noted in section 17.

### 7.4 The dynamic loop

The engagement is not solved once. At each decision epoch the live target set, geometry, TTIs, and thermal states have changed (kills landed, drones advanced). The loop:

```
every DECISION_PERIOD seconds (configurable epoch cadence):
  1. snapshot world state (targets, turrets, ranges, TTIs, thermal)
  2. for each enabled solver:
       solve the battery problem (7.3) -> assignment + per-turret orders
       record objective, solve_time
  3. compute optimality gap of each solver vs the exact reference
  4. the ACTIVE solver's assignment is applied to the turrets
  5. turrets execute (slew/dwell/fire) until next epoch
  6. resolve kills, update ledger, stream telemetry
```

The reference (CP-SAT) runs every epoch when scale permits; above a configurable target-count threshold it runs on a throttled cadence or on a snapshot, and the UI marks gap values as referenced-to-last-exact-solve so we never report a false gap.

---

## 8. Physics model (illustrative, configurable — simulation only)

All constants live in config (section 18). Defaults are plausible and internally consistent, not real weapon data.

### 8.1 Kinematics

2D plane in v1 (top-down), 3D-ready coordinates (z carried but unused). Drones advance toward the asset with a behavior profile:

- `direct`: straight-line toward asset at constant speed.
- `flocking`: boids-style separation/alignment/cohesion with a goal-seek toward the asset (Reynolds model). Parameters in config.
- `staggered`: waves spawned on a schedule from configurable arcs.

Turrets are stationary; only their aim vector moves, bounded by `slew_rate` (deg/s).

### 8.2 Atmospheric attenuation (Beer-Lambert)

Delivered intensity falls off with range through an extinction coefficient set by weather:

```
P_delivered(range) = P_emitted · exp( -alpha_weather · range )
```

`alpha_weather` is a per-profile constant (clear < haze < rain < fog < dust). This is the standard Beer-Lambert form and is the single most important "realism" lever: it makes range and weather dominate dwell-to-kill.

### 8.3 Dwell-to-kill

Energy to kill a target is `E_kill_j = hardness_j` (configurable per drone class). The rate of energy deposition is the delivered power minus a beam-spread/jitter efficiency factor:

```
deposition_rate_ij(t) = P_delivered(range_ij) · eta_track(track_quality, range)
d_ij(t) = E_kill_j / deposition_rate_ij(t)
```

`eta_track` degrades with range and lower track quality. Result: closer + clearer + better-tracked = faster kill. This is modeled at the energy-budget level only; we deliberately do not model material penetration physics.

### 8.4 Slew (setup) model

```
s_i(a, b) = angular_distance(aim_a, aim_b) / slew_rate_i  + settle_time_i
```

Sequence-dependent: the order of targets changes total setup time. This is what makes per-turret ordering a real scheduling decision, not just selection.

### 8.5 Thermal model

```
on fire:  H_i += heat_rate_i · dt
on idle:  H_i -= cool_rate_i · dt   (floored at 0)
if H_i >= H_max_i: turret forced to COOLDOWN until H_i <= H_resume_i
```

Caps sustained duty cycle and forces the optimizer to spread load across turrets and time.

### 8.6 Kill resolution

A target is killed at the instant cumulative delivered energy on it reaches `E_kill_j` while continuously engaged. Breaking the beam (re-slewing away) before kill loses progress unless `partial_energy_retention` is enabled in config (default: cooling/repair model loses progress to keep the scheduling problem honest).

---

## 9. Solver suite (the headline — pluggable)

### 9.1 Solver interface (contract)

Every solver implements one interface so they are interchangeable and comparable:

```python
class Solver(Protocol):
    name: str
    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Return assignment (turret->targets + per-turret order).
        Must respect deadline_ms wall-clock budget; return best-so-far if hit."""
```

`Assignment` includes the chosen `x_ij`, per-turret ordered target lists, and the solver's self-reported objective estimate.

### 9.2 Solvers to implement (in build order)

1. **Greedy: nearest-first.** Each turret takes the closest in-range target. Baseline floor.
2. **Greedy: highest-threat-first.** Prioritize by `v_j / TTI_j` (value density per urgency).
3. **Greedy: most-urgent-killable.** Prioritize targets that can still be killed before TTI, by tightest feasible deadline. Usually the strongest greedy.
4. **Auction / Hungarian assignment.** Solve the turret-to-target assignment as a linear assignment on a value/feasibility matrix; order per-turret by deadline. Strong, fast, classic.
5. **Metaheuristic.** Genetic algorithm OR simulated annealing over (assignment + per-turret permutation). Config selects which. Anytime: returns best-so-far at deadline. This is the "smart, scalable" entrant.
6. **Exact reference: CP-SAT (OR-Tools).** Full battery problem as a constraint/MILP model for small instances. Provides the optimal objective the gap is measured against. Time-boxed; if it cannot prove optimality within budget it reports a bound and the UI labels the gap as bound-based.

### 9.3 Optimality gap

```
gap_policy = (obj_optimal - obj_policy) / max(obj_optimal, epsilon)
```

When the exact solver only has a bound, report `gap_to_bound` and label it as such. Never present an unverified value as a true gap.

### 9.4 Solver-race mode

All enabled solvers run on the **same seeded snapshot** each epoch. Only the active solver's assignment is executed; the others are evaluated for comparison (objective + solve time) and rolled forward on a shadow basis for the gap chart. Switching the active solver mid-run is allowed and logged.

---

## 10. Cost model and breakeven (second headline)

Tracked as a running ledger, updated every epoch:

```
shot_energy_cost   = energy_delivered_kWh · price_per_kWh
maintenance_cost   = maintenance_rate · operating_time
capex_amortized    = system_capex / expected_lifetime_engagements  (per engagement)
cumulative_cost    = Σ (shot_energy_cost + maintenance_cost) + capex_amortized · engagements
value_destroyed    = Σ v_j over killed targets
net_position       = value_destroyed - cumulative_cost
```

**Breakeven curve:** in batch mode, sweep swarm size (and/or weather) and plot `net_position` and the number of intercepts at which `net_position` crosses zero. The narrative truth this surfaces: per-shot the laser wins instantly (cents vs thousands), but the multimillion battery only goes net-positive past a volume of intercepts. That crossover is the chart.

All cost constants are config (section 18) with sourced-but-illustrative defaults.

---

## 11. System architecture

```
┌────────────────────────────────────────────────────────────┐
│  FRONTEND (TypeScript, browser)                              │
│  - Pixi.js battlefield canvas (turrets, beams, drones)       │
│  - Control panel (scenario, weather, swarm, solver, run)     │
│  - Dashboards: gap-vs-compute chart, cost breakeven chart    │
│  - Solver-race split view                                    │
│         ▲  WebSocket (telemetry stream)  │ REST (control)     │
└─────────┼─────────────────────────────────┼──────────────────┘
          │                                 ▼
┌────────────────────────────────────────────────────────────┐
│  BACKEND (Python)                                            │
│  FastAPI (REST) + WebSocket server                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ Sim Engine   │→ │ Solver Suite │→ │ Cost Ledger      │   │
│  │ (physics,    │  │ (greedy,     │  │ (per-epoch)      │   │
│  │  kinematics, │  │  auction,    │  └──────────────────┘   │
│  │  kill res.)  │  │  metaheur.,  │  ┌──────────────────┐   │
│  │              │  │  CP-SAT)     │  │ Telemetry/Replay │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
│  numpy (vectorized physics), OR-Tools (CP-SAT)              │
└────────────────────────────────────────────────────────────┘
```

### 11.1 Why Python backend

The exact reference solver is the spine of the headline metric, and OR-Tools (CP-SAT) plus the metaheuristic ecosystem are Python-native. At balanced scale (tens to low hundreds of drones) Python is fast enough for the decision loop; physics is vectorized with numpy. If thousand-drone scale is ever needed, vectorize harder or move the physics hot loop to a Rust extension (out of scope v1).

### 11.2 Why TS browser frontend

Zero-install demo, runs anywhere a judge has a browser, and Pixi.js gives performant 2D canvas rendering for hundreds of moving entities. The frontend is a thin renderer/controller over the backend telemetry stream; all truth lives server-side.

### 11.3 Headless mode

The sim engine + solver suite + ledger run fully headless for batch sweeps (breakeven and gap-vs-scale curves), producing artifacts without the frontend. Frontend is one consumer of the same engine, never a dependency of it.

---

## 12. Interface contracts

### 12.1 REST endpoints (FastAPI)

```
POST /api/scenario            create/validate a scenario, returns scenario_id
GET  /api/scenario/{id}       fetch a scenario config
POST /api/run                 start a run {scenario_id, solver, seed} -> run_id
POST /api/run/{id}/control    {action: pause|resume|step|stop|set_solver|set_speed}
GET  /api/run/{id}/summary    final metrics + artifact links
POST /api/batch               batch sweep {scenario_id, sweep_spec} -> batch_id
GET  /api/batch/{id}/results  breakeven + gap-vs-scale series
GET  /api/solvers             list available solvers + metadata
GET  /api/weather             list weather profiles
```

### 12.2 WebSocket telemetry (server -> client)

One message per simulation frame (render cadence) and one per decision epoch (solver data). JSON, versioned.

```jsonc
// frame message (render cadence)
{
  "type": "frame",
  "t": 12.34,                      // sim time (s)
  "drones": [
    {"id":"d12","x":120.5,"y":-30.2,"v":[−18,4],"value":2000,
     "hp_frac":0.4,"state":"alive","tti":3.1}
  ],
  "turrets": [
    {"id":"t1","x":0,"y":0,"aim":1.92,"target":"d12",
     "state":"firing","thermal_frac":0.62}
  ],
  "beams": [{"from":"t1","to":"d12","power_frac":0.9}],
  "leaks": 0, "kills": 7
}
```

```jsonc
// epoch message (decision cadence)
{
  "type": "epoch",
  "t": 12.0, "epoch": 24,
  "solvers": [
    {"name":"cp_sat","objective":18400,"solve_ms":210,"is_optimal":true,"bound":18400},
    {"name":"auction","objective":17600,"solve_ms":3,"gap":0.043},
    {"name":"ga","objective":18100,"solve_ms":35,"gap":0.016},
    {"name":"greedy_urgent","objective":16900,"solve_ms":1,"gap":0.081}
  ],
  "active_solver":"auction",
  "ledger": {"cumulative_cost":410000,"value_destroyed":260000,"net":-150000}
}
```

### 12.3 Client -> server control

```jsonc
{"action":"set_solver","solver":"ga"}
{"action":"set_speed","multiplier":2.0}
{"action":"pause"}
{"action":"step","epochs":1}
```

All message schemas are versioned (`schema_version`) and validated (pydantic server-side, zod client-side).

---

## 13. Data models (schemas)

Server-side as pydantic models; mirrored as TS types via generated `types.ts`.

```python
class Drone:
    id: str
    pos: Vec2
    vel: Vec2
    value: float            # cost; also leak penalty
    hardness: float         # energy-to-kill (E_kill)
    class_name: str         # e.g. "quad_small"
    state: Literal["alive","engaged","dead","leaked"]
    energy_absorbed: float

class Turret:
    id: str
    pos: Vec2
    aim: float              # radians
    slew_rate: float        # rad/s
    settle_time: float
    power: float            # emitted
    range_max: float
    thermal: float
    thermal_cfg: ThermalConfig
    state: Literal["idle","slewing","firing","cooldown"]
    current_target: Optional[str]

class WeatherProfile:
    name: str
    alpha: float            # Beer-Lambert extinction coefficient

class Scenario:
    id: str
    asset_pos: Vec2
    battery: list[Turret]
    swarm_spec: SwarmSpec   # count, geometry, behavior, drone class mix
    weather: str
    seed: int
    decision_period: float

class Assignment:
    turret_orders: dict[str, list[str]]   # turret_id -> ordered target ids
    objective_estimate: float

class EpochRecord:
    epoch: int
    t: float
    solver_results: list[SolverResult]
    active_solver: str
    ledger: LedgerSnapshot

class RunSummary:
    run_id: str
    kills: int
    leaks: int
    leaked_value: float
    final_ledger: LedgerSnapshot
    avg_gap_by_solver: dict[str, float]
    avg_solve_ms_by_solver: dict[str, float]
```

---

## 14. Frontend specification (the frontend must carry)

### 14.1 Layout

- **Center: battlefield canvas** (Pixi.js). Asset at center or one edge. Turrets as emplacements that visibly rotate to aim. Active beams drawn turret→target with intensity by delivered power. Drones as markers colored by value class, with a shrinking ring for kill progress and a thin vector for heading. Leaks flash red at the asset.
- **Left: control panel.** Sliders/selects for swarm size, spawn geometry, behavior profile, weather, turret count, decision cadence, sim speed, active solver. Start / pause / step / reset.
- **Right: dashboards.**
  - **Gap-vs-compute chart:** live multi-series line of each solver's objective and a bar/line of solve time per epoch; the gap shaded against the optimal reference.
  - **Cost breakeven chart:** cumulative cost vs value destroyed over time, with the net-position crossover highlighted; in batch view, net-position vs swarm size.
- **Top: scoreboard.** Kills, leaks, protected-value %, active solver, sim clock.

### 14.2 Solver-race view

Split mode: same seeded scenario rendered for two (or more) solvers side by side, scoreboards under each, so a viewer sees one policy leak drones while another holds the line. This is the single most demo-effective screen; prioritize it.

### 14.3 Visual direction

Tactical, dark, high-contrast. Suggested palette to match prior EchoLab work: near-black background, emerald accents for friendly/turret/beam, amber for threat urgency, red for leaks. Subtle grid/scanline texture. Keep typography clean and legible at projector distance. (Design is tertiary priority; do not let it block engine work, but the race view and dashboards must read clearly on a projected screen.)

### 14.4 Performance

Target smooth rendering at hundreds of drones. Use Pixi sprite batching; never re-create display objects per frame, pool them. Interpolate render position between telemetry frames so motion is smooth even if the telemetry cadence is lower than 60fps.

---

## 15. Build phases and acceptance criteria

### Phase 1 — Sim core (headless, backend)

**Deliverables:** entity model; 2D kinematics; the four physics constraints (atmosphere, dwell-to-kill, slew, thermal); kill resolution; one greedy solver; deterministic seeding; JSON telemetry to file.

**Acceptance:**
- A seeded scenario runs headless to completion and emits a telemetry log.
- Same seed → identical log (reproducibility test passes).
- Physics sanity tests pass: kill time increases with range and with worse weather; slew time increases with angular distance; thermal cap forces cooldown.
- A drone that is never engaged leaks; an engaged-in-time drone dies.

### Phase 2 — The brain (solvers + cost, backend)

**Deliverables:** full solver suite incl. CP-SAT exact; optimality-gap computation; cost ledger; FastAPI + WebSocket streaming; headless batch sweep producing breakeven + gap-vs-scale series.

**Acceptance:**
- CP-SAT produces provably optimal assignments for small instances (verified against brute force on tiny cases).
- Heuristic gaps computed correctly against the reference; greedy ≥ gap of auction ≥ gap of GA on average (sanity ordering, not guaranteed but expected).
- Solver-race evaluates all enabled solvers on the identical snapshot each epoch.
- Batch mode outputs a breakeven crossover point and a gap-vs-swarm-size curve.
- Telemetry streams over WebSocket and validates against schema.

### Phase 3 — Frontend (carries)

**Deliverables:** Pixi battlefield, control panel, both dashboards, solver-race split view, scoreboard, dark tactical theme.

**Acceptance:**
- Live run renders turrets slewing/dwelling/killing in sync with telemetry.
- Controls change the running sim (weather, swarm size, solver) and the effect is visible.
- Gap chart and breakeven chart update live and match backend numbers exactly.
- Solver-race split view runs two solvers on one seed and shows divergent outcomes.
- Demo-ready: a preset "wow" scenario loads in one click.

### Phase 4 (optional polish, post-MVP)

3D view, additional drone classes, scenario editor UI, export to video/CSV, shareable run links.

---

## 16. Performance targets

| Metric | Target |
|---|---|
| Balanced scale | 20–150 drones, 2–6 turrets |
| Decision epoch cadence | configurable, default 0.5 s sim-time |
| Heuristic solve time | < 50 ms at balanced scale |
| Auction solve time | < 10 ms at balanced scale |
| CP-SAT solve time budget | time-boxed (default 250 ms), throttled above threshold |
| Telemetry frame rate | 20–30 fps stream; frontend interpolates to 60 fps |
| Reproducibility | bit-identical telemetry for same seed+solver |

Above the CP-SAT threshold (default ~40 targets), the exact solve runs on a throttled cadence and the UI labels gaps as referenced-to-last-exact.

---

## 17. Extensibility and future work

- **Adaptive swarm AI (deferred priority #4):** swarm learns approach geometries/timing that saturate the battery (RL or evolutionary). Engine already exposes the swarm-behavior hook; an adaptive controller plugs in where scripted behavior lives.
- **Multi-objective assignment (v2):** fold cost into the optimization objective (minimize leaked value AND cost), surfacing a Pareto front. v1 keeps cost as analysis-only.
- **3D engagement** with elevation, terrain LOS masking.
- **Rust hot loop** for thousand-drone scale physics.
- **Multi-asset defense** and overlapping batteries.
- **New solvers** (tabu, ACO, PSO, LLM-driven DWTA) drop in via the Solver interface with zero engine changes.

---

## 18. Configuration

All tunables in versioned config (YAML/JSON), never hard-coded. Top-level groups:

```yaml
physics:
  weather_profiles:
    clear: { alpha: 0.002 }
    haze:  { alpha: 0.010 }
    rain:  { alpha: 0.030 }
    fog:   { alpha: 0.080 }
    dust:  { alpha: 0.120 }   # illustrative, not measured
  track_efficiency: { base: 1.0, range_falloff: 0.0015 }
turret_defaults:
  power: 100.0                # illustrative units
  slew_rate: 1.2              # rad/s
  settle_time: 0.1
  range_max: 5000.0
  thermal: { heat_rate: 1.0, cool_rate: 0.4, h_max: 100, h_resume: 30 }
drone_classes:
  quad_small: { hardness: 40,  value: 2000 }
  fixed_wing: { hardness: 120, value: 15000 }
cost:
  price_per_kwh: 0.12
  system_capex: 80000000
  expected_lifetime_engagements: 100000
  maintenance_rate: 50.0      # per operating-hour, illustrative
solver:
  cp_sat_time_budget_ms: 250
  cp_sat_target_threshold: 40
  metaheuristic: "ga"         # or "sa"
sim:
  decision_period: 0.5
  seed: 1337
```

> Every default above is illustrative and exists to make the simulation internally consistent and demonstrable. None is sourced from real weapon engineering data, and the project does not require any.

---

## 19. Repository structure

```
/
├── PRODUCT_DEVELOPMENT_DOCUMENT.md      # this file (ground truth)
├── README.md
├── backend/
│   ├── pyproject.toml
│   ├── beam/
│   │   ├── engine/        # kinematics, physics, kill resolution, loop
│   │   ├── solvers/       # greedy, auction, metaheuristic, cp_sat, base.py
│   │   ├── cost/          # ledger
│   │   ├── api/           # fastapi routes + websocket
│   │   ├── schemas/       # pydantic models
│   │   └── batch/         # headless sweeps
│   └── tests/             # physics, reproducibility, solver correctness
├── frontend/
│   ├── package.json
│   ├── src/
│   │   ├── render/        # pixi battlefield
│   │   ├── panels/        # controls, scoreboard
│   │   ├── charts/        # gap + breakeven
│   │   ├── net/           # websocket + rest client
│   │   └── types.ts       # generated from pydantic
│   └── tests/
├── config/                # scenarios + tunables (yaml)
└── docs/                  # diagrams, model notes
```

---

## 20. Testing strategy

- **Physics unit tests:** monotonicity properties (kill time vs range/weather, slew vs angle), thermal cap behavior, energy bookkeeping conservation.
- **Solver correctness:** brute-force optimum on tiny instances must equal CP-SAT; heuristic objectives must be ≤ optimum and gap must be ≥ 0.
- **Reproducibility:** same seed+solver → identical telemetry hash (CI gate).
- **Schema validation:** every WebSocket/REST payload validated both ends; contract tests on the boundary.
- **Frontend:** chart values reconcile to backend epoch records; render entity counts match telemetry.
- **Regression:** golden telemetry logs for a set of canonical scenarios; diffs flagged in CI.

---

## 21. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| CP-SAT too slow at scale → no live optimal | Gap metric becomes stale | Time-box, throttle cadence above threshold, label gaps honestly as bound/last-exact |
| Frontend can't render large swarms smoothly | Demo stutters | Pixi sprite pooling/batching, interpolation, cap render entity count |
| Non-determinism creeps in (float order, RNG) | Unfair solver comparison, broken repro | Single seeded RNG, fixed iteration order, reproducibility CI gate |
| Scope creep into adaptive swarm / 3D | MVP slips | Phases gated by acceptance criteria; adaptive + 3D are explicitly Phase 4+ |
| Physics seen as "made up" | Credibility hit with reviewers | Document every constant in config with rationale; lead with the WTA research framing, present physics as deliberately illustrative |
| Misread as a weapon-building project | Reputational | Frame consistently as a simulation/OR analysis tool; no hardware, no real weapon parameters (stated in §1, §3, §18) |

---

## 22. References (research grounding)

- Lloyd, S. P., & Witsenhausen, H. S. (1986). Weapons allocation is NP-complete. *Proc. IEEE Summer Computer Simulation Conference.* — the NP-completeness result.
- Ahuja, R. K., et al. — exact and heuristic algorithms for WTA (network/linear approaches).
- Kline, A., Ahner, D., Hill, R. — survey of the Weapon-Target Assignment problem (static and dynamic formulations, exact vs heuristic), *Computers & Operations Research.*
- Leboucher, C., et al. (2013) — real-time WTA solution under the "before targets reach goal" deadline constraint.
- Hu et al. (2020) — Dynamic WTA via cross-entropy, *Mathematical Problems in Engineering.*
- Recent (2025) work applying large language models to dynamic WTA (arXiv) — evidence the field is active.
- Google OR-Tools (CP-SAT) — exact reference solver.
- Reynolds, C. (1987) — Boids/flocking model for swarm behavior.
- Beer-Lambert law — atmospheric attenuation model.
- Publicly reported fielded laser counter-drone economics (cents-per-shot vs missile cost), used only as motivation for the cost model, not as engineering input.

---

*End of document. This PDD is authoritative. Propose changes via PR against this file.*
