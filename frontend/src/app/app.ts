// BeamApp - the top-level frontend composition (pdd.md 14.1, 14.2, 14.3).
//
// Layout (single mode):
//   ┌──────────── top scoreboard ────────────┐
//   │ left controls │ center battlefield │ right dashboards │
//   └─────────────────────────────────────────┘
//
// Solver-race split mode (pdd.md 14.2 - the headline demo screen):
//   the center splits into two battlefields side by side, each with its own
//   scoreboard, both started on the SAME scenario_id + seed but pinned to two
//   different active solvers. The right dashboards follow the "A" (left) stream.
//
// The app owns: a BeamClient, the ControlPanel (left), one or two RunControllers
// (center), and the dashboards (right). It never redefines a wire model - it only
// composes the render/, panels/, charts/, and net/ modules over types.ts.

import { BeamClient } from "../net";
import { ControlPanel, RunReport, ScenarioEditor, type ControlSink } from "../panels";
import type { ControlMessage, RunSummaryResponse } from "../types";
import { ParetoChart } from "../charts";
import { RunController, type RunStatus } from "./runController";
import { parseUrlToken, shareUrl, type ShareToken } from "./shareLink";

/** The preset that the one-click "wow" button loads (pdd.md 4 / 15 Phase 3). */
export const WOW_PRESET = "swarm_24";

/** Default pairing for the solver race: a fast greedy vs a strong solver, so the
 *  divergence reads instantly on a projector. Both are common scaffold solver
 *  names; the app falls back to whatever the backend actually reports. */
export const DEFAULT_RACE_PAIR: [string, string] = ["greedy_nearest", "auction"];

/**
 * Choose the default active solver from a catalog: prefer "auction" (strong+fast,
 * pdd.md 9.2 #4), else the first available, else a hard fallback. Pure.
 */
export function pickPreferredSolver(names: readonly string[]): string {
  if (names.includes("auction")) return "auction";
  return names[0] ?? "auction";
}

/**
 * Choose two DISTINCT solver names for the race from a catalog (pdd.md 14.2).
 * Prefers the demo-effective default pair, falling back to whatever the backend
 * reports while guaranteeing the two entries differ when 2+ solvers exist. Pure.
 */
export function pickRacePair(names: readonly string[]): [string, string] {
  const has = (n: string) => names.includes(n);
  const [da, db] = DEFAULT_RACE_PAIR;
  const a = has(da) ? da : names[0] ?? da;
  let b = has(db) && db !== a ? db : names.find((n) => n !== a) ?? db;
  if (b === a) b = names.find((n) => n !== a) ?? db;
  return [a, b];
}

/** Promise that resolves after `ms` (used to wait for run-summary finalization). */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface BeamAppOptions {
  root: HTMLElement;
  client?: BeamClient;
  document?: Document;
}

type Mode = "single" | "race";

export class BeamApp {
  private readonly root: HTMLElement;
  private readonly doc: Document;
  private readonly client: BeamClient;

  // DOM regions.
  private elScoreboardBar!: HTMLElement;
  private elControls!: HTMLElement;
  private elStage!: HTMLElement;
  private elDashboards!: HTMLElement;
  private elGapCanvas!: HTMLCanvasElement;
  private elCostCanvas!: HTMLCanvasElement;
  private elParetoCanvas!: HTMLCanvasElement;
  private elStatus!: HTMLElement;
  private elModeToggle!: HTMLButtonElement;
  private elViewToggle!: HTMLButtonElement;
  private elWowBtn!: HTMLButtonElement;
  private elReportBtn!: HTMLButtonElement;
  private elShareBtn!: HTMLButtonElement;
  private view3d = false;
  private el3dContainers: HTMLElement[] = [];

  private paretoChart!: ParetoChart;
  private paretoId: string | null = null;
  private evolveId: string | null = null;

  // Stream A is always present; stream B exists only in race mode.
  private ctrlA: RunController | null = null;
  private ctrlB: RunController | null = null;

  private panel!: ControlPanel;
  private editor!: ScenarioEditor;
  private mode: Mode = "single";

