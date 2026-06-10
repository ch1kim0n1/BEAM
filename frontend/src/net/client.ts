// Typed WebSocket + REST client for the BEAM API (pdd.md section 12).
//
// - REST control plane (12.1): scenario / run / batch / catalog endpoints.
// - WebSocket telemetry (12.2): one socket per run at /ws/run/{run_id}; every inbound
//   message is validated with zod against ServerMessageSchema before any handler sees
//   it, so the rest of the frontend only ever deals with well-typed payloads.
// - Client -> server control (12.3): typed control messages sent over the same socket
//   (and mirrored by the REST control endpoint).
//
// types.ts is the single source of truth for every wire shape; this module imports
// from it and never redefines a schema.

import {
  ControlMessage,
  SCHEMA_VERSION,
  ServerMessage,
  ServerMessageSchema,
  type BatchResultsResponse,
  type BatchStartRequest,
  type BatchStartResponse,
  type RunControlResponse,
  type RunStartRequest,
  type RunStartResponse,
  type RunSummaryResponse,
  type ScenarioCreateRequest,
  type ScenarioCreateResponse,
  type ScenarioGetResponse,
  type SolversListResponse,
  type WeatherListResponse,
  type ParetoStartRequest,
  type ParetoStartResponse,
  type ParetoResultsResponse,
  type EvolveRequest,
  type EvolveStartResponse,
  type EvolveProgressResponse,
} from "../types";

// --------------------------------------------------------------------------- //
// REST client                                                                 //
// --------------------------------------------------------------------------- //

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface BeamClientOptions {
  /** Base HTTP origin of the backend, e.g. "http://localhost:8000". Defaults to
   *  the page origin (frontend served behind the same host / proxied). */
  baseUrl?: string;
  /** Explicit WebSocket origin. Derived from baseUrl when omitted (http->ws). */
  wsUrl?: string;
  fetchImpl?: typeof fetch;
}

function defaultBaseUrl(): string {
  // Allow the build-time env var to override (e.g. VITE_API_URL=http://api.example.com).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const envUrl = (typeof import.meta !== "undefined" && (import.meta as any).env?.VITE_API_URL) as string | undefined;
  if (envUrl) return envUrl;
  if (typeof window !== "undefined" && window.location) {
    return window.location.origin;
  }
  return "http://localhost:8000";
}

function httpToWs(base: string): string {
  return base.replace(/^http/i, (m) => (m.toLowerCase() === "http" ? "ws" : m));
}

/** REST control-plane client. One instance per backend; cheap to construct. */
export class BeamRestClient {
  readonly baseUrl: string;
  readonly wsBase: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: BeamClientOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? defaultBaseUrl()).replace(/\/+$/, "");
    this.wsBase = (opts.wsUrl ?? httpToWs(this.baseUrl)).replace(/\/+$/, "");
    this.fetchImpl =
      opts.fetchImpl ??
      (typeof fetch !== "undefined"
        ? fetch.bind(globalThis)
        : (() => {
            throw new Error("no fetch implementation available");
          }));
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: unknown,
  ): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const res = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    const text = await res.text();
    const parsed: unknown = text ? JSON.parse(text) : null;
    if (!res.ok) {
      const detail =
        parsed && typeof parsed === "object" && "detail" in parsed
          ? (parsed as { detail: unknown }).detail
          : res.statusText;
      throw new ApiError(`${method} ${path} -> ${res.status}: ${String(detail)}`, res.status, parsed);
    }
    return parsed as T;
  }

  // --- Scenario (12.1) ----------------------------------------------------- //

  createScenario(req: ScenarioCreateRequest): Promise<ScenarioCreateResponse> {
    return this.request("POST", "/api/scenario", req);
  }

  getScenario(scenarioId: string): Promise<ScenarioGetResponse> {
    return this.request("GET", `/api/scenario/${encodeURIComponent(scenarioId)}`);
  }

  // --- Run (12.1) ---------------------------------------------------------- //

  startRun(req: RunStartRequest): Promise<RunStartResponse> {
    return this.request("POST", "/api/run", req);
  }

  /** Apply a control action over REST (mirror of the WS control path, 12.3). */
  controlRun(runId: string, msg: ControlMessage): Promise<RunControlResponse> {
    return this.request("POST", `/api/run/${encodeURIComponent(runId)}/control`, {
      schema_version: SCHEMA_VERSION,
      ...msg,
    });
  }

  runSummary(runId: string): Promise<RunSummaryResponse> {
    return this.request("GET", `/api/run/${encodeURIComponent(runId)}/summary`);
  }

  // --- Batch (12.1) -------------------------------------------------------- //

  startBatch(req: BatchStartRequest): Promise<BatchStartResponse> {
    return this.request("POST", "/api/batch", req);
  }

  batchResults(batchId: string): Promise<BatchResultsResponse> {
    return this.request("GET", `/api/batch/${encodeURIComponent(batchId)}/results`);
  }

  startPareto(req: ParetoStartRequest): Promise<ParetoStartResponse> {
    return this.request("POST", "/api/batch/pareto", req);
  }

  paretoResults(paretoId: string): Promise<ParetoResultsResponse> {
    return this.request("GET", `/api/batch/pareto/${encodeURIComponent(paretoId)}/results`);
  }

  startEvolve(req: EvolveRequest): Promise<EvolveStartResponse> {
    return this.request("POST", "/api/evolve", req);
  }

  evolveStatus(evolveId: string): Promise<EvolveProgressResponse> {
    return this.request("GET", `/api/evolve/${encodeURIComponent(evolveId)}`);
  }

  // --- Catalog (12.1) ------------------------------------------------------ //

  listSolvers(): Promise<SolversListResponse> {
    return this.request("GET", "/api/solvers");
  }

  listWeather(): Promise<WeatherListResponse> {
    return this.request("GET", "/api/weather");
  }

  /** Full URL of the telemetry socket for a run. */
  telemetryUrl(runId: string): string {
    return `${this.wsBase}/ws/run/${encodeURIComponent(runId)}`;
  }
}

