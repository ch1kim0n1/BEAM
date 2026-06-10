# BEAM - Architecture & Flow Specification

| Field | Value |
|---|---|
| Document type | Architecture breakdown + end-to-end user/information flow |
| Status | v1.0 - authoritative for system structure & data flow |
| Companion docs | `PRODUCT_DEVELOPMENT_DOCUMENT.md` (model/contract), `demo.md` (run-of-show), `design.md` (UI/UX) |
| Audience | All engineers (backend, frontend, integration) |

> Purpose: the other docs say *what the model is* (PDD), *what it looks like* (design), and *what the viewer experiences* (demo). This doc says **how the system is wired and how information moves through it**, so no engineer is ever unsure what a user action triggers or where a value comes from. Read §6 (user flow) and §7 (sequences) together: they are the same story told from the user's side and the system's side.

---

## 1. The mental model in one paragraph

BEAM has two planes. The **control plane** is request/response and changes *what the simulation is doing* (load a preset, run, pause, swap solver, change weather). The **data plane** is a continuous stream that reports *what the simulation is doing* (entity positions every frame, solver scores and the cost ledger every epoch). The backend owns all truth: it runs a deterministic simulation loop, solves the targeting problem each decision epoch, and broadcasts telemetry. The frontend owns nothing but presentation: it sends control commands and renders the stream. If you remember one thing: **control flows in over REST + a command channel; truth flows out over the telemetry stream; the frontend never computes simulation state itself.**

```
        CONTROL PLANE (change it)                 DATA PLANE (observe it)
  user ──▶ UI ──▶ REST / cmd channel ──▶ backend ──▶ telemetry stream ──▶ UI ──▶ user
                                          (owns all truth, runs the loop)
```

---

## 2. System overview

