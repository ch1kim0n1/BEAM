# BEAM — UI/UX Design Specification

| Field | Value |
|---|---|
| Document type | Design specification (visual system + interaction + component contracts) |
| Status | v1.0 — authoritative for the frontend |
| Companion docs | `PRODUCT_DEVELOPMENT_DOCUMENT.md` (model), `demo.md` (run-of-show) |
| Audience | Frontend engineers, plus anyone touching visuals/motion |
| Renderer | Pixi.js for the battlefield canvas; standard DOM/CSS for panels & charts |

> The frontend carries this project. A correct backend with a muddy frontend is a failed demo. Treat this document with the same rigor as the model contract. Every token, state, and motion value below is a build target, not a suggestion. When something here conflicts with "looks fine to me," this document wins.

---

## 1. Design principles (north star)

1. **Legible at projector distance.** Every primary number and state must read from across a room. Minimum sizes and contrast ratios in §3 and §11 are hard floors.
2. **Honest data, always.** The UI never fakes a number. A throttled or bound-based optimality gap is labeled as such. A dropped frame is hidden, but a stale metric is never presented as live.
3. **Calm surface, tense content.** The chrome is quiet and dark so the engagement and the charts carry all the energy. No decorative noise competing with the beams.
4. **One glance, one truth.** Each region answers exactly one question: *what's happening* (battlefield), *how good is the decision* (gap chart), *does it pay off* (breakeven), *what's the score* (scoreboard).
5. **Motion is information.** Every animation encodes state (a slew shows retargeting, a pulse shows firing, a flash shows a leak). No motion is purely ornamental.

---

## 2. Layout system

### 2.1 Global frame

```
┌──────────────────────────────────────────────────────────────────────────┐
│  TOP BAR  ·  BEAM logo │ scenario name │ SCOREBOARD (kills·leaks·prot%·clk) │  56px
├───────────────┬───────────────────────────────────────┬────────────────────┤
│               │                                       │                    │
│  CONTROL      │            BATTLEFIELD                │   DASHBOARDS       │
│  PANEL        │            (Pixi canvas)              │   (stacked)        │
│               │                                       │                    │
│  left rail    │            center, fluid              │   right rail       │
│  300px        │                                       │   360px            │
│               │                                       │   ┌──────────────┐ │
│               │                                       │   │ GAP / COMPUTE│ │
│               │                                       │   ├──────────────┤ │
│               │                                       │   │ BREAKEVEN    │ │
│               │                                       │   └──────────────┘ │
├───────────────┴───────────────────────────────────────┴────────────────────┤
│  TRANSPORT BAR  ·  RUN/PAUSE │ STEP │ RESET │ speed 1×/2×/4× │ epoch readout  │  48px
└──────────────────────────────────────────────────────────────────────────┘
```

- **Top bar:** 56px fixed. Left: wordmark + active scenario. Right: scoreboard (§5).
- **Left rail (control panel):** 300px fixed, scrollable if overflow.
- **Center (battlefield):** fluid, takes all remaining width. This is the largest region by design.
- **Right rail (dashboards):** 360px fixed. Two stacked cards: gap/compute on top, breakeven below.
- **Transport bar:** 48px fixed bottom. Playback controls + epoch readout.

### 2.2 Solver-race split mode

Battlefield region splits into two equal panes side by side, each with a slim per-pane scoreboard header. Right-rail gap chart overlays both solvers' series; breakeven card is hidden in this mode (replaced by a "winner delta" stat: protected-% difference between panes).

```
┌──────── BATTLEFIELD (split) ────────┐
│  greedy_nearest    │   auction      │
│  PROT 78% LEAK 5   │  PROT 96% LEAK 1│  ← per-pane mini-scoreboard, 32px
│                    │                │
│   [pane A canvas]  │ [pane B canvas]│
│                    │                │
└────────────────────┴────────────────┘
```

### 2.3 Analysis / batch mode (P4)

Battlefield is replaced by a large breakeven plot; control panel swaps to sweep + cost controls. Top bar scoreboard hides; transport bar shows "sweep progress" instead of playback.

### 2.4 Breakpoints

