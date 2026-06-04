// Cost breakeven chart (pdd.md 14.1 right dashboard, section 10 cost model).
//
// Two modes off the same instance:
//
//   LIVE  — cumulative cost vs value destroyed over sim time, with the
//           net-position (value_destroyed - cumulative_cost) zero crossover
//           highlighted. This is the per-run ledger telling the story that the
//           per-shot laser is cheap but the battery only goes net-positive once
//           enough value has been destroyed (pdd.md 10).
//
//   BATCH — net_position vs swept parameter (swarm size), with the zero
//           crossover point(s) marked. Sourced from BatchResultsResponse, the
//           breakeven curve the backend computes headless (pdd.md 10, 15 Ph2).
//
// All numbers come from the EpochMessage ledger / BatchResultsResponse wire
// models in types.ts — nothing is recomputed or re-defined here.

import type { BatchResultsResponse, EpochMessage } from "../types";
import {
  DEFAULT_MARGINS,
  fmtCompact,
  FONT_LABEL,
  FONT_SMALL,
  linearScale,
  PALETTE,
  plotRect,
  ticks,
  type ChartPalette,
  type Margins,
} from "./theme";

export interface BreakevenChartOptions {
  maxPoints?: number;
  margins?: Margins;
  palette?: ChartPalette;
}

interface LedgerPoint {
  t: number;
  cost: number;
  value: number;
  net: number;
}

/** Shape we read out of BatchResultsResponse.series / breakeven_crossover. */
interface BatchView {
  parameter: string;
  values: number[];
  net: number[];
  cost?: number[];
  value?: number[];
  /** Parameter value(s) at which net crosses zero, if the backend reported them. */
  crossovers: number[];
}

export class BreakevenChart {
  private readonly maxPoints: number;
  private readonly margins: Margins;
  private readonly palette: ChartPalette;

  private readonly live: LedgerPoint[] = [];
  private batch: BatchView | null = null;
  private mode: "live" | "batch" = "live";

  constructor(opts: BreakevenChartOptions = {}) {
    this.maxPoints = Math.max(2, opts.maxPoints ?? 600);
    this.margins = opts.margins ?? DEFAULT_MARGINS;
    this.palette = opts.palette ?? PALETTE;
  }

  /** Ingest one epoch's ledger for the live curve. */
  push(msg: EpochMessage): void {
    const l = msg.ledger;
    this.live.push({
      t: msg.t,
      cost: l.cumulative_cost,
      value: l.value_destroyed,
      net: l.net,
    });
    if (this.live.length > this.maxPoints) {
      this.live.splice(0, this.live.length - this.maxPoints);
    }
    this.mode = "live";
  }

  /** Switch to the batch breakeven view (net-position vs swept parameter). */
  setBatch(res: BatchResultsResponse): void {
    this.batch = parseBatch(res);
    this.mode = "batch";
  }

  showLive(): void {
    this.mode = "live";
  }

  reset(): void {
    this.live.length = 0;
    this.batch = null;
    this.mode = "live";
  }

  render(canvas: HTMLCanvasElement): void {
    if (this.mode === "batch" && this.batch) {
      this.renderBatch(canvas, this.batch);
    } else {
      this.renderLive(canvas);
    }
  }

  // --- live ------------------------------------------------------------- //

