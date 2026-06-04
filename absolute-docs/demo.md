# BEAM — Demo Script & Simulation Storyboard

| Field | Value |
|---|---|
| Document type | Demo specification (storyboard + run-of-show) |
| Status | v1.0 — authoritative for the demo experience |
| Companion docs | `PRODUCT_DEVELOPMENT_DOCUMENT.md` (model/contract), `design.md` (visual/UX) |
| Audience | Engineers building the demo flow + anyone presenting it |
| Target runtime | 90 seconds core, expandable to ~3 min with narration |

> This document is as binding as the backend spec. The demo is the product for 90% of the people who will ever see BEAM. Build the flow described here deliberately, beat by beat. If a moment in this storyboard is hard to render, that is a bug to fix, not a beat to drop.

---

## 0. What the demo must prove

In 90 seconds, with zero explanation required, a viewer must walk away having understood four things in order:

1. **It's real.** A laser battery actually tracks and kills a swarm. The physics looks believable.
2. **The decision is hard.** Different targeting policies produce visibly different outcomes on the *same* swarm. A dumb policy leaks drones a smart one stops.
3. **There's a measurable best.** An exact optimum exists, every policy is scored against it live, and you can watch the gap.
4. **It pays off.** The expensive laser becomes economically net-positive past a crossover, and we show exactly where.

Each beat below is annotated with which of these it proves and which audience it lands for (J = judge, R = recruiter, S = technical reviewer).

---

## 1. Demo presets (canonical scenarios)

These four presets are shipped, seeded, and pinned. The demo only ever uses these. They live in `config/scenarios/demo/`.

| Preset | File | Swarm | Behavior | Weather | Turrets | Solver(s) | Seed | Purpose |
|---|---|---|---|---|---|---|---|---|
| **P1 First Contact** | `p1_first_contact.yaml` | 12 quad_small | direct | clear | 3 | `greedy_urgent` | 1337 | establish believability, laser dominant |
| **P2 The Race** | `p2_the_race.yaml` | 24 mixed | flocking | haze | 3 | split: `greedy_nearest` vs `auction` | 4242 | show the decision matters |
| **P3 Saturation** | `p3_saturation.yaml` | 60 mixed, 3 waves | staggered | fog | 4 | `ga` (with `cp_sat` reference) | 8675 | show the laser can lose; gap widens |
| **P4 Breakeven** | `p4_breakeven.yaml` | sweep 10→120 | direct | clear | 4 | `auction` | 1337 | the economic crossover chart |

"mixed" = 80% quad_small (value 2000), 20% fixed_wing (value 15000).

Every preset must be reproducible: same seed → identical run. The demo machine runs these from disk, never randomly generated live.

---

## 2. Run-of-show (beat by beat)

Timecodes are cumulative from demo start. Each beat lists: on-screen action, what the operator does, the talk track, and what it proves.

### Cold open — `0:00–0:06` (landing state)

- **Screen:** App loads on the **P1 First Contact** preset, *paused at t=0*. Battlefield shows the asset glowing at center, 3 turret emplacements idle, 12 drone markers massed at the right edge holding position. Scoreboard reads `KILLS 0 · LEAKS 0 · PROTECTED 100%`. A single pulsing **RUN** button.
- **Operator:** nothing yet. Let it sit for 2 seconds so the eye finds the asset, the turrets, the swarm.
- **Talk track:** "This is a laser battery defending one target. Twelve drones are about to attack it."
- **Proves:** orientation. (J, R, S)

### Act 1 — `0:06–0:24` — First Contact (it's real)

- **Operator:** press **RUN**.
- **Screen:**
  - Swarm advances left toward the asset at constant speed, heading vectors visible.
  - Turrets **slew** to their first assigned targets (visible rotation, eased, ~200ms settle), then **fire**: a beam connects turret→drone, the target's **kill-progress ring** shrinks over its dwell time, then the drone **pops** (kill animation: flash + fade + small debris scatter) and the turret immediately slews to the next.
  - Scoreboard `KILLS` ticks up. `PROTECTED` holds at 100%.
  - Bottom-right **gap chart** begins drawing: a single solver line tracking objective; solve-time bars stay tiny.