- **Demo / desktop (≥1440px):** full three-column layout as above. This is the only layout the demo is tuned for.
- **Laptop (1024–1439px):** right rail collapses to a single togglable card; battlefield keeps priority.
- **Below 1024px:** unsupported for demo; show a "best viewed wider" notice. Do not waste effort on mobile.

---

## 3. Design tokens

### 3.1 Color

Dark tactical base with emerald friendly, amber urgency, red threat/leak. Hex values are exact.

```
/* surfaces */
--bg-base:        #070B0A;   /* app background, near-black green-tinted */
--bg-panel:       #0E1513;   /* rails, cards */
--bg-panel-2:     #131D1A;   /* raised elements, inputs */
--border:         #1E2C28;   /* hairlines, grid */
--border-strong:  #2A3D38;

/* friendly / system (emerald) */
--emerald-300:    #6EE7B7;
--emerald-400:    #34D399;   /* turrets, primary accent */
--emerald-500:    #10B981;   /* buttons, active states */
--beam-core:      #5EEAD4;   /* beam center */
--beam-glow:      #2EE6A6;   /* beam outer glow */

/* threat / urgency / failure */
--amber-400:      #FBBF24;   /* urgent / high-value target tint */
--red-500:        #EF4444;   /* leaks, danger */
--red-flash:      #FF5A5A;   /* leak flash peak */

/* data / reference */
--optimal:        #F5E6C8;   /* optimal-reference line (cream/gold), dashed */
--neutral-data:   #8AA39B;   /* secondary chart series */

/* text */
--text-hi:        #E6F4EF;   /* primary numbers, headings */
--text-mid:       #A9C2BA;   /* labels */
--text-low:       #6A847C;   /* captions, units, muted */
--text-disabled:  #44544F;
```

**Drone value→color mapping** (by class): `quad_small` = `--neutral-data`; `fixed_wing` (high value) = `--amber-400` outline + larger marker. Kill-progress ring = `--emerald-400`. Engaged target outline = `--beam-glow`.

**Semantic rule:** emerald = ours/working, amber = urgent/valuable, red = we lost something. Never use red for anything but danger/leaks.

### 3.2 Typography

```
--font-ui:    "Space Grotesk", "Inter", system-ui, sans-serif;   /* headings, labels */
--font-data:  "JetBrains Mono", "IBM Plex Mono", monospace;       /* all numbers, telemetry, code */

/* scale (px) — floors are projector-driven */
--t-display:  40 / 700   /* big scoreboard numbers (kills, protected%) */
--t-h1:       22 / 600
--t-h2:       16 / 600   /* card titles */
--t-body:     14 / 400
--t-label:    13 / 500   /* control labels, uppercase tracking +0.04em */
--t-caption:  12 / 400   /* units, footnotes */
--t-data-lg:  28 / 600   mono  /* live metric readouts */
--t-data:     14 / 500   mono
```

All numeric telemetry uses `--font-data` (mono) so digits don't jitter as values change. All labels use `--font-ui`. Card titles are uppercase with `+0.06em` tracking for a tactical/HUD feel.

### 3.3 Spacing, radius, elevation

```
--space: 4 · 8 · 12 · 16 · 24 · 32 · 48   /* 4px base scale */
--radius-sm: 4px    --radius-md: 8px    --radius-lg: 12px
--card-pad: 16px
--shadow-card:  0 1px 0 rgba(255,255,255,0.02) inset, 0 8px 24px rgba(0,0,0,0.4);
--glow-emerald: 0 0 12px rgba(46,230,166,0.45);
--glow-red:     0 0 16px rgba(255,90,90,0.5);
```

### 3.4 Motion

```
--dur-fast:   120ms   /* hovers, toggles */
--dur-base:   200ms   /* panel transitions, slew settle */
--dur-slow:   320ms   /* chart series tweens, mode switches */
--ease-out:   cubic-bezier(0.16, 1, 0.3, 1);    /* default UI */
--ease-inout: cubic-bezier(0.65, 0, 0.35, 1);   /* slew, camera */
--ease-snap:  cubic-bezier(0.34, 1.56, 0.64, 1);/* kill pop overshoot */
```

---

## 4. Battlefield canvas (Pixi)

The hero. Top-down 2D. World coordinates map to screen with a fixed scale and a small margin so nothing renders flush to the edge.