  // Catalog from the backend (solver/weather names) - used to populate selects
  // and choose the race pair. Falls back to scaffold defaults when unreachable.
  private solverNames: string[] = [];
  private weatherNames: string[] = [];
  private referenceSolver = "cp_sat";

  // The scenario currently being run (shared across both race streams).
  private scenarioId: string | null = null;
  private lastSeed: number | undefined;
  private displaySeed: number | undefined;
  private scenarioLabel = "custom scenario";
  private lastActiveSolver = "";

  // After-action report (post-run summary overlay) + de-dup guard so it opens
  // once per finished run, not on every terminal status callback.
  private report!: RunReport;
  private reportedRunId: string | null = null;

  constructor(opts: BeamAppOptions) {
    this.root = opts.root;
    this.doc = opts.document ?? this.root.ownerDocument ?? globalThis.document;
    this.client = opts.client ?? new BeamClient();
  }

  /** Build the DOM skeleton, load the catalog, and mount the panel. */
  async init(): Promise<void> {
    this.buildLayout();
    this.report = new RunReport({
      root: this.root,
      document: this.doc,
      onReplay: () => void this.loadWowPreset(),
    });
    await this.loadCatalog();
    this.mountPanel();
    this.mountEditor();
    this.rebuildStage();
    this.paretoChart = new ParetoChart();

    const token = parseUrlToken();
    if (token) {
      await this.loadFromShareToken(token);
    } else {
      this.setStatus('ready - press “Load demo” to begin');
    }
  }

  // --- DOM skeleton ------------------------------------------------------- //

  private buildLayout(): void {
    this.root.classList.add("beam-app");
    this.root.replaceChildren();

    // Top: brand + scoreboard bar + mode toggle + wow button + status.
    const top = this.el("header", "beam-topbar");
    const brand = this.el("div", "beam-brand");
    brand.innerHTML = `<span class="beam-brand-mark">◢◣</span> BEAM <span class="beam-brand-sub">Battle Engagement &amp; Aerial Mitigation</span>`;
    top.appendChild(brand);

    this.elScoreboardBar = this.el("div", "beam-scoreboard-bar");
    top.appendChild(this.elScoreboardBar);

    const topActions = this.el("div", "beam-topactions");
    this.elWowBtn = this.doc.createElement("button");
    this.elWowBtn.className = "beam-wow";
    this.elWowBtn.type = "button";
    this.elWowBtn.textContent = "▶ Load demo (swarm_24)";
    this.elWowBtn.addEventListener("click", () => void this.loadWowPreset());
    topActions.appendChild(this.elWowBtn);

    this.elModeToggle = this.doc.createElement("button");
    this.elModeToggle.className = "beam-mode-toggle";
    this.elModeToggle.type = "button";
    this.elModeToggle.textContent = "Solver race: OFF";
    this.elModeToggle.addEventListener("click", () => void this.toggleMode());
    topActions.appendChild(this.elModeToggle);

    this.elViewToggle = this.doc.createElement("button");
    this.elViewToggle.className = "beam-view-toggle";
    this.elViewToggle.type = "button";
    this.elViewToggle.textContent = "3D: OFF";
    this.elViewToggle.addEventListener("click", () => this.toggleView());
    topActions.appendChild(this.elViewToggle);

    this.elReportBtn = this.doc.createElement("button");
    this.elReportBtn.className = "beam-report-toggle";
    this.elReportBtn.type = "button";
    this.elReportBtn.textContent = "▤ Report";
    this.elReportBtn.disabled = true;
    this.elReportBtn.addEventListener("click", () => this.report.reopen());
    topActions.appendChild(this.elReportBtn);

    this.elShareBtn = this.doc.createElement("button");
    this.elShareBtn.className = "beam-share";
    this.elShareBtn.type = "button";
    this.elShareBtn.textContent = "⧉ Share";
    this.elShareBtn.disabled = true;
    this.elShareBtn.addEventListener("click", () => void this.copyShareLink());
    topActions.appendChild(this.elShareBtn);

    top.appendChild(topActions);

    this.root.appendChild(top);

    // Status line.
    this.elStatus = this.el("div", "beam-status");
    this.root.appendChild(this.elStatus);

    // Body: left controls | center stage | right dashboards.
    const body = this.el("div", "beam-body");
    this.elControls = this.el("aside", "beam-controls-panel");
    this.elStage = this.el("main", "beam-stage");
    this.elDashboards = this.el("aside", "beam-dashboards");

    // Dashboards: three stacked chart cards.
    const gapCard = this.chartCard("Solver race - objective & gap", "gap");
    this.elGapCanvas = gapCard.canvas;
    const costCard = this.chartCard("Cost exchange - breakeven", "cost");
    this.elCostCanvas = costCard.canvas;
    this.elDashboards.appendChild(gapCard.card);
    this.elDashboards.appendChild(costCard.card);

    const paretoCard = this.chartCard("Pareto frontier - cost vs value saved", "pareto");
    this.elParetoCanvas = paretoCard.canvas;
    const paretoBtn = this.doc.createElement("button");
    paretoBtn.type = "button";
    paretoBtn.className = "beam-pareto-btn";
    paretoBtn.textContent = "Run Pareto Analysis";
    paretoBtn.addEventListener("click", () => void this.runParetoAnalysis());
    paretoCard.card.insertBefore(paretoBtn, paretoCard.canvas);
    this.elDashboards.appendChild(paretoCard.card);

    body.appendChild(this.elControls);
    body.appendChild(this.elStage);
    body.appendChild(this.elDashboards);
    this.root.appendChild(body);
  }