- **Talk track:** "Each turret is a single beam. It has to hold on a target long enough to burn it down, then swing to the next. One beam, one target at a time. Watch it clear the wave."
- **End state:** all 12 down, 0 leaks, clean. Brief 1s hold on `KILLS 12 · LEAKS 0`.
- **Proves:** #1 (believable physics: slew, dwell, sequential kills). (J, R, S)

### Transition — `0:24–0:28`

- **Operator:** click **P2 The Race** preset; the view swaps to **solver-race split**, paused at t=0.
- **Talk track:** "Same idea, but now the swarm is bigger and moving as a flock. And here's the real question: how should the battery decide who to shoot first?"

### Act 2 — `0:28–0:50` — The Race (the decision matters)

- **Screen:** Split battlefield. **Left pane: `greedy_nearest`** (shoot whatever's closest). **Right pane: `auction`** (optimal-ish assignment). Identical swarm, identical seed, side by side. Each pane has its own mini-scoreboard.
- **Operator:** press **RUN** (drives both panes in lockstep on the same clock).
- **Screen, the payoff:**
  - Both start similar. Then the left pane (nearest-first) wastes time on low-value drones near the turrets while a **fixed_wing** (high value, amber-tinted, larger) slips through. `LEAKS` ticks up on the left, `PROTECTED` drops to ~78%.
  - The right pane (auction) prioritizes the urgent high-value threats; it holds `PROTECTED` near 96%.
  - The contrast is the moment. Left pane flashes **red leak** at its asset; right pane does not.
- **Talk track:** "Left shoots the nearest drone. Right solves an assignment that weighs value and time-to-impact. Same swarm. Left leaks the expensive one. Right holds the line. The policy is the product."
- **End state:** hold 2s on the divergent scoreboards (e.g. left `PROTECTED 78%`, right `PROTECTED 96%`).
- **Proves:** #2 (the decision is hard and consequential). (J, R, S)

### Transition — `0:50–0:54`

- **Operator:** click **P3 Saturation**. Single battlefield returns, paused. Gap chart now shows multiple solver series plus a distinct **optimal reference** line.
- **Talk track:** "Now let's overwhelm it. Sixty drones, three waves, in fog."

### Act 3 — `0:54–1:14` — Saturation (it can lose; the gap is real)

- **Screen:**
  - Fog visibly degrades the scene (desaturated overlay, longer beams, slower kills, because attenuation raises dwell-to-kill).
  - The active solver is `ga`; the **optimal `cp_sat` reference** is plotted as the ceiling on the gap chart.
  - Waves arrive staggered. The single battery cannot service everything: `LEAKS` climbs, `PROTECTED` falls to maybe ~70%.
  - On the **gap chart**, as target count spikes during each wave, the heuristic line dips below the optimal reference and the **shaded gap widens**; simultaneously the **solve-time bar for `cp_sat` spikes** (and gets labeled "throttled / bound" once it crosses the target threshold), while `ga` stays cheap.
- **Talk track:** "Fog makes every kill slower. The swarm saturates one battery. Notice two things: drones now leak, and the gap between our fast solver and the proven optimum opens up, while the exact solver gets too slow to keep up. That tradeoff is the whole field."
- **Proves:** #3 (a measurable optimum, an honest gap, the speed/quality tension) and reinforces #1 (weather physics). (S strongly, J, R)

### Transition — `1:14–1:18`

- **Operator:** click **P4 Breakeven**. View switches to **Analysis / batch** mode.
- **Talk track:** "Last thing. These systems cost millions. Do they actually pay off?"

### Act 4 — `1:18–1:32` — Breakeven (it pays off)

- **Screen:** The breakeven dashboard animates a pre-computed sweep: x-axis = swarm size (10→120), two lines, **cumulative cost** (nearly flat, pennies per shot) and **value destroyed** (rising steeply). The **crossover point** is drawn with a vertical marker and a callout: e.g. `Net-positive past ~38 intercepts`. Net-position fills green to the right of the crossover.
- **Operator:** optionally drag the **price-per-kWh** or **system-capex** slider to show the crossover move in real time.
- **Talk track:** "Per shot the laser costs pennies against drones worth thousands. But the battery itself costs millions, so it only pays for itself past this crossover. We compute exactly where, for any assumptions you give us."
- **Proves:** #4 (economic payoff, quantified). (J, R strongly)

### Closer — `1:32–1:40`

- **Operator:** press **RESET** → returns to P1 landing state, ready for the next viewer.
- **Talk track:** "Believable engagement, an optimal targeting solver scored live, and the economics. That's BEAM."

---

## 3. Timing budget

| Segment | Duration | Cumulative |
|---|---|---|
| Cold open | 6s | 0:06 |
| Act 1 First Contact | 18s | 0:24 |
| → transition | 4s | 0:28 |
| Act 2 The Race | 22s | 0:50 |
| → transition | 4s | 0:54 |
| Act 3 Saturation | 20s | 1:14 |
| → transition | 4s | 1:18 |
| Act 4 Breakeven | 14s | 1:32 |
| Closer / reset | 8s | 1:40 |

Total ~100s with talk track; ~90s silent. Each act is independently runnable for a longer, deeper walkthrough.

---

## 4. Interaction map (exact controls per beat)

| Beat | Control used | Resulting state |
|---|---|---|
| Cold open | load `P1` (auto on boot) | paused, t=0 |
| Act 1 | `RUN` | P1 plays to completion |
| → | preset selector → `P2` | split view, paused |
| Act 2 | `RUN` | both panes play in lockstep |
| → | preset selector → `P3` | single view, paused, multi-solver chart |
| Act 3 | `RUN` | P3 plays; waves arrive |
| → | preset selector → `P4` | analysis/batch mode |
| Act 4 | (optional) cost sliders | crossover marker moves live |
| Closer | `RESET` | back to P1 landing |

Speed control (`1× / 2× / 4×`) is available throughout for recovery and for fitting time, but the canonical demo runs at `1×` for Acts 1–2 and may use `2×` for Act 3's later waves.

---

## 5. The five "money" frames (must be visually unmissable)

These are the screenshots the whole demo exists to produce. Engineers should explicitly tune rendering so each reads instantly on a projector:

1. **A beam mid-dwell** on a drone with its kill-progress ring half-shrunk. (Act 1)
2. **The split divergence**: left pane red leak flashing, right pane clean. (Act 2)
3. **The widening gap**: heuristic line dropping away from the optimal-reference ceiling during a wave spike. (Act 3)
4. **The cp_sat solve-time spike** labeled "throttled" beside `ga`'s flat cheap bar. (Act 3)
5. **The breakeven crossover** marker with its callout and green net-positive fill. (Act 4)

---

## 6. Demo-mode requirements (engineering)

- **Determinism:** all four presets pinned to seeds; reset returns to an identical state.
- **No cold-start stutter:** preload all four presets and warm the solvers on boot so the first `RUN` is instant. Pre-compute P4's sweep at build time and ship the series so Act 4 is instant.
- **Lockstep split (P2):** both panes share one simulation clock and one seeded swarm; never two independent RNG streams.
- **Graceful speed:** if the render loop ever falls behind telemetry, drop to interpolation-only and surface nothing to the viewer (never freeze, never show a spinner mid-demo).
- **Recovery affordance:** a hidden hotkey jumps directly to any preset, so a presenter can recover from any state in one keystroke.
- **Projector legibility:** see `design.md` for minimum sizes and contrast; the demo is the acceptance test for those.
- **Offline:** the demo must run fully local with no network dependency.

---

## 7. Pre-demo checklist

- [ ] Backend running, all four presets load without error.
- [ ] Solvers warmed (first run is instant).
- [ ] P4 sweep series present on disk.
- [ ] Display at demo resolution; fonts/contrast verified at projector distance.
- [ ] Speed at `1×`, audio off (unless using optional SFX).
- [ ] `RESET` confirmed to return to clean P1 landing.
- [ ] Recovery hotkeys confirmed.

---

## 8. Optional depth (for a longer session)

If a reviewer wants more, these are the supported tangents, each reachable from the live controls without leaving the app:

- Live-swap the active solver during P3 and watch the gap and leak rate change.
- Toggle weather (clear→fog) on a running scenario to show dwell-to-kill stretch.
- Add turrets (3→6) and watch saturation relieve.
- Open a per-turret thermal readout to show duty-cycle cooldowns forcing load-spreading.
- Run a fresh P4 sweep with edited cost assumptions to regenerate the crossover.

> The 90-second core is the default. The depth menu is opt-in. Never lead with depth; lead with the four proofs in order.