```
┌──────────────────────────────── FRONTEND (browser, TypeScript) ───────────────────────────────┐
│                                                                                                 │
│  Panels/Controls ──cmd──▶ Net Layer ──REST──▶                Net Layer ◀──WS telemetry──        │
│        │                      │                                   │                             │
│        ▼                      ▼                                   ▼                             │
│  Mode Controller ───────▶ Client State Store ◀──────────── Telemetry Ingest                     │
│  (single/split/analysis)      │                                                                 │
│        │                      ├────────▶ Render Layer (Pixi battlefield, interpolated)          │
│        │                      ├────────▶ Charts (gap/compute, breakeven)                        │
│        │                      └────────▶ Scoreboard / Transport                                 │
└───────────────────────────────────────────┬────────────────────────────┬──────────────────────┘
                                  REST (control plane)        WebSocket (data plane, bidirectional)
                                             │                            │
┌────────────────────────────────────────────▼────────────────────────────▼──────────────────────┐
│                                   BACKEND (Python)                                               │
│  ┌──────────────┐                                                                                │
│  │  API Layer   │  FastAPI REST routes  +  WebSocket endpoint (telemetry out / commands in)      │
│  └──────┬───────┘                                                                                │
│         ▼                                                                                        │
│  ┌──────────────┐   owns lifecycle of a run; holds RunContext; applies queued commands           │
│  │ Run Manager  │   at tick/epoch boundaries (preserves determinism)                             │
│  └──────┬───────┘                                                                                │
│         ▼                                                                                        │
│  ┌──────────────┐   tick loop: kinematics · atmosphere · dwell · slew · thermal · kill res.      │
│  │  Sim Engine  │───────────────┐                                                                │
│  └──────┬───────┘               │ each EPOCH:                                                    │
│         │ each FRAME            ▼                                                                 │
│         │             ┌──────────────────┐   ┌──────────────┐   ┌──────────────┐                 │
│         │             │  Solver Suite    │──▶│  Gap Calc    │   │ Cost Ledger  │                 │
│         │             │ greedy/auction/  │   │ vs cp_sat ref│   │ per-epoch    │                 │
│         │             │ ga/sa/cp_sat     │   └──────────────┘   └──────────────┘                 │
│         │             └──────────────────┘                                                       │
│         ▼                                                                                        │
│  ┌──────────────┐   builds frame & epoch messages, broadcasts to subscribers, records to replay  │
│  │  Telemetry   │                                                                                │
│  └──────────────┘                                                                                │
│                                                                                                  │
│  Cross-cutting: Config Loader (yaml) · Schemas (pydantic) · Batch Runner (headless sweeps)        │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Backend component breakdown

### 3.1 API Layer (`beam/api/`)
- **REST routes** (control-plane lifecycle): create/validate scenario, start run, stop, summary, list solvers/weather, start batch, fetch batch results. (Full list: PDD §12.1.)
- **WebSocket endpoint** (data plane, bidirectional): on connect, subscribes the client to a run's telemetry; **outbound** = `frame` and `epoch` messages; **inbound** = in-run control commands (`pause`, `resume`, `step`, `set_speed`, `set_solver`, `set_weather`).
- Validates every payload (pydantic) against the versioned schema. Rejects malformed commands with a non-fatal error message; never drops the socket on a bad command.

> **Plane reconciliation (resolves PDD §12.1 vs §12.3):** Run *lifecycle* (create/start/stop/batch) is REST. In-run *controls* travel over the WebSocket as client→server messages, for low latency and strict ordering relative to the stream. A REST `/run/{id}/control` mirror exists for scripting/headless but the live UI uses the socket.

### 3.2 Run Manager (`beam/api/run_manager.py`)
- Owns the lifecycle and identity of each run. Holds a **RunContext**: the `WorldState`, the active solver, the enabled solver set (for races), the ledger, the seed, the speed multiplier, and a **command queue**.
- Runs the sim loop as its own async task. **Commands are not applied the instant they arrive**; they are enqueued and drained at the next safe boundary (tick for transport, epoch for solver/scenario changes). This is what keeps runs deterministic and comparisons fair.
- Implements the run **state machine** (§5).

### 3.3 Sim Engine (`beam/engine/`)
The deterministic core. One **tick** advances physics by `dt`; one **epoch** (every `decision_period`) triggers a re-solve. Subsystems, run in order each tick:
1. **Kinematics** - advance drones per behavior (direct/flocking/staggered); update turret aim toward current target by slew rate.
2. **Atmosphere** - compute delivered power per active beam via Beer-Lambert (`exp(-alpha·range)`).
3. **Dwell/energy** - deposit energy onto engaged targets; `dwell-to-kill = E_kill / deposition_rate`.
4. **Slew** - accumulate setup time for retargeting turrets.
5. **Thermal** - heat firing turrets, cool idle ones, force cooldown at cap.
6. **Kill resolution** - mark drones dead when cumulative energy ≥ `E_kill`; mark drones leaked when they reach the asset.
- Owns `WorldState`; emits nothing itself (Telemetry reads from it).
- **Single seeded RNG**, fixed iteration order. Reproducibility is a hard invariant (CI-gated).

### 3.4 Solver Suite (`beam/solvers/`)
- A **registry** maps solver name → implementation; all implement `solve(state, deadline_ms) -> Assignment` (PDD §9.1).
- Each epoch the Run Manager calls the **active** solver to get the assignment that is actually applied to the turrets. In race/eval mode it also calls every **enabled** solver on the *same snapshot* for scoring (shadow evaluation; their assignments are not applied unless that pane/solver is active).
- **Gap Calc** computes each solver's objective vs the `cp_sat` reference. Above the target threshold, `cp_sat` is time-boxed/throttled and the result is tagged `bound`, which propagates to the UI as `~GAP (bound)`.

### 3.5 Cost Ledger (`beam/cost/`)
- Updated each epoch: per-shot energy cost, amortized capex per engagement, maintenance, cumulative cost, value destroyed, net position. Pure function of the run so far + config; feeds the `epoch` message and the batch breakeven series.

### 3.6 Telemetry (`beam/api/telemetry.py`)
- Builds **`frame`** messages at render cadence (entity positions/states) and **`epoch`** messages at decision cadence (solver scores, gaps, ledger). Broadcasts to all subscribers of the run and appends to a replay log. Message shapes are PDD §12.2.

### 3.7 Batch Runner (`beam/batch/`)
- Runs the engine+solvers+ledger fully headless over a sweep spec (e.g. swarm size 10→120), producing the breakeven and gap-vs-scale series consumed by analysis mode (P4). No sockets, no rendering. Demo ships pre-computed P4 output.

### 3.8 Cross-cutting
- **Config Loader** (`beam/` + `config/`): all tunables from yaml; nothing hard-coded.
- **Schemas** (`beam/schemas/`): pydantic models, the single source for the wire contract; TS types are generated from these.

---

## 4. Frontend component breakdown

### 4.1 Net Layer (`src/net/`)
- **REST client** for lifecycle calls.
- **WebSocket client** for the telemetry stream and outbound commands. Handles reconnect with backoff; on disconnect, freezes entities and shows the "RECONNECTING" chip (design §10); on reconnect, resubscribes and resumes from the live stream.

### 4.2 Client State Store (`src/store/`)
- The single client-side model: current entities (drones/turrets/beams), scoreboard metrics, rolling buffers for chart series, run state, mode. **Derived from telemetry only.** Components subscribe; they never fetch or compute simulation truth.

### 4.3 Render Layer (`src/render/`)
- Pixi battlefield. Consumes `frame` messages, **interpolates** entity positions between frames for 60fps smoothness, applies state visuals and effects (design §4, §7). Sprite-pooled and batched.

### 4.4 Charts (`src/charts/`)
- Gap/compute and breakeven, fed from `epoch` messages and batch results. Tween new points; render the optimal-reference line and gap shading; honor the bound-labeling honesty rule.

### 4.5 Panels (`src/panels/`)
- Control panel (emits commands), scoreboard and transport (subscribe to store). Controls tagged `[live]` send a command immediately; `[restart]` controls stage changes applied on the next run.

### 4.6 Mode Controller (`src/mode/`)
- Switches single / split / analysis layouts and tells the Net Layer what to subscribe to (e.g. split mode subscribes to two solver-shadow streams of one run). Cross-fades per design §7.5.

### 4.7 Types (`src/types.ts`)
- Generated from backend pydantic schemas. The contract is enforced at the type level on both ends.

---

## 5. Run lifecycle state machine

```
        ┌─────────┐  boot
        │  BOOT   │──────▶ preload presets, warm solvers
        └────┬────┘
             ▼
        ┌─────────┐  load preset / POST scenario+run (paused)
   ┌───▶│  IDLE   │  battlefield at t=0, RUN pulsing
   │    └────┬────┘
   │         │ RUN (cmd: resume)
   │         ▼
   │    ┌─────────┐  tick loop active, telemetry streaming
   │    │ RUNNING │◀──────────┐
   │    └────┬────┘           │ resume
   │   pause │   │ engagement │
   │         ▼   │ complete   │
   │    ┌─────────┐           │
   │    │ PAUSED  │───────────┘
   │    └─────────┘
   │         │ (RUNNING) all drones dead or leaked
   │         ▼
   │    ┌─────────┐  final scoreboard + summary
   │    │FINISHED │
   │    └────┬────┘
   │  RESET  │
   └─────────┘

   ANALYSIS (P4): a parallel mode entered from any state via the P4 preset;
   replaces battlefield with the breakeven plot; no tick loop, drives a (pre-computed) sweep.
   SPLIT: a flavor of RUNNING with two enabled solvers rendered in two panes on one clock.
