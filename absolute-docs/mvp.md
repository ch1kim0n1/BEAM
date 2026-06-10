# BEAM - MVP Definition

> **Derived view, not ground truth.** This file consolidates the MVP scope from
> [`pdd.md`](./pdd.md) §3, §4, §15, §16 into one focused document. If this file and
> `pdd.md` disagree, **`pdd.md` is authoritative** - reconcile against it.

| Field | Value |
|---|---|
| Document type | MVP Scope & Acceptance |
| Source | `pdd.md` Phases 1–3 |
| Status | Scope frozen for MVP build |
| Last updated | 2026-06-03 |

---

## 1. What the MVP is

BEAM's MVP is a **deterministic, real-time simulation of a multi-turret laser
battery defending a fixed asset against a drone swarm**, that solves the targeting
decision live as a **Dynamic Weapon-Target Assignment (DWTA)** problem and surfaces
two headline outputs from one engine:

1. **Solver race with optimality gap** - multiple assignment policies (greedy
   baselines, auction, a metaheuristic, and an exact CP-SAT reference) run on the
   identical seeded scenario each decision epoch; the system streams each policy's
   objective, its gap from the proven optimum, and its solve time, live.
2. **Cost-exchange breakeven curve** - a ledger tracks per-shot energy cost,
   amortized capex, and maintenance against cumulative value of drones destroyed,
   and surfaces the net-position crossover point.

It is a **software simulation and operations-research analysis tool**. No hardware,
no real weapon parameters; all physics constants are illustrative and live in config.

The MVP is complete when a reviewer can, in **under 60 seconds and with no
explanation**, watch a swarm attack, see turrets slew/dwell/kill, see one solver beat
another on the same seed, and read the breakeven crossover.

---

## 2. In scope (MVP = PDD Phases 1–3)

### Engine (Phase 1)
- Entity model: drones, turrets, battery, asset, scenario, weather.
- 2D kinematics (top-down; z carried, unused). Drone behaviors: `direct`,
  `flocking` (boids), `staggered` waves.
- The four physics constraints that make targeting hard:
  - **Atmospheric attenuation** - Beer-Lambert `P·exp(-alpha·range)` per weather profile.
  - **Dwell-to-kill** - `E_kill / deposition_rate`, deposition degrades with range/track.
  - **Slew** - sequence-dependent setup: `angular_distance / slew_rate + settle_time`.
  - **Thermal** - heat while firing, cool while idle, forced cooldown at cap.
- Kill resolution (continuous-energy; break-beam loses progress by default).
- Deterministic seeding; JSON telemetry to file.

### Brain - solvers + cost (Phase 2)
- Pluggable solver suite behind one `Solver` interface, in build order:
  1. `greedy_nearest` - baseline floor
  2. `greedy_threat` - value-per-urgency (`v_j / TTI_j`)
  3. `greedy_urgent` - tightest still-killable deadline first
  4. `auction` - linear/Hungarian assignment, order per-turret by deadline
  5. `ga` / `sa` - metaheuristic, anytime, best-so-far at deadline (config selects)
  6. `cp_sat` - exact OR-Tools reference; provides the gap baseline; time-boxed
- Optimality-gap computation per epoch, against the exact reference; honest
  bound/last-exact labeling above the CP-SAT target threshold.
- Cost ledger: shot energy, maintenance, amortized capex, net position.
- FastAPI REST + WebSocket telemetry streaming (versioned, validated both ends).
- Headless batch sweeps producing breakeven + gap-vs-scale series.

### Frontend (Phase 3 - carries the demo)
- Pixi.js battlefield canvas: turrets slewing, beams by delivered power, drones
  with kill-progress rings + heading vectors, leaks flashing at the asset.
- Control panel: swarm size, spawn geometry, behavior, weather, turret count,
  decision cadence, sim speed, active solver; start/pause/step/reset.
- Dashboards: live gap-vs-compute chart, cost breakeven chart.
- **Solver-race split view** - same seed, two+ solvers side by side (highest-value
  demo screen; prioritize).
- Scoreboard: kills, leaks, protected-value %, active solver, sim clock.
- Dark tactical theme; smooth rendering at hundreds of drones (sprite pooling +
  interpolation).

