// After-action report (post-run summary overlay).
//
// When a run ends, the backend has already aggregated the authoritative figures
// into RunSummary (kills, leaks, leaked value, the final cost ledger, and per-
// solver average optimality gap + solve time) and persisted a determinism hash
// over the full telemetry stream. This module surfaces that as a full-screen
// "After-Action Report" - the decision-maker's read on a single engagement:
//
//   • Outcome     - kills, leaks, protected-value %.
//   • Cost exchange - the headline. Net position ($), cost-exchange ratio
//                     (value destroyed per $ spent), cost-per-kill, and the
//                     cost breakdown (shot energy / maintenance / amortized capex).
//   • Solvers     - leaderboard of every solver evaluated in the race: average
//                   optimality gap vs the reference and average decision latency,
//                   so "which policy, and what did it cost us in compute" is one
//                   glance.
//   • Provenance  - the SHA-256 telemetry hash: this run is bit-for-bit
//                   reproducible from its seed, which is the claim that matters
//                   for an audited, deterministic decision system.
//
// Split like the rest of panels/: pure helpers (metric derivation + formatting)
// over a thin DOM view. Every number originates from the validated wire models in
// ../types - nothing is recomputed against the engine, only derived for display.

import type { RunSummary, RunSummaryResponse } from "../types";

// --------------------------------------------------------------------------- //
// Inputs                                                                       //
// --------------------------------------------------------------------------- //

/** Live context the summary endpoint does not carry, captured by the host app. */
export interface RunReportContext {
  /** Human label for the scenario (e.g. the preset name "swarm_24"). */
  scenarioLabel?: string;
  /** Seed the run was driven with, if known (presets carry their own). */
  seed?: number;
  /** Active (driving) solver - highlighted in the leaderboard. */
  activeSolver?: string;
  /** Reference solver (optimal baseline gaps are measured against), e.g. cp_sat. */
  referenceSolver?: string;
  /** Protected-value fraction [0,1] as tracked live by the scoreboard. */
  protectedValueFrac?: number;
  /** Simulation duration (seconds) - the latest telemetry clock. */
  simSeconds?: number;
  /** Epochs (decision periods) elapsed. */
  epochs?: number;
}

export interface RunReportData {
  response: RunSummaryResponse;
  context: RunReportContext;
}

// --------------------------------------------------------------------------- //
// Derived metrics (pure)                                                       //
// --------------------------------------------------------------------------- //

export interface SolverRow {
  name: string;
  avgGap: number | null; // fraction [0,1]
  avgSolveMs: number | null;
  isActive: boolean;
  isReference: boolean;
}

export interface ReportMetrics {
  kills: number;
  leaks: number;
  engaged: number; // kills + leaks (drones that reached a terminal state)
  protectedValueFrac: number | null;
  valueDestroyed: number;
  leakedValue: number;
  cost: number;
  net: number;
  /** value destroyed per $ spent. >1 means the defense is winning the exchange. */
  costExchangeRatio: number | null;
  /** $ spent per drone killed. */
  costPerKill: number | null;
  shotEnergyCost: number;
  maintenanceCost: number;
  capexAmortized: number;
  engagements: number;
  /** Average decision latency (ms) of the active solver, if known. */
  activeSolveMs: number | null;
  /** Average optimality gap (fraction) of the active solver, if known. */
  activeGap: number | null;
  solvers: SolverRow[];
}

/**
 * Derive every displayed figure from a RunSummary + host context. Defensive
 * against missing/zero denominators (returns null rather than Infinity/NaN).
 */
export function computeReportMetrics(
  summary: RunSummary,
  ctx: RunReportContext,
): ReportMetrics {
  const ledger = summary.final_ledger;
  const cost = ledger.cumulative_cost;
  const valueDestroyed = ledger.value_destroyed;
  const net = ledger.net;
  const kills = summary.kills;
  const leaks = summary.leaks;

  const names = new Set<string>([
    ...Object.keys(summary.avg_gap_by_solver ?? {}),
    ...Object.keys(summary.avg_solve_ms_by_solver ?? {}),
  ]);
  const solvers: SolverRow[] = [...names].map((name) => ({
    name,
    avgGap: numOrNull(summary.avg_gap_by_solver?.[name]),
    avgSolveMs: numOrNull(summary.avg_solve_ms_by_solver?.[name]),
    isActive: name === ctx.activeSolver,
    isReference: name === ctx.referenceSolver,
  }));
  // Sort: reference first, then by ascending gap (best policy on top).
  solvers.sort((a, b) => {
    if (a.isReference !== b.isReference) return a.isReference ? -1 : 1;
    return (a.avgGap ?? Infinity) - (b.avgGap ?? Infinity);
  });

  return {
    kills,
    leaks,
    engaged: kills + leaks,
    protectedValueFrac: numOrNull(ctx.protectedValueFrac),
    valueDestroyed,
    leakedValue: summary.leaked_value,
    cost,
    net,
    costExchangeRatio: cost > 0 ? valueDestroyed / cost : null,
    costPerKill: kills > 0 ? cost / kills : null,
    shotEnergyCost: ledger.shot_energy_cost,
    maintenanceCost: ledger.maintenance_cost,
    capexAmortized: ledger.capex_amortized,
    engagements: ledger.engagements,
    activeSolveMs: numOrNull(
      ctx.activeSolver ? summary.avg_solve_ms_by_solver?.[ctx.activeSolver] : null,
    ),
    activeGap: numOrNull(
      ctx.activeSolver ? summary.avg_gap_by_solver?.[ctx.activeSolver] : null,
    ),
    solvers,
  };
}