### 4.1 Coordinate & camera

- World origin at the **asset**, +x toward the swarm spawn edge. Fit-to-view so the full engagement envelope (asset + max detection range) is always visible. No panning/zoom in demo mode (avoid losing the viewer); a dev free-cam may exist behind a flag.
- Render at device pixel ratio; crisp lines at projector scale.

### 4.2 The asset

- Central diamond/hex emblem, `--emerald-400`, with a soft inner glow. A faint concentric **range ring** at each turret's max range (`--border`, 1px, dashed) so the viewer sees the defended envelope.
- On a leak: the asset pulses `--red-flash` once (see §7.3) and a small notch is removed from a "integrity" arc around it (purely visual reinforcement of `PROTECTED %`).

### 4.3 Turrets

A turret is an emplacement glyph with a barrel/aim indicator that rotates. Four visual states:

| State | Visual |
|---|---|
| `idle` | dim emerald body, barrel still, no beam |
| `slewing` | barrel rotates toward new aim with `--ease-inout` over the computed slew time; faint motion arc trails the barrel |
| `firing` | beam active (see §4.5); barrel locked on target; subtle emitter pulse at `--dur-fast` |
| `cooldown` | body desaturates toward `--text-low`, a thin **thermal arc** around the turret fills/empties; small "heat shimmer" overlay; cannot fire |

A **thermal ring** (0–100%) hugs each turret base at all times: `--emerald-400` filling to `--amber-400` near the cap, `--red-500` at forced cooldown.

### 4.4 Drones

- Marker shape by class: `quad_small` = small chevron, `fixed_wing` = larger arrow, amber-outlined (high value reads instantly).
- **Heading vector:** a 1px line from the marker showing velocity direction; length scales lightly with speed.
- **Kill-progress ring:** when engaged, an arc around the drone depletes from full to empty as delivered energy approaches `E_kill`. Color `--emerald-400`. This is the single most important per-drone affordance: it shows dwell happening.
- **States:** `alive` (class color), `engaged` (adds `--beam-glow` outline + progress ring), `dead` (kill pop, then removed), `leaked` (the drone reaches the asset → red streak into the asset + leak flash, then removed).

### 4.5 Beams

- A beam is a turret→target line with a bright `--beam-core` center and a wider `--beam-glow` additive bloom. Opacity/width scale with **delivered power** (so a fog-attenuated long-range shot visibly looks weaker/thinner than a clear close shot, reinforcing the physics).
- A faint heat-haze distortion at the impact point during firing.
- Beams never persist after a kill or retarget; they cut with the state change.

### 4.6 Weather overlay

- A full-canvas tint/texture per profile: `clear` none; `haze` slight desaturation; `rain` faint diagonal streaks + cool tint; `fog` heavy desaturation + reduced contrast + soft vignette; `dust` warm-brown haze. The overlay is subtle enough not to hide entities but obvious enough to read as "conditions got worse," matching the longer dwell times the model produces.

### 4.7 Performance rules (binding)

- **Sprite pooling:** allocate drone/beam/turret display objects once and reuse; never create/destroy per frame.
- **Interpolation:** render position is interpolated between telemetry frames so motion is smooth at 60fps even when telemetry streams at 20–30fps.
- **Batching:** use Pixi `ParticleContainer`/batched sprites for drones at high counts.
- **Frame-budget fallback:** if the render loop falls behind, drop to interpolation-only and skip non-essential effects (heat haze, debris) before ever dropping frame rate. Never show a spinner mid-engagement.

---

## 5. Scoreboard (top bar, right)

Four metrics, mono, large, with tiny uppercase labels above each:

```
  KILLS        LEAKS        PROTECTED        CLOCK
   12            0            100%           00:14.2
```

- `KILLS` emerald, `LEAKS` red (0 stays muted `--text-low`; >0 turns `--red-500` and the count briefly scales 1.15× on each increment).
- `PROTECTED %` = protected value / total threat value, the headline survivability number; `--t-display`. Color shifts emerald→amber→red as it falls past 90/75 thresholds.
- `CLOCK` = sim time, mono.
- In split mode, each pane gets its own compact scoreboard (§2.2).

---

## 6. Control panel (left rail)

