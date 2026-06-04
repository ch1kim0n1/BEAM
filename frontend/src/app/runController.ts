// RunController — drives ONE telemetry stream into one set of views.
//
// A controller owns: a battlefield renderer, a scoreboard, and (optionally) the
// gap + breakeven dashboards. It is the glue between the validated telemetry
// socket (net/) and the view modules (render/, panels/, charts/). The split-view
// (pdd.md 14.2) mounts two RunControllers side by side over the *same seed*, each
// pinned to a different active solver, so a viewer watches one policy leak drones
// while the other holds the line.
//
// Nothing here redefines a wire model: every payload is a validated ServerMessage
// from the shared types.ts contract, handed in by the TelemetryClient.

import { BeamClient, TelemetryClient } from "../net";
import type { ServerMessage } from "../types";
import { Battlefield } from "../render";
import { Scoreboard } from "../panels";
import { GapChart, BreakevenChart } from "../charts";

export interface RunControllerViews {
  /** Element the Pixi battlefield canvas attaches to. */
  battlefield: HTMLElement;
  /** Element the scoreboard mounts into. */
  scoreboard: HTMLElement;
  /** Optional gap-vs-compute chart canvas (single-view right dashboard). */
  gapCanvas?: HTMLCanvasElement;
  /** Optional cost breakeven chart canvas. */
  costCanvas?: HTMLCanvasElement;
}

export interface RunControllerOptions {
  client: BeamClient;
  views: RunControllerViews;
  /** Label shown above this stream's scoreboard (e.g. the solver name in split). */
  title?: string;
  /** Telemetry frame cadence hint (s) for render interpolation. */
  framePeriodS?: number;
  /** Fired on socket lifecycle / errors so the host can surface status. */
  onStatus?: (status: RunStatus, detail?: string) => void;
}

export type RunStatus =
  | "idle"
  | "starting"
  | "streaming"
  | "ended"
  | "error"
  | "stopped";

/**
 * One self-contained run pipeline: REST start -> WS telemetry -> views.
 * Reusable: call `start()` again (it tears the previous run down first).
 */
export class RunController {
  readonly battlefield: Battlefield;
  readonly scoreboard: Scoreboard;
  readonly gap: GapChart | null;
  readonly cost: BreakevenChart | null;

  private readonly client: BeamClient;
  private readonly views: RunControllerViews;
  private readonly onStatus?: RunControllerOptions["onStatus"];

  private socket: TelemetryClient | null = null;
  private runId: string | null = null;
  private status: RunStatus = "idle";
  private chartDirty = false;
  private chartRaf: number | null = null;
  private initialized = false;

  constructor(opts: RunControllerOptions) {
    this.client = opts.client;
    this.views = opts.views;
    this.onStatus = opts.onStatus;
    this.battlefield = new Battlefield({
      parent: opts.views.battlefield,
      framePeriodS: opts.framePeriodS,
    });
    this.scoreboard = new Scoreboard({
      root: opts.views.scoreboard,
      title: opts.title,
    });
    this.gap = opts.views.gapCanvas ? new GapChart() : null;
    this.cost = opts.views.costCanvas ? new BreakevenChart() : null;
  }

  /** Initialize the Pixi renderer (idempotent). Must complete before frames. */
  async init(): Promise<void> {
    if (this.initialized) return;
    await this.battlefield.init();
    this.initialized = true;
    this.renderCharts(true);
  }

  get currentRunId(): string | null {
    return this.runId;
  }
  get currentStatus(): RunStatus {
    return this.status;
  }
  /** The live socket, for the control panel to push pause/step/solver/speed. */
  get telemetry(): TelemetryClient | null {
    return this.socket;
  }

  /**
   * Start a run from an existing scenario_id, pinned to `activeSolver` and
   * evaluating `enabledSolvers` each epoch (the solver race). Tears down any
   * prior run first. Returns once telemetry is connecting.
   */
  async start(args: {
    scenarioId: string;
    activeSolver: string;
    enabledSolvers?: string[];
    seed?: number;
  }): Promise<void> {
    await this.init();
    this.teardownSocket();
    this.resetViews();
    this.setStatus("starting");

    let runId: string;
    try {
      const res = await this.client.startRun({
        scenario_id: args.scenarioId,
        solver: args.activeSolver,
        seed: args.seed ?? null,
        enabled_solvers: args.enabledSolvers ?? null,
      });
      runId = res.run_id;
    } catch (e) {
      this.setStatus("error", String(e));
      throw e;
    }
    this.runId = runId;

    const socket = this.client.openTelemetry(runId, {
      onMessage: (msg) => this.handleMessage(msg),
      onOpen: () => this.setStatus("streaming"),
      onError: (err) => this.setStatus("error", err.message),
      onClose: () => {
        if (this.status === "streaming") this.setStatus("ended");
      },
    });
    this.socket = socket;
    socket.connect();
  }

  /** Send a control message over this run's socket (no-op if not open). */
  control(send: (s: TelemetryClient) => void): void {
    if (this.socket) {
      try {
        send(this.socket);
      } catch {
        /* socket not open yet; ignore — caller may retry on next user action */
      }
    }
  }

  /** Stop the run and close the socket. */
  stop(): void {
    if (this.socket) {
      try {
        this.socket.stop();
      } catch {
        /* already closed */
      }
    }
    this.teardownSocket();
    this.setStatus("stopped");
  }

  /** Tear everything down (Pixi included). The controller is unusable after. */
  destroy(): void {
    this.teardownSocket();
    if (this.chartRaf != null) cancelAnimationFrame(this.chartRaf);
    this.chartRaf = null;
    this.battlefield.destroy();
  }

  // --- telemetry dispatch ------------------------------------------------- //

  private handleMessage(msg: ServerMessage): void {
    switch (msg.type) {
      case "frame":
        this.battlefield.pushFrame(msg);
        this.scoreboard.applyFrame(msg);
        break;
      case "epoch":
        this.scoreboard.applyEpoch(msg);
        this.gap?.push(msg);
        this.cost?.push(msg);
        this.chartDirty = true;
        break;
      case "end":
        this.setStatus("ended", msg.status);
        break;
      case "error":
        this.setStatus("error", JSON.stringify(msg.detail));
        break;
      case "ack":
        // Transport ack — no view change; host reads it via onStatus if needed.
        break;
    }
  }

  // --- charts (throttled to animation frames) ----------------------------- //

  private renderCharts(force = false): void {
    if (!force && !this.chartDirty) return;
    this.chartDirty = false;
    if (this.gap && this.views.gapCanvas) this.gap.render(this.views.gapCanvas);
    if (this.cost && this.views.costCanvas) this.cost.render(this.views.costCanvas);
  }

  /** Begin a render-frame loop that flushes dirty charts. Call once after init. */
  startChartLoop(): void {
    if (this.chartRaf != null) return;
    const tick = () => {
      this.renderCharts();
      this.chartRaf = requestAnimationFrame(tick);
    };
    this.chartRaf = requestAnimationFrame(tick);
  }

  // --- internals ---------------------------------------------------------- //

  private resetViews(): void {
    this.scoreboard.reset();
    this.gap?.reset();
    this.cost?.reset();
    this.renderCharts(true);
  }

  private teardownSocket(): void {
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }

  private setStatus(status: RunStatus, detail?: string): void {
    this.status = status;
    this.onStatus?.(status, detail);
  }
}
