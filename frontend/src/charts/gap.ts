// Gap-vs-compute chart (pdd.md 14.1 right dashboard, 9.3 optimality gap).
//
// Live multi-series view of the solver race: for every epoch each enabled solver
// reports an objective and a solve time. We draw:
//   - a line per solver of its objective over epochs (top panel),
//   - the optimality gap of the *active* solver shaded against the optimal
//     reference (the area between the active line and the optimal line),
//   - a per-epoch bar of each solver's solve time (bottom panel).
//
// The optimal reference is the solver entry flagged `is_optimal === true`
// (CP-SAT, pdd.md 9.2 #6). When CP-SAT only proved a bound, the gap is
// bound-based; we surface that in the readout and dash the reference line so the
// chart never presents an unverified value as a true gap (pdd.md 9.3).
//
// Data comes straight from the EpochMessage wire model in types.ts - no schema
// is redefined here.

import type { EpochMessage, EpochSolverEntry } from "../types";
import {
  clamp,
  DEFAULT_MARGINS,
  fmtCompact,
  fmtMs,
  FONT_LABEL,
  FONT_SMALL,
  linearScale,
  PALETTE,
  plotRect,
  seriesColor,
  setupCanvas,
  ticks,
  type ChartPalette,
  type Margins,
} from "./theme";

export interface GapChartOptions {
  /** Rolling window of epochs to keep (older points are dropped). */
  maxEpochs?: number;
  /** Reserve space so solve-time bars get the bottom slice of the plot. */
  solveTimeFraction?: number;
  margins?: Margins;
  palette?: ChartPalette;
}

interface EpochSample {
  epoch: number;
  activeSolver: string;
  /** solverName -> entry for this epoch. */
  entries: Map<string, EpochSolverEntry>;
}

/**
 * Stateful, dependency-free gap-vs-compute chart. Push one EpochMessage per
 * epoch with `push`, then `render` to a canvas. The instance owns the rolling
 * history so callers stay stateless.
 */
export class GapChart {
  private readonly maxEpochs: number;
  private readonly solveTimeFraction: number;
  private readonly margins: Margins;
  private readonly palette: ChartPalette;

  private readonly samples: EpochSample[] = [];
  /** Insertion-ordered set of solver names ever seen (stable legend order). */
  private readonly solverOrder: string[] = [];
  private readonly solverSeen = new Set<string>();

  constructor(opts: GapChartOptions = {}) {
    this.maxEpochs = Math.max(2, opts.maxEpochs ?? 240);
    this.solveTimeFraction = clamp(opts.solveTimeFraction ?? 0.28, 0.1, 0.5);
    this.margins = opts.margins ?? DEFAULT_MARGINS;
    this.palette = opts.palette ?? PALETTE;
  }

  /** Ingest one epoch of solver-race results. Ignores non-epoch messages. */
  push(msg: EpochMessage): void {
    const entries = new Map<string, EpochSolverEntry>();
    for (const e of msg.solvers) {
      entries.set(e.name, e);
      if (!this.solverSeen.has(e.name)) {
        this.solverSeen.add(e.name);
        this.solverOrder.push(e.name);
      }
    }
    this.samples.push({ epoch: msg.epoch, activeSolver: msg.active_solver, entries });
    if (this.samples.length > this.maxEpochs) {
      this.samples.splice(0, this.samples.length - this.maxEpochs);
    }
  }

  reset(): void {
    this.samples.length = 0;
    this.solverOrder.length = 0;
    this.solverSeen.clear();
  }

  get solvers(): readonly string[] {
    return this.solverOrder;
  }

  /** Name of the solver flagged optimal in the latest sample, if any. */
  private referenceName(): string | null {
    for (let i = this.samples.length - 1; i >= 0; i--) {
      for (const [name, e] of this.samples[i].entries) {
        if (e.is_optimal === true) return name;
      }
    }
    return null;
  }

  /**
   * Latest gap readout for the active solver, mirroring the backend gap exactly
   * (pdd.md 9.3). Returns null if no gap is available this epoch.
   */
  latestGap(): { value: number; boundBased: boolean; solver: string } | null {
    const last = this.samples[this.samples.length - 1];
    if (!last) return null;
    const active = last.entries.get(last.activeSolver);
    if (!active || active.gap == null) return null;
    return {
      value: active.gap,
      boundBased: active.gap_is_bound_based,
      solver: last.activeSolver,
    };
  }