Grouped sections, each a labeled card. Controls marked **[live]** apply to the running sim immediately; **[restart]** apply on next run.

### 6.1 Scenario

- **Preset selector** (segmented or dropdown): P1–P4 + "Custom". [restart]
- **Seed** (mono input). [restart]

### 6.2 Swarm

- **Count** slider 1–200 [restart], with live numeric readout.
- **Behavior** select: direct / flocking / staggered. [restart]
- **Class mix** (quad/fixed-wing %) two-handle slider. [restart]
- **Spawn geometry** select: line / arc / multi-arc. [restart]

### 6.3 Battery

- **Turrets** stepper 1–8. [restart]
- **Slew rate**, **power**, **range**, **thermal cap** sliders (advanced, collapsed by default). [live where safe, else restart]

### 6.4 Environment

- **Weather** select: clear / haze / rain / fog / dust. **[live]** (changes attenuation immediately; great for demo).

### 6.5 Solver

- **Active solver** select: greedy_nearest / greedy_threat / greedy_urgent / auction / ga / sa / cp_sat. **[live]** (swapping mid-run is logged and visible on the gap chart).
- **Solver-race toggle**: pick two solvers → enters split mode. [restart]
- **CP-SAT time budget** slider (advanced). [live]

### 6.6 Cost (visible in analysis mode)

- **price_per_kWh**, **system_capex**, **maintenance_rate**, **lifetime_engagements** sliders. **[live]** in P4 (crossover marker moves as you drag).

Control styling: labels `--t-label` uppercase `--text-mid`; sliders use an emerald fill track on `--bg-panel-2`; selects are dark with emerald focus ring; all focus states use `--glow-emerald`.

---

## 7. Motion & feedback spec

### 7.1 Slew

Barrel rotates from old aim to new over the model's slew time, `--ease-inout`. A faint arc trails to make the rotation legible. Never instant teleport, even at high speed multipliers (cap the visual minimum at 80ms so it's always perceptible).

### 7.2 Kill pop

On kill: target flashes white→class-color, scales 1.0→1.3→0 with `--ease-snap`, emits 4–6 short debris particles fading over 300ms. The owning turret begins its next slew on the same frame.

### 7.3 Leak flash

On leak: a `--red-flash` streak travels from the drone into the asset (120ms), the asset pulses red glow (`--glow-red`, 320ms decay), the scoreboard `LEAKS` increments with a 1.15× scale bounce, and the asset integrity arc loses a notch. Strong but brief; one beat of dread, then back to calm.

### 7.4 Chart transitions

New data points tween in over `--dur-slow`. The gap shading grows/shrinks smoothly; it never jumps. Solver-swap events drop a thin vertical marker on the gap chart timeline.

### 7.5 Mode switches

Battlefield ↔ split ↔ analysis cross-fade over `--dur-slow`; panels slide, never pop. Preset changes always land in a paused t=0 state.

---

## 8. Gap / compute dashboard (right rail, top)

Answers: *how good is the targeting decision, and at what compute cost?*

- **Title:** "OPTIMALITY GAP" (uppercase).
- **Primary plot:** time on x (epochs), objective value on y. One line per enabled solver (`--neutral-data` family, distinct dashes), plus the **optimal reference** as a `--optimal` dashed ceiling. The area between each heuristic and the optimal is **shaded** = the gap.
- **Live gap readout:** big mono number per active solver, e.g. `GAP 4.3%`, color emerald (<5%) → amber (5–15%) → red (>15%).
- **Solve-time strip:** a thin bar row under the plot, one bar per solver per epoch, height = solve_ms (log scale). `cp_sat` bars spike and, past the target threshold, are hatched + labeled **"THROTTLED / BOUND"**; heuristic bars stay short. This visual is the speed/quality tradeoff in one glance.
- **Honesty rule:** when the gap is measured against a bound rather than a proven optimum, the readout shows `~GAP (bound)` and the reference line switches to a dotted style. Never present a bound-based gap as exact.

```
 OPTIMALITY GAP                         GAP  4.3%
 obj │            ┄┄┄┄┄┄┄ optimal (ref) ┄┄┄┄┄
     │     ╱▔▔▔╲___╱▔▔  auction  (shaded gap)
     │  ╱╲╱        ╲    ga
     └───────────────────────────────  epochs →
 solve ms (log)  ▁▁▂▁  ▁▁▂▁   █(cp_sat throttled)
```