  private chartCard(
    title: string,
    kind: string,
  ): { card: HTMLElement; canvas: HTMLCanvasElement } {
    const card = this.el("section", `beam-card beam-card-${kind}`);
    const h = this.el("h2", "beam-card-title");
    h.textContent = title;
    const canvas = this.doc.createElement("canvas");
    canvas.className = "beam-chart";
    card.appendChild(h);
    card.appendChild(canvas);
    return { card, canvas };
  }

  // --- catalog ------------------------------------------------------------ //

  private async loadCatalog(): Promise<void> {
    try {
      const [solvers, weather] = await Promise.all([
        this.client.listSolvers(),
        this.client.listWeather(),
      ]);
      this.solverNames = solvers.solvers.map((s) => s.name);
      this.weatherNames = weather.profiles.map((w) => w.name);
      if (solvers.reference) this.referenceSolver = solvers.reference;
    } catch {
      // Backend not up yet (e.g. static preview); fall back to scaffold names so
      // the UI still renders and the demo button can retry on click.
      this.solverNames = [
        "greedy_nearest",
        "greedy_threat",
        "greedy_urgent",
        "auction",
        "ga",
        "cp_sat",
      ];
      this.weatherNames = ["clear", "haze", "rain", "fog", "dust"];
    }
  }

  // --- control panel ------------------------------------------------------ //

  private mountEditor(): void {
    this.editor = new ScenarioEditor({
      root: this.root,
      weatherNames: this.weatherNames,
      document: this.doc,
      onSave: async (overlay) => {
        this.setStatus("creating scenario from editor...");
        const created = await this.client.createScenario({ scenario: overlay });
        this.scenarioId = created.scenario_id;
        this.scenarioLabel = "custom scenario";
        this.displaySeed = (overlay.seed as number | undefined) ?? this.lastSeed;
        this.lastSeed = this.displaySeed;
        await this.startRuns();
        this.setStatus("running custom scenario from editor");
      },
    });
  }

  private mountPanel(): void {
    const sink: ControlSink = {
      start: (overrides) => void this.startFromControls(overrides),
      reset: (overrides) => void this.startFromControls(overrides),
      send: (msg) => this.broadcastControl(msg),
      openEditor: () => this.editor?.open(),
      startEvolve: () => void this.startEvolution(),
    };
    this.panel = new ControlPanel({
      root: this.elControls,
      sink,
      solverNames: this.solverNames,
      weatherNames: this.weatherNames,
      initial: { activeSolver: this.preferredSolver() },
      document: this.doc,
    });
  }