  render(canvas: HTMLCanvasElement): void {
    const { ctx, width, height } = setupCanvas(canvas);
    const p = this.palette;

    ctx.fillStyle = p.background;
    ctx.fillRect(0, 0, width, height);

    const full = plotRect(width, height, this.margins);
    // Split the plot: objective lines on top, solve-time bars on the bottom.
    const stGap = 10;
    const stH = full.h * this.solveTimeFraction;
    const objRect = { x: full.x, y: full.y, w: full.w, h: full.h - stH - stGap };
    const stRect = { x: full.x, y: full.y + objRect.h + stGap, w: full.w, h: stH };

    if (this.samples.length === 0) {
      this.drawEmpty(ctx, width, height);
      return;
    }

    // ---- domains -------------------------------------------------------- //
    const epochMin = this.samples[0].epoch;
    const epochMax = this.samples[this.samples.length - 1].epoch;
    let objMax = 0;
    let stMax = 0;
    for (const s of this.samples) {
      for (const e of s.entries.values()) {
        if (Number.isFinite(e.objective)) objMax = Math.max(objMax, e.objective);
        if (Number.isFinite(e.solve_ms)) stMax = Math.max(stMax, e.solve_ms);
      }
    }
    objMax = objMax > 0 ? objMax * 1.08 : 1;
    stMax = stMax > 0 ? stMax * 1.15 : 1;

    const xs = linearScale(epochMin, epochMax, objRect.x, objRect.x + objRect.w);
    const ysObj = linearScale(0, objMax, objRect.y + objRect.h, objRect.y);
    const ysSt = linearScale(0, stMax, stRect.y + stRect.h, stRect.y);

    // ---- grid + axes ---------------------------------------------------- //
    this.drawGrid(ctx, objRect, xs, ysObj, objMax, "obj");
    this.drawGrid(ctx, stRect, xs, ysSt, stMax, "ms");

    // ---- gap shading (active vs optimal) -------------------------------- //
    const refName = this.referenceName();
    this.drawGapBand(ctx, xs, ysObj, refName);

    // ---- objective lines ------------------------------------------------ //
    for (const name of this.solverOrder) {
      this.drawSolverLine(ctx, xs, ysObj, name, name === refName);
    }

    // ---- solve-time bars (latest epoch, grouped) ----------------------- //
    this.drawSolveTimeBars(ctx, stRect, ysSt);

    // ---- legend + gap readout ------------------------------------------ //
    this.drawLegend(ctx, objRect, refName);
    this.drawGapReadout(ctx, objRect);
  }

  // --------------------------------------------------------------------- //