---

## 9. Breakeven dashboard (right rail, bottom / analysis center)

Answers: *does it pay off, and where?*

- **Title:** "COST EXCHANGE".
- **Live mode (during a run):** two stacked mono readouts, `COST` and `VALUE DESTROYED`, plus `NET` (red while negative, emerald once positive), updating per epoch.
- **Analysis mode (P4):** full plot, x = swarm size (or intercepts), `cumulative cost` line (near-flat) vs `value destroyed` line (steep). The **crossover** is marked with a vertical line + callout (`Net-positive past ~38 intercepts`) and the region right of it is filled `--emerald-500` at low opacity. Cost-assumption sliders move the crossover live.

```
 COST EXCHANGE
 $  │                         ╱ value destroyed
    │                    ╱▕  ← crossover (net-positive →) [green fill]
    │ ____________▕________
    │  cumulative cost (flat)
    └──────────────────────────────  swarm size →
```

---

## 10. States: empty, loading, paused, error, disconnected

- **Initial / empty:** battlefield shows asset + idle turrets + massed swarm at spawn, paused, single pulsing RUN. No charts populated yet (axes drawn, "awaiting run" caption `--text-low`).
- **Loading a preset:** cross-fade; never a blank screen.
- **Paused:** dim a 4px emerald border around the battlefield + "PAUSED" chip top-left of canvas; charts hold last values (not cleared).
- **Connection lost (WebSocket):** a small amber chip "RECONNECTING" top-right of the battlefield; freeze entities at last known position with a subtle pulse; auto-resume on reconnect. Never a full-screen error during a demo.
- **Solver/engine error:** non-blocking toast bottom-left, `--red-500` left border, plain-language message; the rest of the UI stays interactive.

---

## 11. Accessibility & projector legibility (hard floors)

- **Contrast:** all text ≥ 4.5:1 against its surface; primary numbers ≥ 7:1.
- **Min sizes on screen at demo resolution:** scoreboard display ≥ 32px; card titles ≥ 16px; no interactive label below 13px.
- **Color is never the only signal:** leaks also flash + bounce the counter; high-value drones are also larger, not just amber; the optimal line is also dashed, not just cream. (Protects colorblind viewers and survives projector color shift.)
- **Motion sensitivity:** a `reduce-motion` flag swaps pops/flashes for instant state changes and disables debris (off by default for demo).
- **Keyboard:** space = run/pause, `R` = reset, `1–4` = jump to preset, arrows = step. (Also the presenter recovery hotkeys from `demo.md`.)

---

## 12. Iconography & wordmark

- Minimal line icons, 1.5px stroke, `--text-mid`, emerald on active. Play/pause/step/reset/speed in the transport bar; a small gear for advanced control groups.
- **BEAM wordmark:** `--font-ui` 600, letter-spacing `+0.12em`, with a single emerald horizontal "beam" underline accent that subtly animates (a traveling highlight) only on the landing/idle state, never during a run.

---

## 13. Asset deliverables (what to produce)

- `frontend/src/theme/tokens.ts` — every token in §3 as exported constants; nothing hard-coded in components.
- Pixi texture atlas: turret (4 states), drone (2 classes), asset, debris particle, beam segment.
- Weather overlay shaders/filters (5 profiles).
- Chart components: gap/compute, breakeven (shared axis/tween utilities).
- A `docs/media/` capture script that auto-generates the five "money frames" from `demo.md` §5 for the README.

---

## 14. Definition of done (frontend, ties to PDD Phase 3)

- All four demo presets render and play per `demo.md`, at `1×`, smoothly, on the demo display.
- Every token, state, and motion value in this document is implemented; no hard-coded colors or magic durations.
- Gap and breakeven charts match backend epoch records exactly (contract test).
- Split mode runs two solvers in lockstep with divergent, legible outcomes.
- The five money frames are visually unmissable on a projector.
- Reduce-motion and keyboard controls work.
- No full-screen error or spinner can appear during a running demo.

> If the backend is right and this is right, the demo runs itself.