  /** Pick a sensible default active solver from the catalog (auction if present). */
  private preferredSolver(): string {
    return pickPreferredSolver(this.solverNames);
  }

  // --- stage (single vs split battlefields) ------------------------------- //

  /** (Re)build the center stage for the current mode, allocating controllers. */
  private rebuildStage(): void {
    if (this.elShareBtn) this.elShareBtn.disabled = true;
    this.el3dContainers = [];
    // Tear down existing controllers.
    this.ctrlA?.destroy();
    this.ctrlB?.destroy();
    this.ctrlA = null;
    this.ctrlB = null;
    this.elStage.replaceChildren();

    if (this.mode === "single") {
      const pane = this.makePane("");
      this.elStage.classList.remove("beam-stage-split");
      this.elStage.appendChild(pane.wrap);
      this.ctrlA = new RunController({
        client: this.client,
        views: {
          battlefield: pane.field,
          battlefield3dContainer: pane.field3d,
          scoreboard: pane.score,
          gapCanvas: this.elGapCanvas,
          costCanvas: this.elCostCanvas,
        },
        framePeriodS: 0.5,
        onStatus: (s, d) => this.reflectStatus(s, d),
      });
      void this.ctrlA.init().then(() => this.ctrlA?.startChartLoop());
    } else {
      this.elStage.classList.add("beam-stage-split");
      const [solverA, solverB] = this.racePair();
      const paneA = this.makePane(solverA);
      const paneB = this.makePane(solverB);
      this.elStage.appendChild(paneA.wrap);
      this.elStage.appendChild(paneB.wrap);

      // In race mode the right dashboards track stream A (left), so the gap chart
      // still shows the full solver race (all enabled solvers evaluated per epoch
      // by the same engine) and the breakeven follows the A run's ledger.
      this.ctrlA = new RunController({
        client: this.client,
        views: {
          battlefield: paneA.field,
          battlefield3dContainer: paneA.field3d,
          scoreboard: paneA.score,
          gapCanvas: this.elGapCanvas,
          costCanvas: this.elCostCanvas,
        },
        title: solverA,
        framePeriodS: 0.5,
        onStatus: (s, d) => this.reflectStatus(s, d),
      });
      this.ctrlB = new RunController({
        client: this.client,
        views: {
          battlefield: paneB.field,
          battlefield3dContainer: paneB.field3d,
          scoreboard: paneB.score,
        },
        title: solverB,
        framePeriodS: 0.5,
      });
      void Promise.all([this.ctrlA.init(), this.ctrlB.init()]).then(() => {
        this.ctrlA?.startChartLoop();
      });
    }
  }

  /** One battlefield pane: a titled wrapper with a field area + scoreboard. */
  private makePane(title: string): {
    wrap: HTMLElement;
    field: HTMLElement;
    field3d: HTMLElement;
    score: HTMLElement;
  } {
    const wrap = this.el("div", "beam-pane");
    const score = this.el("div", "beam-pane-score");
    const field = this.el("div", "beam-pane-field");
    const field3d = this.el("div", "beam-pane-field beam-pane-field-3d");
    field3d.style.display = "none";
    // Scoreboard above the battlefield (pdd.md 14.2: scoreboards under/over each).
    wrap.appendChild(score);
    wrap.appendChild(field);
    wrap.appendChild(field3d);
    this.el3dContainers.push(field3d);
    if (title) wrap.dataset.solver = title;
    return { wrap, field, field3d, score };
  }

  /** Two distinct solver names for the race, from the catalog when possible. */
  private racePair(): [string, string] {
    return pickRacePair(this.solverNames);
  }

  // --- run lifecycle ------------------------------------------------------ //