  private renderLive(canvas: HTMLCanvasElement): void {
    const { ctx, width, height } = setupCanvasLazy(canvas);
    const p = this.palette;
    ctx.fillStyle = p.background;
    ctx.fillRect(0, 0, width, height);
    const rect = plotRect(width, height, this.margins);

    if (this.live.length === 0) {
      drawEmpty(ctx, width, height, p, "awaiting ledger…");
      return;
    }

    const tMin = this.live[0].t;
    const tMax = this.live[this.live.length - 1].t;
    let yMax = 0;
    let yMin = 0;
    for (const d of this.live) {
      yMax = Math.max(yMax, d.cost, d.value);
      yMin = Math.min(yMin, d.net);
    }
    yMax = yMax > 0 ? yMax * 1.08 : 1;
    yMin = Math.min(0, yMin * 1.08);

    const xs = linearScale(tMin, tMax, rect.x, rect.x + rect.w);
    const ys = linearScale(yMin, yMax, rect.y + rect.h, rect.y);

    this.drawGrid(ctx, rect, xs, ys, yMin, yMax, "t");

    // Zero net baseline (the breakeven axis).
    const zeroY = ys.apply(0);
    ctx.strokeStyle = p.gridStrong;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(rect.x, zeroY);
    ctx.lineTo(rect.x + rect.w, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Shade net region above/below zero (emerald = net-positive, red = under).
    this.drawNetFill(ctx, xs, ys, zeroY);

    // cost (red) and value (emerald) cumulative lines.
    drawLine(ctx, this.live, xs, ys, (d) => d.cost, p.red, 1.75);
    drawLine(ctx, this.live, xs, ys, (d) => d.value, p.emerald, 1.75);
    // net (cyan) line on top.
    drawLine(ctx, this.live, xs, ys, (d) => d.net, p.cyan, 2);

    // Crossover marker: first epoch where net goes >= 0.
    this.markLiveCrossover(ctx, xs, ys);

    this.drawLiveLegend(ctx, rect);
    this.drawLiveReadout(ctx, rect);
  }

  private drawNetFill(
    ctx: CanvasRenderingContext2D,
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
    zeroY: number,
  ): void {
    if (this.live.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(xs.apply(this.live[0].t), zeroY);
    for (const d of this.live) ctx.lineTo(xs.apply(d.t), ys.apply(d.net));
    ctx.lineTo(xs.apply(this.live[this.live.length - 1].t), zeroY);
    ctx.closePath();
    // We can't easily two-tone a single fill; use a faint cyan wash and rely on
    // the zero line + net line colour for the read.
    ctx.fillStyle = "rgba(56, 189, 248, 0.10)";
    ctx.fill();
  }

  private markLiveCrossover(
    ctx: CanvasRenderingContext2D,
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
  ): void {
    for (let i = 1; i < this.live.length; i++) {
      const a = this.live[i - 1];
      const b = this.live[i];
      if (a.net < 0 && b.net >= 0) {
        // Linear interpolate the exact crossing time.
        const frac = (0 - a.net) / (b.net - a.net);
        const t = a.t + (b.t - a.t) * frac;
        const x = xs.apply(t);
        const y = ys.apply(0);
        this.drawCrossoverMarker(ctx, x, y, `net+ @ t=${t.toFixed(1)}`);
        return;
      }
    }
  }

  // --- batch ------------------------------------------------------------ //

  private renderBatch(canvas: HTMLCanvasElement, b: BatchView): void {
    const { ctx, width, height } = setupCanvasLazy(canvas);
    const p = this.palette;
    ctx.fillStyle = p.background;
    ctx.fillRect(0, 0, width, height);
    const rect = plotRect(width, height, this.margins);

    if (b.values.length === 0) {
      drawEmpty(ctx, width, height, p, "no batch results");
      return;
    }

    const xMin = b.values[0];
    const xMax = b.values[b.values.length - 1];
    let yMax = 0;
    let yMin = 0;
    for (const n of b.net) {
      if (Number.isFinite(n)) {
        yMax = Math.max(yMax, n);
        yMin = Math.min(yMin, n);
      }
    }
    if (yMax === yMin) {
      yMax += 1;
      yMin -= 1;
    } else {
      yMax *= 1.08;
      yMin = Math.min(0, yMin * 1.08);
    }

    const xs = linearScale(xMin, xMax, rect.x, rect.x + rect.w);
    const ys = linearScale(yMin, yMax, rect.y + rect.h, rect.y);

    this.drawGrid(ctx, rect, xs, ys, yMin, yMax, "param", b.values);

    const zeroY = ys.apply(0);
    ctx.strokeStyle = p.gridStrong;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(rect.x, zeroY);
    ctx.lineTo(rect.x + rect.w, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // net-position polyline + points.
    const pts = b.values.map((v, i) => ({ v, n: b.net[i] }));
    ctx.strokeStyle = p.cyan;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    for (const { v, n } of pts) {
      if (!Number.isFinite(n)) {
        started = false;
        continue;
      }
      const x = xs.apply(v);
      const y = ys.apply(n);
      started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      started = true;
    }
    ctx.stroke();
    for (const { v, n } of pts) {
      if (!Number.isFinite(n)) continue;
      ctx.fillStyle = n >= 0 ? p.emerald : p.red;
      ctx.beginPath();
      ctx.arc(xs.apply(v), ys.apply(n), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Crossover(s): prefer backend-reported, else interpolate sign changes.
    const crossings = b.crossovers.length ? b.crossovers : interpCrossings(pts);
    for (const cx of crossings) {
      this.drawCrossoverMarker(
        ctx,
        xs.apply(cx),
        zeroY,
        `breakeven ≈ ${fmtCompact(cx)}`,
      );
    }

    this.drawBatchLabels(ctx, rect, b.parameter);
  }

  // --- shared chrome ---------------------------------------------------- //

  private drawGrid(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
    xs: ReturnType<typeof linearScale>,
    ys: ReturnType<typeof linearScale>,
    yMin: number,
    yMax: number,
    xKind: "t" | "param",
    explicitXTicks?: number[],
  ): void {
    const p = this.palette;
    ctx.strokeStyle = p.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = p.textMuted;
    ctx.font = FONT_SMALL;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    for (const ty of ticks(yMin, yMax, 4)) {
      const y = Math.round(ys.apply(ty)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(rect.x, y);
      ctx.lineTo(rect.x + rect.w, y);
      ctx.stroke();
      ctx.fillText(fmtCompact(ty), rect.x - 6, y);
    }

    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const xt =
      explicitXTicks && explicitXTicks.length <= 12
        ? explicitXTicks
        : ticks(xs.domainMin, xs.domainMax, 5);
    for (const tx of xt) {
      const x = Math.round(xs.apply(tx)) + 0.5;
      const lbl = xKind === "t" ? tx.toFixed(0) : fmtCompact(tx);
      ctx.fillText(lbl, x, rect.y + rect.h + 6);
    }

    ctx.strokeStyle = p.axis;
    ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w, rect.h);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawCrossoverMarker(
    ctx: CanvasRenderingContext2D,
    x: number,
    y: number,
    label: string,
  ): void {
    const p = this.palette;
    ctx.strokeStyle = p.amber;
    ctx.lineWidth = 1.25;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, y - 60);
    ctx.lineTo(x, y + 8);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = p.amber;
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.font = FONT_SMALL;
    ctx.textBaseline = "bottom";
    // Keep the label inside the canvas horizontally.
    ctx.textAlign = "center";
    ctx.fillText(label, x, y - 64);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawLiveLegend(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
  ): void {
    const p = this.palette;
    const items: Array<[string, string]> = [
      ["value", p.emerald],
      ["cost", p.red],
      ["net", p.cyan],
    ];
    ctx.font = FONT_SMALL;
    ctx.textBaseline = "middle";
    let lx = rect.x + 6;
    const ly = rect.y + 8;
    for (const [name, color] of items) {
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly - 4, 10, 8);
      lx += 14;
      ctx.fillStyle = p.text;
      ctx.fillText(name, lx, ly);
      lx += ctx.measureText(name).width + 14;
    }
    ctx.textBaseline = "alphabetic";
  }

  private drawLiveReadout(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
  ): void {
    const last = this.live[this.live.length - 1];
    if (!last) return;
    const p = this.palette;
    ctx.font = FONT_LABEL;
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillStyle = last.net >= 0 ? p.emerald : p.red;
    const sign = last.net >= 0 ? "+" : "";
    ctx.fillText(`net ${sign}${fmtCompact(last.net)}`, rect.x + rect.w - 4, rect.y + 4);
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
  }

  private drawBatchLabels(
    ctx: CanvasRenderingContext2D,
    rect: { x: number; y: number; w: number; h: number },
    parameter: string,
  ): void {
    const p = this.palette;
    ctx.font = FONT_SMALL;
    ctx.fillStyle = p.textMuted;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.fillText(parameter, rect.x + rect.w / 2, rect.y + rect.h + 26);
    ctx.textAlign = "left";
    ctx.fillStyle = p.text;
    ctx.textBaseline = "top";
    ctx.font = FONT_LABEL;
    ctx.fillText("net position vs swarm size", rect.x + 6, rect.y + 4);
    ctx.textBaseline = "alphabetic";
  }
}

// --------------------------------------------------------------------------- //
// pure helpers                                                                //
// --------------------------------------------------------------------------- //

function drawLine(
  ctx: CanvasRenderingContext2D,
  pts: LedgerPoint[],
  xs: ReturnType<typeof linearScale>,
  ys: ReturnType<typeof linearScale>,
  pick: (d: LedgerPoint) => number,
  color: string,
  width: number,
): void {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  let started = false;
  for (const d of pts) {
    const v = pick(d);
    if (!Number.isFinite(v)) {
      started = false;
      continue;
    }
    const x = xs.apply(d.t);
    const y = ys.apply(v);
    started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    started = true;
  }
  ctx.stroke();
}

function drawEmpty(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  p: ChartPalette,
  text: string,
): void {
  ctx.fillStyle = p.textMuted;
  ctx.font = FONT_LABEL;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, w / 2, h / 2);
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
}

/** Interpolate parameter values where a net series crosses zero. Exported for tests. */
export function interpCrossings(pts: Array<{ v: number; n: number }>): number[] {
  const out: number[] = [];
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1];
    const b = pts[i];
    if (!Number.isFinite(a.n) || !Number.isFinite(b.n)) continue;
    if ((a.n < 0 && b.n >= 0) || (a.n > 0 && b.n <= 0)) {
      const frac = (0 - a.n) / (b.n - a.n);
      out.push(a.v + (b.v - a.v) * frac);
    }
  }
  return out;
}

/**
 * Normalise a BatchResultsResponse into the minimal view this chart draws. The
 * backend `series` map is loosely typed on the wire (Record<string, unknown>),
 * so we defensively pull the net-position series and any crossover values.
 */
export function parseBatch(res: BatchResultsResponse): BatchView {
  const values = Array.isArray(res.values) ? res.values.map(Number) : [];
  const series = (res.series ?? {}) as Record<string, unknown>;

  const net =
    pickNumberArray(series, ["net", "net_position", "net_position_vs_swarm_size"]) ??
    deriveNet(series) ??
    values.map(() => NaN);

  const view: BatchView = {
    parameter: res.parameter ?? "swarm_spec.count",
    values,
    net,
    cost: pickNumberArray(series, ["cost", "cumulative_cost"]) ?? undefined,
    value: pickNumberArray(series, ["value", "value_destroyed"]) ?? undefined,
    crossovers: extractCrossovers(res.breakeven_crossover),
  };
  return view;
}

function pickNumberArray(
  obj: Record<string, unknown>,
  keys: string[],
): number[] | null {
  for (const k of keys) {
    const v = obj[k];
    if (Array.isArray(v) && v.every((x) => typeof x === "number")) {
      return v as number[];
    }
  }
  return null;
}

function deriveNet(series: Record<string, unknown>): number[] | null {
  const cost = pickNumberArray(series, ["cost", "cumulative_cost"]);
  const value = pickNumberArray(series, ["value", "value_destroyed"]);
  if (cost && value && cost.length === value.length) {
    return value.map((v, i) => v - cost[i]);
  }
  return null;
}

function extractCrossovers(raw: unknown): number[] {
  if (raw == null) return [];
  if (typeof raw === "number" && Number.isFinite(raw)) return [raw];
  if (Array.isArray(raw)) {
    return raw.filter((x): x is number => typeof x === "number" && Number.isFinite(x));
  }
  if (typeof raw === "object") {
    const o = raw as Record<string, unknown>;
    for (const k of ["crossover", "value", "swarm_size", "x"]) {
      const v = o[k];
      if (typeof v === "number" && Number.isFinite(v)) return [v];
    }
    // {crossovers: [...]} form
    const arr = o["crossovers"];
    if (Array.isArray(arr)) {
      return arr.filter((x): x is number => typeof x === "number");
    }
  }
  return [];
}

// Local re-export to keep setupCanvas import lean while letting tests inject a
// fake canvas. Kept thin on purpose.
import { setupCanvas } from "./theme";
function setupCanvasLazy(canvas: HTMLCanvasElement) {
  return setupCanvas(canvas);
}