// --------------------------------------------------------------------------- //
// WebSocket telemetry client                                                  //
// --------------------------------------------------------------------------- //

export interface TelemetryHandlers {
  /** A validated server message of any kind. Fires for every parsed message. */
  onMessage?: (msg: ServerMessage) => void;
  onOpen?: (ev: Event) => void;
  onClose?: (ev: CloseEvent) => void;
  /** Transport error, or a message that failed zod validation (with the raw value). */
  onError?: (err: Error, raw?: unknown) => void;
}

export interface TelemetryClientOptions extends TelemetryHandlers {
  /** Reconnect with exponential backoff on unexpected close. Default true. */
  autoReconnect?: boolean;
  /** Initial backoff in ms (doubles up to maxBackoffMs). Default 500. */
  backoffMs?: number;
  maxBackoffMs?: number;
  /** Injectable WebSocket ctor (for tests / non-browser hosts). */
  WebSocketImpl?: typeof WebSocket;
}

/**
 * Single-run telemetry socket. Validates every inbound frame/epoch/ack/error/end
 * with zod before dispatching, and sends typed ControlMessages back. Reconnects with
 * exponential backoff unless explicitly closed via `close()` or after an `end` message.
 */
export class TelemetryClient {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly opts: TelemetryClientOptions;
  private readonly WS: typeof WebSocket;
  private backoff: number;
  private readonly maxBackoff: number;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByCaller = false;
  private ended = false;

  constructor(url: string, opts: TelemetryClientOptions = {}) {
    this.url = url;
    this.opts = opts;
    this.backoff = opts.backoffMs ?? 500;
    this.maxBackoff = opts.maxBackoffMs ?? 10_000;
    const impl = opts.WebSocketImpl ?? (typeof WebSocket !== "undefined" ? WebSocket : undefined);
    if (!impl) throw new Error("no WebSocket implementation available");
    this.WS = impl;
  }

  /** Open the socket. Safe to call once; use `close()`/`connect()` to recycle. */
  connect(): void {
    this.closedByCaller = false;
    this.ended = false;
    this.open();
  }

  private open(): void {
    const ws = new this.WS(this.url);
    this.ws = ws;

    ws.onopen = (ev) => {
      this.backoff = this.opts.backoffMs ?? 500; // reset backoff on success
      this.opts.onOpen?.(ev);
    };

    ws.onmessage = (ev: MessageEvent) => {
      let raw: unknown;
      try {
        raw = typeof ev.data === "string" ? JSON.parse(ev.data) : ev.data;
      } catch (e) {
        this.opts.onError?.(new Error(`malformed JSON: ${String(e)}`), ev.data);
        return;
      }
      const parsed = ServerMessageSchema.safeParse(raw);
      if (!parsed.success) {
        this.opts.onError?.(
          new Error(`telemetry validation failed: ${parsed.error.message}`),
          raw,
        );
        return;
      }
      const msg = parsed.data;
      if (msg.type === "end") this.ended = true;
      this.opts.onMessage?.(msg);
    };

    ws.onerror = (ev) => {
      this.opts.onError?.(new Error("websocket error"), ev);
    };

    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      this.opts.onClose?.(ev);
      const shouldReconnect =
        (this.opts.autoReconnect ?? true) && !this.closedByCaller && !this.ended;
      if (shouldReconnect) this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    const delay = this.backoff;
    this.backoff = Math.min(this.backoff * 2, this.maxBackoff);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closedByCaller && !this.ended) this.open();
    }, delay);
  }

  /** Send a typed control message (12.3). schema_version is stamped automatically. */
  send(msg: ControlMessage): void {
    if (!this.ws || this.ws.readyState !== this.WS.OPEN) {
      throw new Error("telemetry socket is not open");
    }
    const payload = { schema_version: SCHEMA_VERSION, ...msg };
    this.ws.send(JSON.stringify(payload));
  }

  // Convenience control wrappers (12.3).
  pause(): void {
    this.send({ action: "pause" });
  }
  resume(): void {
    this.send({ action: "resume" });
  }
  step(epochs = 1): void {
    this.send({ action: "step", epochs });
  }
  stop(): void {
    this.send({ action: "stop" });
  }
  setSolver(solver: string): void {
    this.send({ action: "set_solver", solver });
  }
  setSpeed(multiplier: number): void {
    this.send({ action: "set_speed", multiplier });
  }

  get readyState(): number {
    return this.ws?.readyState ?? this.WS.CLOSED;
  }

  /** Close the socket and suppress reconnection. */
  close(code?: number, reason?: string): void {
    this.closedByCaller = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close(code, reason);
    this.ws = null;
  }
}

// --------------------------------------------------------------------------- //
// Facade                                                                      //
// --------------------------------------------------------------------------- //

/** REST client + factory for per-run telemetry sockets. The one object the rest of
 *  the app constructs to talk to the backend. */
export class BeamClient extends BeamRestClient {
  /** Open a validated telemetry socket for a run. Call `.connect()` on the result. */
  openTelemetry(runId: string, handlers: TelemetryClientOptions = {}): TelemetryClient {
    return new TelemetryClient(this.telemetryUrl(runId), handlers);
  }
}

export type {
  ServerMessage,
  ControlMessage,
} from "../types";