  /** Create/refresh the scenario from panel overrides, then start the run(s). */
  private async startFromControls(
    overrides: Record<string, unknown>,
  ): Promise<void> {
    try {
      this.setStatus("creating scenario…");
      const created = await this.client.createScenario({ scenario: overrides });
      this.scenarioId = created.scenario_id;
      this.scenarioLabel = "custom scenario";
      this.displaySeed = this.lastSeed;
      await this.startRuns();
    } catch (e) {
      this.setStatus(`failed to start: ${String(e)}`);
    }
  }

  /** Load a run from a share token decoded from the ?run= URL param. */
  private async loadFromShareToken(token: ShareToken): Promise<void> {
    try {
      if (this.solverNames.length === 0) await this.loadCatalog();
      this.setStatus("loading shared run…");
      const req =
        token.preset != null
          ? { preset: token.preset }
          : { scenario: token.scenario ?? {} };
      const created = await this.client.createScenario(req);
      this.scenarioId = created.scenario_id;
      this.scenarioLabel = token.preset ?? "shared scenario";
      this.lastSeed = token.seed;
      this.displaySeed = token.seed;
      this.lastActiveSolver = token.solver;
      this.panel.setSolver(token.solver);
      await this.startRuns();
      this.setStatus(`running shared run - solver: ${token.solver}`);
    } catch (e) {
      this.setStatus(`could not load shared run: ${String(e)}`);
    }
  }

  /** Copy a share URL for the current run to the clipboard. */
  private async copyShareLink(): Promise<void> {
    if (!this.scenarioId) return;
    const state = this.panel.getState();
    const token: ShareToken = {
      v: 1,
      solver: this.lastActiveSolver || state.activeSolver,
      seed: this.displaySeed,
    };
    if (this.scenarioLabel === WOW_PRESET) {
      token.preset = WOW_PRESET;
    } else {
      try {
        const res = await this.client.getScenario(this.scenarioId);
        token.scenario = res.scenario;
      } catch {
        this.setStatus("could not generate share link");
        return;
      }
    }
    try {
      await navigator.clipboard.writeText(shareUrl(token));
      this.setStatus("share link copied to clipboard");
    } catch {
      this.setStatus("clipboard unavailable - share URL in DevTools console");
      console.info("BEAM share URL:", shareUrl(token));
    }
  }

  private async runParetoAnalysis(): Promise<void> {
    if (!this.scenarioId) {
      this.setStatus("load a scenario first before running Pareto analysis");
      return;
    }
    this.setStatus("starting Pareto analysis (may take ~60s)...");
    try {
      const res = await this.client.startPareto({
        scenario_id: this.scenarioId,
        seed: this.lastSeed ?? 1337,
        n_points: 15,
        solver: "cp_sat",
      });
      this.paretoId = res.pareto_id;
      this.pollParetoResults();
    } catch (e) {
      this.setStatus(`Pareto analysis failed to start: ${String(e)}`);
    }
  }

  private pollParetoResults(): void {
    if (!this.paretoId) return;
    const id = this.paretoId;
    const poll = async () => {
      try {
        const res = await this.client.paretoResults(id);
        if (res.status === "done") {
          this.paretoChart.setPoints(res.points);
          this.paretoChart.render(this.elParetoCanvas);
          this.setStatus(`Pareto analysis complete - ${res.points.length} frontier points`);
        } else if (res.status === "error") {
          this.setStatus(`Pareto error: ${res.error ?? "unknown"}`);
        } else {
          setTimeout(() => void poll(), 2000);
        }
      } catch {
        // polling failed, stop
      }
    };
    void poll();
  }

  private async startEvolution(): Promise<void> {
    const state = this.panel.getState();
    this.setStatus(`evolving swarm (${state.swarmSize} drones) against ${state.activeSolver}...`);
    try {
      const res = await this.client.startEvolve({
        preset: this.scenarioLabel === WOW_PRESET ? WOW_PRESET : "swarm_24",
        defender_solver: state.activeSolver,
        generations: 20,
        population_size: 30,
        swarm_count: state.swarmSize,
        behavior: state.behavior,
      });
      this.evolveId = res.evolve_id;
      this.pollEvolution();
    } catch (e) {
      this.setStatus(`evolution failed to start: ${String(e)}`);
    }
  }