```

Transitions are driven by control-plane commands; the resulting state and all numbers come back over the data plane.

---

## 6. User flow (what the user does, and what it triggers)

This is the demo (`demo.md`) re-told as cause→effect across the stack. Read each row left to right: the user's action, the UI control, the command sent, the backend effect, and what the user then sees stream back.

| # | User intent | UI control | Plane / message | Backend effect | What streams back (sees) |
|---|---|---|---|---|---|
| 1 | Start the demo | (auto on boot) | REST `POST /scenario`+`/run` (P1, paused) | RunContext built; state = IDLE | battlefield at t=0, paused, RUN pulsing |
| 2 | "Attack." | press **RUN** | WS cmd `resume` | state → RUNNING; tick loop starts | frames stream: swarm advances, turrets slew/dwell/kill; scoreboard ticks |
| 3 | Compare policies | select **P2** (race) | REST new run + `set solver-race` | new RunContext, two enabled solvers, SPLIT | two paused panes, one seed |
| 4 | "Watch them diverge." | press **RUN** | WS cmd `resume` | both panes driven on one clock; each pane's solver applied within its pane | left leaks, right holds; per-pane scoreboards diverge |
| 5 | Overwhelm it | select **P3** | REST new run | RunContext: 60 drones, fog, ga + cp_sat ref | paused single view, multi-series gap chart armed |
| 6 | "Saturate." | press **RUN** | WS cmd `resume` | waves spawn; cp_sat throttles past threshold | leaks climb; gap shading widens; cp_sat solve-time bar spikes (labeled) |
| 7 | (optional) prove physics | **Weather** select → fog/clear | WS cmd `set_weather` (live) | atmosphere `alpha` changes at next tick | dwell-to-kill visibly stretches/shrinks; beams thin/thicken |
| 8 | (optional) prove decision | **Active solver** swap | WS cmd `set_solver` (live, at epoch boundary) | active solver changes; marker logged | gap line and leak rate shift; vertical marker on chart |
| 9 | Show the economics | select **P4** | REST `GET /batch/{id}/results` | analysis mode; load pre-computed sweep | breakeven plot animates; crossover marker + callout |
| 10 | Explore assumptions | **cost sliders** | WS/REST cost params (live) | ledger recomputed over the sweep | crossover marker slides live |
| 11 | Next viewer | press **RESET** | WS cmd `reset` / REST new P1 run | back to IDLE on P1 | clean landing state |

**The one sentence for a lost engineer:** every button the user presses is a control-plane message that changes the RunContext at a safe boundary; everything the user *sees* arrives only as `frame`/`epoch` telemetry the backend computed. The UI is a window, not a brain.

---

## 7. Information-flow sequences

### 7.1 Load a preset (control plane)

```
User → UI: pick preset / boot
UI  → REST: POST /scenario {preset}          ──▶ validate, store, return scenario_id
UI  → REST: POST /run {scenario_id, solver(s), seed} ──▶ RunManager builds RunContext (PAUSED, t=0)
UI  → WS:   connect & subscribe(run_id)      ──▶ backend registers subscriber
backend → WS: frame@t=0 (initial layout)     ──▶ UI renders idle battlefield
state: IDLE
```

### 7.2 Run an engagement (the core loop)

```
User → UI: press RUN
UI  → WS: {action: resume}                   ──▶ command queued
RunManager: drain cmd at boundary → state RUNNING; start tick loop