function numOrNull(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// --------------------------------------------------------------------------- //
// Formatting (pure)                                                            //
// --------------------------------------------------------------------------- //

/** Compact currency, e.g. 1234567 -> "$1.23M", -800 -> "-$800". */
export function fmtMoney(v: number, signed = false): string {
  if (!Number.isFinite(v)) return "-";
  const sign = v < 0 ? "-" : signed ? "+" : "";
  const a = Math.abs(v);
  let body: string;
  if (a >= 1e9) body = (a / 1e9).toFixed(a >= 1e10 ? 0 : 2) + "B";
  else if (a >= 1e6) body = (a / 1e6).toFixed(a >= 1e7 ? 0 : 2) + "M";
  else if (a >= 1e3) body = (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
  else body = a.toFixed(a < 10 && a !== Math.floor(a) ? 1 : 0);
  return `${sign}$${body}`;
}

export function fmtPct(frac: number | null, digits = 0): string {
  if (frac == null || !Number.isFinite(frac)) return "-";
  return `${(frac * 100).toFixed(digits)}%`;
}

/**
 * Format an optimality gap. The wire contract treats `gap` as a fraction (e.g.
 * 0.03 -> "3.0%"), matching the live gap chart. Guard against non-fractional /
 * pathological values (a near-zero optimal reference makes the normalized ratio
 * blow up): anything outside a sane band is shown as a compact magnitude rather
 * than an absurd 15-digit percentage, so the report never prints garbage.
 */
export function fmtGap(frac: number | null): string {
  if (frac == null || !Number.isFinite(frac)) return "-";
  if (Math.abs(frac) <= 5) return `${(frac * 100).toFixed(1)}%`;
  // Out-of-band: render the raw magnitude compactly (e.g. "-1.2e13").
  return frac.toExponential(1);
}

export function fmtMs(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "-";
  if (v >= 1000) return (v / 1000).toFixed(2) + " s";
  if (v >= 10) return v.toFixed(0) + " ms";
  return v.toFixed(1) + " ms";
}

export function fmtRatio(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return "-";
  return `${v.toFixed(v >= 10 ? 0 : 1)}×`;
}

export function fmtClock(seconds: number | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "-";
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

/** Short hash for display: first 10 + last 6 of a hex digest. */
export function shortHash(h: string | null | undefined): string {
  if (!h) return "-";
  const hex = h.startsWith("sha256:") ? h.slice(7) : h;
  if (hex.length <= 20) return hex;
  return `${hex.slice(0, 10)}…${hex.slice(-6)}`;
}

// --------------------------------------------------------------------------- //
// DOM view                                                                     //
// --------------------------------------------------------------------------- //

export interface RunReportOptions {
  /** Element the overlay mounts into (positioned over the stage). */
  root: HTMLElement;
  document?: Document;
  onClose?: () => void;
  onReplay?: () => void;
}

/**
 * Full-screen after-action report overlay. `show(data)` renders and reveals it;
 * `hide()` removes it from view (kept in the DOM, cheap to re-show).
 */
export class RunReport {
  private readonly doc: Document;
  private readonly root: HTMLElement;
  private readonly onClose?: () => void;
  private readonly onReplay?: () => void;
  private overlay: HTMLElement | null = null;
  private last: RunReportData | null = null;

  constructor(opts: RunReportOptions) {
    this.root = opts.root;
    this.doc = opts.document ?? this.root.ownerDocument ?? globalThis.document;
    this.onClose = opts.onClose;
    this.onReplay = opts.onReplay;
  }

  get visible(): boolean {
    return this.overlay?.classList.contains("is-open") ?? false;
  }

  /** Re-show the most recent report, if one was rendered. */
  reopen(): void {
    if (this.last) this.show(this.last);
  }

  show(data: RunReportData): void {
    this.last = data;
    const summary = data.response.summary;
    if (!summary) {
      // No aggregated summary yet (run still finalizing): show a minimal note.
      this.render(this.renderPending(data));
      return;
    }
    const m = computeReportMetrics(summary, data.context);
    this.render(this.renderReport(m, data));
  }

  hide(): void {
    this.overlay?.classList.remove("is-open");
  }

  private render(card: HTMLElement): void {
    if (!this.overlay) {
      this.overlay = this.div("beam-report-overlay");
      this.overlay.addEventListener("click", (e) => {
        if (e.target === this.overlay) this.close();
      });
      this.root.appendChild(this.overlay);
    }
    this.overlay.replaceChildren(card);
    // Force reflow-free open on next frame so the transition runs.
    this.overlay.classList.add("is-open");
  }

  private close(): void {
    this.hide();
    this.onClose?.();
  }

  // --- builders ----------------------------------------------------------- //

  private renderReport(m: ReportMetrics, data: RunReportData): HTMLElement {
    const ctx = data.context;
    const card = this.div("beam-report");

    // Header.
    const head = this.div("beam-report-head");
    const title = this.div("beam-report-title");
    title.innerHTML = `After-Action Report <span class="beam-report-scn">${escapeHtml(
      ctx.scenarioLabel ?? data.response.run_id,
    )}</span>`;
    const verdict = m.net >= 0 ? "NET-POSITIVE ENGAGEMENT" : "NET-NEGATIVE ENGAGEMENT";
    const sub = this.div("beam-report-sub");
    sub.textContent = `${verdict} · ${m.kills} killed · ${m.leaks} leaked · ${fmtClock(
      ctx.simSeconds,
    )} sim`;
    head.appendChild(title);
    head.appendChild(sub);
    card.appendChild(head);

    // Headline KPI strip.
    const kpis = this.div("beam-report-kpis");
    kpis.appendChild(
      this.kpi("Net position", fmtMoney(m.net, true), m.net >= 0 ? "pos" : "neg", "value destroyed − cost"),
    );
    kpis.appendChild(
      this.kpi("Cost exchange", fmtRatio(m.costExchangeRatio), m.costExchangeRatio != null && m.costExchangeRatio >= 1 ? "pos" : "neg", "$ destroyed per $ spent"),
    );
    kpis.appendChild(this.kpi("Protected value", fmtPct(m.protectedValueFrac), "accent", "of threat value defended"));
    kpis.appendChild(this.kpi("Cost / kill", fmtMoney(m.costPerKill ?? NaN), "neutral", "spend per drone down"));
    card.appendChild(kpis);

    // Two-column body: cost breakdown + outcome | solver leaderboard.
    const body = this.div("beam-report-body");

    // Left: cost & outcome detail.
    const left = this.div("beam-report-col");
    left.appendChild(this.sectionTitle("Cost exchange"));
    left.appendChild(
      this.rows([
        ["Value destroyed", fmtMoney(m.valueDestroyed)],
        ["Total cost", fmtMoney(m.cost)],
        ["- shot energy", fmtMoney(m.shotEnergyCost)],
        ["- maintenance", fmtMoney(m.maintenanceCost)],
        ["- capex (amortized)", fmtMoney(m.capexAmortized)],
        ["Engagements opened", String(m.engagements)],
      ]),
    );
    left.appendChild(this.sectionTitle("Outcome"));
    left.appendChild(
      this.rows([
        ["Drones killed", String(m.kills)],
        ["Drones leaked", String(m.leaks)],
        ["Leaked value", fmtMoney(m.leakedValue)],
        ["Protected value", fmtPct(m.protectedValueFrac)],
      ]),
    );
    body.appendChild(left);

    // Right: solver leaderboard + provenance.
    const right = this.div("beam-report-col");
    right.appendChild(this.sectionTitle("Solver leaderboard"));
    right.appendChild(this.solverTable(m));
    right.appendChild(this.sectionTitle("Run provenance"));
    right.appendChild(
      this.rows([
        ["Run ID", data.response.run_id],
        ["Seed", ctx.seed != null ? String(ctx.seed) : "-"],
        ["Epochs", ctx.epochs != null ? String(ctx.epochs) : "-"],
        ["Active solver", ctx.activeSolver ?? "-"],
        ["Decision latency", fmtMs(m.activeSolveMs)],
        ["Telemetry hash", shortHash(data.response.telemetry_hash)],
      ]),
    );
    const note = this.div("beam-report-note");
    note.textContent =
      "Deterministic: identical seed + scenario reproduce this telemetry bit-for-bit (hash above).";
    right.appendChild(note);
    body.appendChild(right);

    card.appendChild(body);
    card.appendChild(this.actions(data));
    return card;
  }

  private renderPending(data: RunReportData): HTMLElement {
    const card = this.div("beam-report");
    const head = this.div("beam-report-head");
    const title = this.div("beam-report-title");
    title.textContent = "After-Action Report";
    head.appendChild(title);
    const sub = this.div("beam-report-sub");
    sub.textContent = `Run ${data.response.run_id} - summary not available yet (status: ${data.response.status}).`;
    head.appendChild(sub);
    card.appendChild(head);
    card.appendChild(this.actions(data));
    return card;
  }

  private actions(data: RunReportData): HTMLElement {
    const bar = this.div("beam-report-actions");
    if (this.onReplay) {
      const replay = this.button("↻ Replay demo", "beam-report-btn primary");
      replay.addEventListener("click", () => {
        this.close();
        this.onReplay?.();
      });
      bar.appendChild(replay);
    }
    const exportBtn = this.button("Export JSON", "beam-report-btn");
    exportBtn.addEventListener("click", () => this.exportJson(data));
    bar.appendChild(exportBtn);
    const close = this.button("Close", "beam-report-btn");
    close.addEventListener("click", () => this.close());
    bar.appendChild(close);
    return bar;
  }

  private solverTable(m: ReportMetrics): HTMLElement {
    const wrap = this.div("beam-report-table");
    const header = this.div("beam-report-trow head");
    for (const [c, label] of [
      ["solver", "Solver"],
      ["gap", "Avg gap"],
      ["ms", "Avg solve"],
    ] as const) {
      const cell = this.div(`beam-report-tcell ${c}`);
      cell.textContent = label;
      header.appendChild(cell);
    }
    wrap.appendChild(header);
    if (m.solvers.length === 0) {
      const empty = this.div("beam-report-trow");
      const cell = this.div("beam-report-tcell");
      cell.textContent = "no solver data";
      empty.appendChild(cell);
      wrap.appendChild(empty);
      return wrap;
    }
    for (const s of m.solvers) {
      const row = this.div("beam-report-trow" + (s.isActive ? " active" : ""));
      const name = this.div("beam-report-tcell solver");
      name.textContent = s.name + (s.isReference ? " *" : "") + (s.isActive ? "  ◂ active" : "");
      const gap = this.div("beam-report-tcell gap");
      gap.textContent = s.isReference ? "ref" : fmtGap(s.avgGap);
      const ms = this.div("beam-report-tcell ms");
      ms.textContent = fmtMs(s.avgSolveMs);
      row.appendChild(name);
      row.appendChild(gap);
      row.appendChild(ms);
      wrap.appendChild(row);
    }
    const legend = this.div("beam-report-tlegend");
    legend.textContent = "* optimal reference - gaps are measured against it.";
    wrap.appendChild(legend);
    return wrap;
  }

  private exportJson(data: RunReportData): void {
    try {
      const payload = JSON.stringify(
        { context: data.context, summary: data.response },
        null,
        2,
      );
      const blob = new Blob([payload], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = this.doc.createElement("a");
      a.href = url;
      a.download = `beam-report-${data.response.run_id}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* clipboard/download unavailable (e.g. headless) - non-fatal */
    }
  }

  // --- tiny DOM helpers --------------------------------------------------- //

  private kpi(label: string, value: string, tone: string, hint: string): HTMLElement {
    const cell = this.div(`beam-report-kpi tone-${tone}`);
    const v = this.div("beam-report-kpi-value");
    v.textContent = value;
    const l = this.div("beam-report-kpi-label");
    l.textContent = label;
    const h = this.div("beam-report-kpi-hint");
    h.textContent = hint;
    cell.appendChild(v);
    cell.appendChild(l);
    cell.appendChild(h);
    return cell;
  }

  private sectionTitle(text: string): HTMLElement {
    const t = this.div("beam-report-section");
    t.textContent = text;
    return t;
  }

  private rows(items: Array<[string, string]>): HTMLElement {
    const wrap = this.div("beam-report-rows");
    for (const [k, v] of items) {
      const row = this.div("beam-report-row");
      const key = this.div("beam-report-key");
      key.textContent = k;
      const val = this.div("beam-report-val");
      val.textContent = v;
      row.appendChild(key);
      row.appendChild(val);
      wrap.appendChild(row);
    }
    return wrap;
  }

  private div(className: string): HTMLElement {
    const el = this.doc.createElement("div");
    el.className = className;
    return el;
  }

  private button(text: string, className: string): HTMLButtonElement {
    const b = this.doc.createElement("button");
    b.type = "button";
    b.className = className;
    b.textContent = text;
    return b;
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
