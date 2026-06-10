// Top scoreboard (pdd.md section 14.1): kills, leaks, protected-value %, active
// solver, sim clock.
//
// Same split as controls.ts: a small DOM view (`Scoreboard`) over a pure model
// (`ScoreboardModel` + helpers). The model accumulates state from the telemetry
// stream; the view only formats and paints it. Tests drive the model directly,
// no DOM required.
//
// Inputs come straight off the validated wire types (FrameMessage / EpochMessage
// from src/types.ts). We never recompute backend numbers - we surface them.
// "Protected-value %" is the one derived figure, and its definition is documented
// where it is computed.

import {
  type EpochMessage,
  type FrameMessage,
} from "../types";

// --------------------------------------------------------------------------- //
// Model                                                                        //
// --------------------------------------------------------------------------- //

/** The denormalised numbers the scoreboard displays. */
export interface ScoreboardModel {
  /** Cumulative drones killed (from the latest frame). */
  kills: number;
  /** Cumulative drones leaked (reached the asset). */
  leaks: number;
  /**
   * Fraction in [0,1] of threat *value* that did NOT leak, i.e. protected.
   * Computed as 1 - leakedValue / engagedValue once value is known; until any
   * value has been observed it reads 1.0 (nothing lost yet).
   */
  protectedValueFrac: number;
  /** Name of the solver currently driving the live run. */
  activeSolver: string;
  /** Simulation clock (seconds) - the latest telemetry timestamp. */
  simClock: number;
  /** Latest epoch index seen (for context / debugging). */
  epoch: number;
}

export function emptyScoreboardModel(): ScoreboardModel {
  return {
    kills: 0,
    leaks: 0,
    protectedValueFrac: 1,
    activeSolver: "",
    simClock: 0,
    epoch: 0,
  };
}

/**
 * Running accumulator for the "value" side of protected-value %. The wire frame
 * gives us kills/leaks counts and per-drone value, but a drone that has left the
 * frame (killed or leaked) is no longer in `drones[]`, so we cannot recompute the
 * leaked value from a single frame. We therefore track it incrementally: each
 * frame we observe which drones newly entered the `leaked`/`dead` state and add
 * their last-known value to the appropriate bucket. The denominator is the total
 * value that ever entered the fight.
 */
export interface ValueAccumulator {
  /** Last-seen value per drone id (so we can attribute it on disappearance). */
  lastValue: Map<string, number>;
  /** Last-seen terminal state per id, to detect first transition. */
  counted: Set<string>;
  totalValue: number;
  leakedValue: number;
}

export function emptyValueAccumulator(): ValueAccumulator {
  return {
    lastValue: new Map(),
    counted: new Set(),
    totalValue: 0,
    leakedValue: 0,
  };
}

/**
 * Fold a frame into the value accumulator. Returns the same object (mutated) for
 * convenience. New drone ids contribute to `totalValue`; the first time a drone
 * is seen in a terminal state we attribute its value (to `leakedValue` for
 * "leaked"). Idempotent per-id via the `counted` set, so replaying a frame twice
 * does not double-count.
 */
export function foldFrameValue(
  acc: ValueAccumulator,
  frame: FrameMessage,
): ValueAccumulator {
  for (const d of frame.drones) {
    if (!acc.lastValue.has(d.id)) {
      acc.totalValue += d.value;
    }
    acc.lastValue.set(d.id, d.value);
    if ((d.state === "leaked" || d.state === "dead") && !acc.counted.has(d.id)) {
      acc.counted.add(d.id);
      if (d.state === "leaked") acc.leakedValue += d.value;
    }
  }
  return acc;
}

/**
 * Protected-value fraction in [0,1]. 1.0 when no value has entered yet (nothing
 * to lose). Clamped defensively in case telemetry counts leaked value before the
 * drone's spawn value was observed.
 */
export function protectedValueFraction(acc: ValueAccumulator): number {
  if (acc.totalValue <= 0) return 1;
  const frac = 1 - acc.leakedValue / acc.totalValue;
  return Math.min(1, Math.max(0, frac));
}

// --------------------------------------------------------------------------- //
// Formatting helpers (pure)                                                    //
// --------------------------------------------------------------------------- //