loop (every dt):
  SimEngine.tick(): kinematics → atmosphere → dwell → slew → thermal → kill-res
  Telemetry.frame() ─WS▶ UI.Render (interpolated)            [render cadence]

  on epoch boundary (every decision_period):
    drain any queued epoch-level commands (solver/scenario)
    Solvers.solve(active) → apply assignment to turrets
    Solvers.solve(enabled…) on same snapshot → scores      [shadow eval]
    GapCalc vs cp_sat ; CostLedger.update()
    Telemetry.epoch() ─WS▶ UI.Charts + Scoreboard
until all drones dead/leaked → state FINISHED → Telemetry.summary
```

### 7.3 Live control mid-run (e.g. change weather / swap solver)

```
User → UI: change Weather to fog  (a [live] control)
UI  → WS: {action: set_weather, weather: fog}
RunManager: enqueue; apply at NEXT TICK boundary (weather) / NEXT EPOCH (solver)
SimEngine: subsequent ticks use new alpha  → longer dwell-to-kill
Telemetry: frames/epochs reflect the change ─WS▶ UI updates
            (no run restart, no reload - continuity preserved)
```
> Why boundary-applied: applying mid-tick would break determinism and make the live gap comparison unfair. The queue + boundary rule is the contract.

### 7.4 Solver-race split

```
UI (split mode) subscribes to one run with two enabled solvers.
Each epoch: RunManager solves BOTH on the identical seeded snapshot.
Pane A renders solver_A's applied assignment; Pane B renders solver_B's.
Both panes advance on ONE clock / ONE swarm RNG (never two streams).
epoch message carries per-solver objective+gap → gap chart overlays both;
per-pane scoreboards come from each solver's applied outcome.
```

### 7.5 Analysis / batch (P4)

```
(at build time) BatchRunner sweeps swarm size headless → series on disk
User → UI: select P4
UI  → REST: GET /batch/{id}/results          ──▶ returns cost/value/net series
UI: animate breakeven plot; compute crossover; show callout
User drags cost slider → UI recomputes ledger over series locally OR
   re-requests with new cost params → crossover marker moves live