  private drawEmpty(ctx: CanvasRenderingContext2D, w: number, h: number): void {
    const p = this.palette;
    ctx.fillStyle = p.textMuted;
    ctx.font = FONT_LABEL;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("awaiting solver race…", w / 2, h / 2);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawGrid(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
    yMax: number,
    kind: "obj" | "ms",
  ): void {
    const p = this.palette;
    ctx.strokeStyle = p.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = p.textMuted;
    ctx.font = FONT_SMALL;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    for (const ty of ticks(0, yMax, kind === "ms" ? 2 : 3)) {
      const y = Math.round(ys.apply(ty)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(rect.x, y);
      ctx.lineTo(rect.x + rect.w, y);
      ctx.stroke();
      const label = kind === "ms" ? fmtMs(ty) : fmtCompact(ty);
      ctx.fillText(label, rect.x - 6, y);
    }

    // x ticks (epochs) only on the bottom panel.
    if (kind === "ms") {
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (const tx of ticks(xs.domainMin, xs.domainMax, 5)) {
        const x = Math.round(xs.apply(tx)) + 0.5;
        ctx.fillStyle = p.textMuted;
        ctx.fillText(String(Math.round(tx)), x, rect.y + rect.h + 6);
      }
    }

    // panel border
    ctx.strokeStyle = p.axis;
    ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w, rect.h);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawGapBand(
    ctx: CanvasRenderingContext2D,
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
    refName: string | null,
  ): void {
    if (!refName) return;
    // Build paired (optimal, active) points where both exist this epoch.
    const top: Array<[number, number]> = []; // optimal line
    const bot: Array<[number, number]> = []; // active line
    let anyBoundBased = false;
    for (const s of this.samples) {
      const ref = s.entries.get(refName);
      const active = s.entries.get(s.activeSolver);
      if (!ref || !active) continue;
      if (ref.is_optimal !== true) anyBoundBased = true;
      const x = xs.apply(s.epoch);
      top.push([x, ys.apply(ref.objective)]);
      bot.push([x, ys.apply(active.objective)]);
    }
    if (top.length < 2) return;

    ctx.beginPath();
    ctx.moveTo(top[0][0], top[0][1]);
    for (let i = 1; i < top.length; i++) ctx.lineTo(top[i][0], top[i][1]);
    for (let i = bot.length - 1; i >= 0; i--) ctx.lineTo(bot[i][0], bot[i][1]);
    ctx.closePath();
    // Amber for a true gap, muted/striped feel for bound-based uncertainty.
    ctx.fillStyle = anyBoundBased
      ? "rgba(251, 191, 36, 0.10)"
      : "rgba(251, 191, 36, 0.18)";
    ctx.fill();
  }

  private drawSolverLine(
    ctx: CanvasRenderingContext2D,
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
    name: string,
    isRef: boolean,
  ): void {
    const color = isRef ? this.palette.text : seriesColor(name, this.palette);
    ctx.strokeStyle = color;
    ctx.lineWidth = isRef ? 1.5 : 1.75;
    if (isRef) {
      // Dash the reference when its latest sample is only a bound, not proven.
      const last = this.samples[this.samples.length - 1];
      const e = last?.entries.get(name);
      ctx.setLineDash(e && e.is_optimal !== true ? [5, 4] : []);
    }
    ctx.beginPath();
    let started = false;
    for (const s of this.samples) {
      const e = s.entries.get(name);
      if (!e || !Number.isFinite(e.objective)) {
        started = false; // gap in the series -> break the line
        continue;
      }
      const x = xs.apply(s.epoch);
      const y = ys.apply(e.objective);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  private drawSolveTimeBars(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
    ys: ReturnType<typeof linearScale>,
  ): void {
    // Show the most recent epoch's solve times as a grouped bar cluster on the
    // right edge of the bottom panel - a compact "who's spending compute now".
    const last = this.samples[this.samples.length - 1];
    if (!last) return;
    const names = this.solverOrder.filter((n) => last.entries.has(n));
    if (names.length === 0) return;

    const clusterW = Math.min(rect.w * 0.5, 12 * names.length + 6);
    const x0 = rect.x + rect.w - clusterW - 4;
    const barW = Math.max(3, (clusterW - 6) / names.length - 3);
    const base = rect.y + rect.h;
    let bx = x0 + 3;
    for (const name of names) {
      const e = last.entries.get(name)!;
      const top = ys.apply(e.solve_ms);
      ctx.fillStyle = seriesColor(name, this.palette);
      ctx.globalAlpha = 0.85;
      ctx.fillRect(bx, top, barW, base - top);
      ctx.globalAlpha = 1;
      bx += barW + 3;
    }
    ctx.fillStyle = this.palette.textMuted;
    ctx.font = FONT_SMALL;
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillText("solve ms (latest)", rect.x + rect.w, rect.y + 2);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawLegend(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
    refName: string | null,
  ): void {
    const p = this.palette;
    ctx.font = FONT_SMALL;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    let lx = rect.x + 6;
    const ly = rect.y + 8;
    for (const name of this.solverOrder) {
      const isRef = name === refName;
      const color = isRef ? p.text : seriesColor(name, p);
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly - 4, 10, 8);
      lx += 14;
      const label = isRef ? `${name}*` : name;
      ctx.fillStyle = p.text;
      ctx.fillText(label, lx, ly);
      lx += ctx.measureText(label).width + 14;
    }
    ctx.textBaseline = "alphabetic";
  }

  private drawGapReadout(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
  ): void {
    const g = this.latestGap();
    if (!g) return;
    const p = this.palette;
    const pct = (g.value * 100).toFixed(1) + "%";
    const label = g.boundBased ? `gap≤ ${pct} (bound)` : `gap ${pct}`;
    ctx.font = FONT_LABEL;
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillStyle = g.boundBased ? p.textMuted : p.amber;
    ctx.fillText(label, rect.x + rect.w - 4, rect.y + 4);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }
}
