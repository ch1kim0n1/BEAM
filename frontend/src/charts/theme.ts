// Shared chart theme + lightweight canvas drawing helpers.
//
// The dashboards (pdd.md 14.1, 14.3) are intentionally dependency-free: a thin
// layer over the 2D canvas API rather than a charting library, so they stay
// small, fast under live updates, and read clearly on a projected screen. The
// palette mirrors the tactical dark direction in pdd.md 14.3 (near-black
// background, emerald friendly, amber urgency, red leaks).

export interface ChartPalette {
  background: string;
  panel: string;
  grid: string;
  gridStrong: string;
  axis: string;
  text: string;
  textMuted: string;
  emerald: string;
  amber: string;
  red: string;
  cyan: string;
  /** Deterministic per-series colour wheel (solver lines). */
  series: readonly string[];
}

export const PALETTE: ChartPalette = {
  background: "#05080a",
  panel: "#0b1014",
  grid: "rgba(120, 160, 150, 0.08)",
  gridStrong: "rgba(120, 160, 150, 0.18)",
  axis: "rgba(150, 190, 180, 0.45)",
  text: "#cfe9df",
  textMuted: "rgba(160, 190, 180, 0.6)",
  emerald: "#34d399",
  amber: "#fbbf24",
  red: "#f87171",
  cyan: "#38bdf8",
  series: [
    "#34d399", // emerald
    "#38bdf8", // cyan
    "#fbbf24", // amber
    "#a78bfa", // violet
    "#f472b6", // pink
    "#4ade80", // green
    "#fb923c", // orange
    "#22d3ee", // teal
  ],
};

/** Stable colour for a named series (solver). Order-independent, deterministic. */
export function seriesColor(name: string, palette: ChartPalette = PALETTE): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) >>> 0;
  }
  return palette.series[h % palette.series.length];
}

export const FONT_SMALL = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
export const FONT_LABEL = "12px ui-monospace, SFMono-Regular, Menlo, monospace";

/** Inner plot rectangle after reserving margins for axes/labels. */
export interface Margins {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export const DEFAULT_MARGINS: Margins = { top: 16, right: 14, bottom: 28, left: 56 };

export interface PlotRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function plotRect(width: number, height: number, m: Margins): PlotRect {
  return {
    x: m.left,
    y: m.top,
    w: Math.max(1, width - m.left - m.right),
    h: Math.max(1, height - m.top - m.bottom),
  };
}

/**
 * Resize a canvas for the device pixel ratio and return its 2D context already
 * scaled so the rest of the drawing code can work in CSS pixels. Returns the
 * logical (CSS-pixel) width/height to lay out against.
 */
export function setupCanvas(
  canvas: HTMLCanvasElement,
  dpr: number = (typeof window !== "undefined" ? window.devicePixelRatio : 1) || 1,
): { ctx: CanvasRenderingContext2D; width: number; height: number } {
  // Fall back to attribute sizes when clientWidth is 0 (e.g. detached / tests).
  const cssWidth = canvas.clientWidth || canvas.width || 320;
  const cssHeight = canvas.clientHeight || canvas.height || 180;
  canvas.width = Math.max(1, Math.round(cssWidth * dpr));
  canvas.height = Math.max(1, Math.round(cssHeight * dpr));
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("2D canvas context unavailable");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, width: cssWidth, height: cssHeight };
}

/** A linear value->pixel mapping with a guarded (never-zero) span. */
export interface Scale {
  readonly domainMin: number;
  readonly domainMax: number;
  readonly rangeMin: number;
  readonly rangeMax: number;
  apply(v: number): number;
}

export function linearScale(
  domainMin: number,
  domainMax: number,
  rangeMin: number,
  rangeMax: number,
): Scale {
  let span = domainMax - domainMin;
  if (!Number.isFinite(span) || span === 0) span = 1;
  const m = (rangeMax - rangeMin) / span;
  return {
    domainMin,
    domainMax,
    rangeMin,
    rangeMax,
    apply(v: number): number {
      return rangeMin + (v - domainMin) * m;
    },
  };
}

/**
 * "Nice" rounded tick step for a given raw span and target tick count — the
 * classic 1/2/5 * 10^k progression. Pure + deterministic.
 */
export function niceStep(span: number, targetTicks: number): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const raw = span / Math.max(1, targetTicks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  let step: number;
  if (norm < 1.5) step = 1;
  else if (norm < 3) step = 2;
  else if (norm < 7) step = 5;
  else step = 10;
  return step * mag;
}

/** Tick values covering [min, max] on a nice step, inclusive of endpoints. */
export function ticks(min: number, max: number, targetTicks = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
    return [min];
  }
  const step = niceStep(max - min, targetTicks);
  const start = Math.ceil(min / step) * step;
  const out: number[] = [];
  // Guard against pathological loops from tiny steps.
  for (let v = start, i = 0; v <= max + step * 1e-9 && i < 1000; v += step, i++) {
    // Snap away -0 and floating dust.
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return out;
}

/** Compact human number for axis labels: 1.2k, 3.4M, 5.6B. */
export function fmtCompact(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e9) return (v / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
  if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
  if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
  if (a >= 1) return v.toFixed(0);
  if (a === 0) return "0";
  return v.toPrecision(2);
}

export function fmtMs(v: number): string {
  if (v >= 1000) return (v / 1000).toFixed(1) + "s";
  if (v >= 10) return v.toFixed(0) + "ms";
  return v.toFixed(1) + "ms";
}

export function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}