/** mm:ss.t simulation clock from a seconds value. */
export function formatClock(seconds: number): string {
  const s = Math.max(0, seconds);
  const mins = Math.floor(s / 60);
  const secs = Math.floor(s % 60);
  const tenths = Math.floor((s * 10) % 10);
  const mm = String(mins).padStart(2, "0");
  const ss = String(secs).padStart(2, "0");
  return `${mm}:${ss}.${tenths}`;
}

/** Whole-percent string, e.g. 0.873 -> "87%". */
export function formatPercent(frac: number): string {
  return `${Math.round(Math.min(1, Math.max(0, frac)) * 100)}%`;
}

// --------------------------------------------------------------------------- //
// DOM view                                                                     //
// --------------------------------------------------------------------------- //

export interface ScoreboardOptions {
  root: HTMLElement;
  /** Optional human label shown before the stats (e.g. solver name in race view). */
  title?: string;
  document?: Document;
}

type StatKey = "kills" | "leaks" | "protected" | "solver" | "clock";

/**
 * The top scoreboard. Call `applyFrame` / `applyEpoch` as telemetry arrives; the
 * view repaints from its internal model. In the solver-race split view one
 * Scoreboard is mounted under each battlefield.
 */
export class Scoreboard {
  private readonly doc: Document;
  private readonly root: HTMLElement;
  private readonly values: Partial<Record<StatKey, HTMLElement>> = {};

  readonly model: ScoreboardModel = emptyScoreboardModel();
  private readonly acc: ValueAccumulator = emptyValueAccumulator();

  constructor(opts: ScoreboardOptions) {
    this.root = opts.root;
    this.doc = opts.document ?? this.root.ownerDocument ?? globalThis.document;
    this.mount(opts.title);
  }

  /** Fold a telemetry frame: kills/leaks/clock + protected-value bookkeeping. */
  applyFrame(frame: FrameMessage): void {
    foldFrameValue(this.acc, frame);
    this.model.kills = frame.kills;
    this.model.leaks = frame.leaks;
    this.model.simClock = frame.t;
    this.model.protectedValueFrac = protectedValueFraction(this.acc);
    this.repaint();
  }

  /** Fold an epoch message: active solver + clock + epoch index. */
  applyEpoch(epoch: EpochMessage): void {
    this.model.activeSolver = epoch.active_solver;
    this.model.simClock = epoch.t;
    this.model.epoch = epoch.epoch;
    this.repaint();
  }

  /** Reset to the empty state (e.g. on Reset / new run). */
  reset(): void {
    Object.assign(this.model, emptyScoreboardModel());
    this.acc.lastValue.clear();
    this.acc.counted.clear();
    this.acc.totalValue = 0;
    this.acc.leakedValue = 0;
    this.repaint();
  }

  // --- DOM ---------------------------------------------------------------- //

  private mount(title?: string): void {
    this.root.classList.add("beam-scoreboard");
    this.root.replaceChildren();
    if (title) {
      const t = this.doc.createElement("div");
      t.className = "beam-scoreboard-title";
      t.textContent = title;
      this.root.appendChild(t);
    }
    const stats: [StatKey, string][] = [
      ["kills", "Kills"],
      ["leaks", "Leaks"],
      ["protected", "Protected"],
      ["solver", "Solver"],
      ["clock", "Clock"],
    ];
    for (const [key, label] of stats) {
      const cell = this.doc.createElement("div");
      cell.className = `beam-stat beam-stat-${key}`;
      const lab = this.doc.createElement("span");
      lab.className = "beam-stat-label";
      lab.textContent = label;
      const val = this.doc.createElement("span");
      val.className = "beam-stat-value";
      cell.appendChild(lab);
      cell.appendChild(val);
      this.root.appendChild(cell);
      this.values[key] = val;
    }
    this.repaint();
  }

  private repaint(): void {
    const set = (k: StatKey, v: string) => {
      const el = this.values[k];
      if (el) el.textContent = v;
    };
    set("kills", String(this.model.kills));
    set("leaks", String(this.model.leaks));
    set("protected", formatPercent(this.model.protectedValueFrac));
    set("solver", this.model.activeSolver || "-");
    set("clock", formatClock(this.model.simClock));
  }
}