  private pollEvolution(): void {
    if (!this.evolveId) return;
    const id = this.evolveId;
    const poll = async () => {
      try {
        const res = await this.client.evolveStatus(id);
        if (res.status === "done") {
          this.setStatus(`evolution complete - best leaked value: ${res.best_fitness.toFixed(0)}`);
          this.editor?.open();
        } else if (res.status === "error") {
          this.setStatus(`evolution error: ${res.error ?? "unknown"}`);
        } else {
          this.setStatus(
            `evolving... gen ${res.generation}/${res.generations} best=${res.best_fitness.toFixed(0)}`,
          );
          setTimeout(() => void poll(), 3000);
        }
      } catch {
        // polling failed, stop
      }
    };
    void poll();
  }

  /** The one-click "wow" path: load the preset scenario and start immediately. */
  private async loadWowPreset(): Promise<void> {
    try {
      // Catalog may have failed on first paint (backend not up); retry now.
      if (this.solverNames.length === 0) await this.loadCatalog();
      this.setStatus(`loading preset “${WOW_PRESET}”…`);
      const created = await this.client.createScenario({ preset: WOW_PRESET });
      this.scenarioId = created.scenario_id;
      this.scenarioLabel = WOW_PRESET;
      this.lastSeed = undefined; // preset carries its own seed (1337)
      this.displaySeed = 1337; // shown in the report; not sent as an override
      await this.startRuns();
      this.setStatus(`running “${WOW_PRESET}” - seed 1337`);
    } catch (e) {
      this.setStatus(`could not load demo (is the backend on :8000?): ${String(e)}`);
    }
  }

  /** Start one run (single) or two same-seed runs (race) on the current scenario. */
  private async startRuns(): Promise<void> {
    if (!this.scenarioId) return;
    const state = this.panel.getState();
    const seed = this.lastSeed;
    // A fresh run invalidates any prior report-open guard.
    this.reportedRunId = null;
    this.lastActiveSolver =
      this.mode === "single" ? state.activeSolver : this.racePair()[0];

    if (this.mode === "single") {
      await this.ctrlA?.start({
        scenarioId: this.scenarioId,
        activeSolver: state.activeSolver,
        enabledSolvers: this.solverNames.length ? this.solverNames : undefined,
        seed,
      });
      this.panel.setRunning(true);
      this.elShareBtn.disabled = false;
    } else {
      const [solverA, solverB] = this.racePair();
      // Same scenario_id + same seed => identical seeded scenario for both
      // streams (pdd.md 14.2 / 9.4). Each stream is pinned to one active solver.
      await Promise.all([
        this.ctrlA?.start({
          scenarioId: this.scenarioId,
          activeSolver: solverA,
          enabledSolvers: this.solverNames.length ? this.solverNames : undefined,
          seed,
        }),
        this.ctrlB?.start({
          scenarioId: this.scenarioId,
          activeSolver: solverB,
          seed,
        }),
      ]);
      this.panel.setRunning(true);
      this.elShareBtn.disabled = false;
    }
  }

  /** Fan a live control message out to every active stream (pause/step/etc.). */
  private broadcastControl(msg: ControlMessage): void {
    const apply = (c: RunController | null) => {
      if (!c) return;
      c.control((s) => {
        switch (msg.action) {
          case "pause":
            s.pause();
            break;
          case "resume":
            s.resume();
            break;
          case "step":
            s.step(msg.epochs ?? 1);
            break;
          case "stop":
            s.stop();
            break;
          case "set_speed":
            if (msg.multiplier != null) s.setSpeed(msg.multiplier);
            break;
          case "set_solver":
            // In race mode each stream is pinned to its own solver, so a global
            // set_solver only applies to the single-view stream A.
            if (this.mode === "single" && msg.solver) s.setSolver(msg.solver);
            break;
        }
      });
    };
    apply(this.ctrlA);
    if (msg.action !== "set_solver") apply(this.ctrlB);
  }

