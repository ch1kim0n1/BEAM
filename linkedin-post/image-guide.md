# Image Guide

## Use These Screenshots (in demo/)

### Option A — `battlefield-early.png` (RECOMMENDED for first impression)
The opening frame: all 24 drones in flocking formation, long emerald beams lancing out from the four turrets at full 4000m range, depth-squashed range rings giving the 2.5D perspective, ground shadows under each drone, solver race chart initializing on the right. This is the "oh that's cool" screenshot — it reads immediately as a defense visualization, not a generic dashboard. Use this one.

### Option B — `battlefield-mid.png` (best for showing the analytical side)
t=13s: 11 kills, 0 leaks, the remaining swarm compressed into a tight cluster getting picked off, cost-exchange chart showing net +$90k with the breakeven crossover marker at t=4.0. Stronger if your audience is more analytics/OR than pure SWE. The "net+ @ t=4.0" annotation on the chart is a talking point in itself.

### Option C — `after-action-report.png` (use as a second image in a carousel)
The full after-action report: +$43k net position, 2.5× cost exchange, 100% protected, solver leaderboard with the auction solver highlighted at 3.1% gap / 1.9ms. If LinkedIn lets you post multiple images, use `battlefield-early.png` as image 1 and this as image 2 — the first gets the scroll-stop, the second rewards people who swipe.

---

## Screen Recording Script (Loom / QuickTime / OBS)
45-second script — do this in one take, no cuts needed:

1. **(0–5s)** Open http://localhost:5173. Let the dark tactical UI load. Don't say anything yet.
2. **(5–8s)** Click "▶ Load demo (swarm_24)". Pause half a second.
3. **(8–20s)** Watch the beams fire immediately at range — the whole ring of 24 drones gets engaged within the first second. Let 4-5 kills happen. The solver race chart on the right will start moving.
4. **(20–30s)** Hover over the cost-exchange chart on the right — show the "net+ @ t=4.0" crossover marker. That's the breakeven point.
5. **(30–40s)** Let the run finish. The after-action report opens automatically.
6. **(40–45s)** Slowly scroll the report to show: net +$43k, solver leaderboard (auction 3.1% gap at 1.9ms vs CP-SAT at 127ms), telemetry hash at the bottom.

**No narration needed** — the visuals carry it. If you want VO: "This is BEAM — the drone targeting problem is NP-complete, so I built a live solver race to show what the math actually looks like."

---

## Static Image Prompt (if you want a generated diagram instead)

**Midjourney / DALL-E / Ideogram:**
"Dark tactical operations dashboard, near-black background, four laser emplacements at center firing bright cyan beams outward to a ring of 24 small diamond-shaped drone targets at long range, tilted isometric ground plane with concentric elliptical range rings, small ground shadows under each drone, right panel shows two analytical charts with emerald and amber line graphs, monospace labels, high contrast, professional defense visualization aesthetic, no text overlay"