---

## 3. Out of scope (post-MVP - PDD Phase 4+)

- 3D view, elevation, terrain LOS masking.
- Scenario editor UI; export to video; shareable run links.
- Adaptive / learning swarm AI (deferred priority #4; scripted/flocking only in MVP).
- Multi-objective assignment (fold cost into objective → Pareto front).
- Rust hot loop for thousand-drone scale.
- Multi-asset defense, overlapping batteries.
- Multiplayer, accounts, persistence beyond run artifacts.

---

## 4. Deliverables & acceptance by phase

### Phase 1 - Sim core (headless backend)
**Deliverables:** entity model; 2D kinematics; four physics constraints; kill
resolution; one greedy solver; deterministic seeding; JSON telemetry to file.

**Acceptance:**
- Seeded scenario runs headless to completion and emits a telemetry log.
- Same seed → byte-identical log (reproducibility test passes).
- Physics sanity: kill time rises with range and worse weather; slew rises with
  angular distance; thermal cap forces cooldown.
- An un-engaged drone leaks; an engaged-in-time drone dies.

### Phase 2 - Brain (solvers + cost backend)
**Deliverables:** full solver suite incl. CP-SAT; optimality-gap computation; cost
ledger; FastAPI + WebSocket streaming; headless batch sweep (breakeven + gap-vs-scale).

**Acceptance:**
- CP-SAT provably optimal on small instances (verified vs brute force on tiny cases).
- Heuristic gaps correct vs reference; gaps non-negative; expected ordering
  greedy ≥ auction ≥ GA on average.
- Solver-race evaluates all enabled solvers on the identical snapshot each epoch.
- Batch mode outputs a breakeven crossover and a gap-vs-swarm-size curve.
- Telemetry streams over WebSocket and validates against schema.

### Phase 3 - Frontend (carries)
**Deliverables:** Pixi battlefield, control panel, both dashboards, solver-race
split view, scoreboard, dark tactical theme.

**Acceptance:**
- Live run renders turrets slewing/dwelling/killing in sync with telemetry.
- Controls change the running sim (weather, swarm, solver); effect is visible.
- Gap and breakeven charts update live and match backend numbers exactly.
- Solver-race split runs two solvers on one seed showing divergent outcomes.
- A preset "wow" scenario loads in one click.

---

## 5. Performance targets

| Metric | Target |
|---|---|
| Balanced scale | 20–150 drones, 2–6 turrets |
| Decision epoch cadence | configurable, default 0.5 s sim-time |
| Heuristic solve time | < 50 ms at balanced scale |
| Auction solve time | < 10 ms at balanced scale |
| CP-SAT solve budget | time-boxed (default 250 ms), throttled above ~40 targets |
| Telemetry frame rate | 20–30 fps stream; frontend interpolates to 60 fps |
| Reproducibility | bit-identical telemetry for same seed + solver |

---

## 6. Definition of done

The MVP ships when **all three phases' acceptance criteria pass** and:

- **Demo bar:** the <60-second no-explanation reviewer test (§1) is met.
- **Depth:** CP-SAT provably optimal on small instances; every heuristic's gap is
  mathematically correct against it.
- **Rigor:** every number on screen traces to a documented model in `pdd.md`; no
  magic constants without a config entry and rationale.
- **Reproducibility:** same seed + solver → byte-identical telemetry (CI gate).
- **Performance:** decision loop sustains the target epoch rate at balanced scale
  without dropping frames.

---

## 7. Build order for the swarm

1. **Scaffold** - repo structure, `config/defaults.yaml`, `pyproject.toml`,
   `package.json`, pydantic schemas as the shared contract.
2. **Phase 1** - engine modules (kinematics, physics, kill resolution, loop) + first
   greedy solver + telemetry, gated by Phase 1 acceptance.
3. **Phase 2** - remaining solvers (parallel, one interface), cost ledger, gap,
   FastAPI/WebSocket, batch sweeps, gated by Phase 2 acceptance.
4. **Phase 3** - frontend renderer/panels/charts/race-split over the telemetry stream.
5. **Integration + verify** - reproducibility, schema contract tests, demo preset.