  // --- mode toggle -------------------------------------------------------- //

  private async toggleMode(): Promise<void> {
    this.mode = this.mode === "single" ? "race" : "single";
    this.elModeToggle.textContent =
      this.mode === "race" ? "Solver race: ON" : "Solver race: OFF";
    this.elModeToggle.classList.toggle("active", this.mode === "race");
    this.panel.setRunning(false);
    this.rebuildStage();
    // If a scenario is already loaded, re-run it under the new mode for continuity.
    if (this.scenarioId) {
      this.setStatus(
        this.mode === "race"
          ? "solver race - same seed, two solvers side by side"
          : "single view",
      );
      await this.startRuns();
    }
  }

  private toggleView(): void {
    this.view3d = !this.view3d;
    this.elViewToggle.textContent = this.view3d ? "3D: ON" : "3D: OFF";
    this.elViewToggle.classList.toggle("active", this.view3d);

    // Swap visibility of 2D / 3D containers in all panes
    const panes2d = this.elStage.querySelectorAll<HTMLElement>(
      ".beam-pane-field:not(.beam-pane-field-3d)",
    );
    panes2d.forEach((el) => { el.style.display = this.view3d ? "none" : ""; });
    this.el3dContainers.forEach((el) => { el.style.display = this.view3d ? "" : "none"; });

    // Activate / pause the appropriate renderer
    this.ctrlA?.battlefield.setActive(!this.view3d);
    this.ctrlA?.battlefield3d?.setActive(this.view3d);
    this.ctrlB?.battlefield.setActive(!this.view3d);
    this.ctrlB?.battlefield3d?.setActive(this.view3d);
  }

  // --- status ------------------------------------------------------------- //

  private reflectStatus(s: RunStatus, detail?: string): void {
    if (s === "error") this.setStatus(`stream error: ${detail ?? ""}`);
    else if (s === "ended") {
      this.setStatus("run complete - opening after-action report");
      void this.showReport();
    }
  }

  // --- after-action report ------------------------------------------------ //

  /** Fetch the run summary and open the after-action report. Idempotent per run:
   *  the "ended" status can arrive more than once (socket close races), so we
   *  guard on the run id. The summary.json is written as the run finalizes, so we
   *  retry briefly if it isn't aggregated yet. */
  private async showReport(): Promise<void> {
    const runId = this.ctrlA?.currentRunId;
    if (!runId || this.reportedRunId === runId) return;
    this.reportedRunId = runId;
    try {
      const response = await this.fetchSummaryWithRetry(runId);
      const model = this.ctrlA?.scoreboard.model;
      this.report.show({
        response,
        context: {
          scenarioLabel: this.scenarioLabel,
          seed: this.displaySeed,
          activeSolver: this.lastActiveSolver || model?.activeSolver,
          referenceSolver: this.referenceSolver,
          protectedValueFrac: model?.protectedValueFrac,
          simSeconds: model?.simClock,
          epochs: model?.epoch,
        },
      });
      this.elReportBtn.disabled = false;
      this.setStatus("run complete - after-action report ready (▤ Report to reopen)");
    } catch (e) {
      this.reportedRunId = null; // allow a manual retry
      this.setStatus(`run complete (report unavailable: ${String(e)})`);
    }
  }

  private async fetchSummaryWithRetry(
    runId: string,
    attempts = 4,
  ): Promise<RunSummaryResponse> {
    let last: RunSummaryResponse | null = null;
    for (let i = 0; i < attempts; i++) {
      last = await this.client.runSummary(runId);
      if (last.summary) return last;
      await delay(150 * (i + 1));
    }
    // Return whatever we have; the overlay renders a "pending" state if needed.
    return last as RunSummaryResponse;
  }

  private setStatus(text: string): void {
    this.elStatus.textContent = text;
  }

  // --- tiny DOM helper ---------------------------------------------------- //

  private el<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    className: string,
  ): HTMLElementTagNameMap[K] {
    const node = this.doc.createElement(tag);
    if (className) node.className = className;
    return node;
  }
}