(no tick loop, no battlefield, no streaming)
```

### 7.6 Disconnect / recovery (data plane resilience)

```
WS drops mid-run:
  UI.Net: detect close → show "RECONNECTING" chip; freeze entities (last frame), pulse
  UI.Net: backoff-reconnect → resubscribe(run_id)
backend: run keeps ticking (truth never paused by a viewer's socket)
on reconnect: backend sends current frame → UI snaps/interp to live → chip clears
(never a full-screen error during a demo)
```

---

## 8. Concurrency & ordering model (backend)

- **One sim loop task per active run**, advancing deterministically. A split race is still one run/one loop solving multiple solvers per epoch (not two loops).
- **Command queue per run.** Inbound commands are timestamped and drained at boundaries: transport (`pause/resume/step/speed`) and weather at tick boundary; `set_solver`/scenario-level at epoch boundary. This serializes all mutations relative to the deterministic loop.
- **Broadcast fan-out:** Telemetry pushes to all subscribers of a run; a slow/absent subscriber never stalls the loop (drop-newest per-subscriber buffer; truth keeps advancing).
- **Speed multiplier** scales sim-time per wall-second, not the determinism: the same seed at `1×` and `4×` produces the same engagement, just delivered faster.

---

## 9. Determinism guarantees (why comparisons are fair)

1. One seeded RNG per run; fixed subsystem and entity iteration order.
2. All solvers each epoch see the **same immutable snapshot**.
3. Commands applied only at boundaries, never mid-tick.
4. Same `{seed, scenario, solver}` → byte-identical telemetry hash (CI gate, PDD §20).
   These four together are what make "left pane leaked, right pane held" a real result rather than an artifact.

---

## 10. Where each value on screen comes from (traceability)

| On-screen element | Source message | Produced by |
|---|---|---|
| Drone/turret/beam positions & states | `frame` | SimEngine → Telemetry |
| KILLS / LEAKS / PROTECTED % / CLOCK | `frame` | SimEngine kill-resolution → Telemetry |
| Per-solver objective & GAP % | `epoch` | Solver Suite + GapCalc |
| Solve-time bars (+ throttled/bound tag) | `epoch` | Solver Suite timing |
| COST / VALUE / NET (live) | `epoch` | Cost Ledger |
| Breakeven curve & crossover (P4) | REST batch results | Batch Runner |
| Active-solver swap markers | `epoch` event field | Run Manager |

If a number is on screen and not in this table, it is being computed client-side and that is a bug.

---

## 11. Definition of done (integration)

- Every control in `design.md` §6 maps to exactly one control-plane message in §6 here, and the effect appears only via telemetry.
- The full P1→P2→P3→P4→reset user flow (§6) runs end to end with the sequences in §7.
- Determinism gate (§9) green in CI.
- Disconnect/recovery (§7.6) verified without a visible full-screen error.
- The traceability table (§10) holds: no on-screen value is computed in the client.

> If `demo.md` is the script and `design.md` is the set, this document is the wiring diagram. Follow §6 and §7 and the user is never lost, because the system never is.
