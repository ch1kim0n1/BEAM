// Tests for the net layer: zod validation of inbound telemetry, typed control
// send/encode, REST request encoding, and reconnect suppression on `end`.

import { describe, expect, it, vi } from "vitest";
import {
  BeamFrameSchema,
  ServerMessageSchema,
  SCHEMA_VERSION,
} from "../types";
import { BeamRestClient, TelemetryClient } from "./client";

// --- types.ts schema validation ------------------------------------------- //

describe("ServerMessageSchema", () => {
  it("parses a frame message and aliases beam 'from'", () => {
    const raw = {
      type: "frame",
      schema_version: "1.0",
      t: 12.34,
      drones: [
        {
          id: "d12",
          x: 120.5,
          y: -30.2,
          v: [-18, 4],
          value: 2000,
          hp_frac: 0.4,
          state: "alive",
          tti: 3.1,
        },
      ],
      turrets: [
        { id: "t1", x: 0, y: 0, aim: 1.92, target: "d12", state: "firing", thermal_frac: 0.62 },
      ],
      beams: [{ from: "t1", to: "d12", power_frac: 0.9 }],
      leaks: 0,
      kills: 7,
    };
    const msg = ServerMessageSchema.parse(raw);
    expect(msg.type).toBe("frame");
    if (msg.type === "frame") {
      expect(msg.beams[0].fromTurret).toBe("t1");
      expect(msg.beams[0].from).toBe("t1");
      expect(msg.drones[0].v).toEqual([-18, 4]);
    }
  });

  it("parses an epoch message with optional solver fields", () => {
    const raw = {
      type: "epoch",
      schema_version: "1.0",
      t: 12.0,
      epoch: 24,
      solvers: [
        { name: "cp_sat", objective: 18400, solve_ms: 210, is_optimal: true, bound: 18400 },
        { name: "auction", objective: 17600, solve_ms: 3, gap: 0.043 },
      ],
      active_solver: "auction",
      ledger: { cumulative_cost: 410000, value_destroyed: 260000, net: -150000 },
    };
    const msg = ServerMessageSchema.parse(raw);
    expect(msg.type).toBe("epoch");
    if (msg.type === "epoch") {
      expect(msg.solvers[0].is_optimal).toBe(true);
      expect(msg.solvers[1].gap).toBeCloseTo(0.043);
      expect(msg.solvers[0].gap).toBeNull(); // default applied
    }
  });

  it("parses ack / error / end control-plane messages", () => {
    expect(
      ServerMessageSchema.parse({
        type: "ack",
        action: "set_speed",
        status: "running",
        active_solver: "ga",
        speed: 2.0,
      }).type,
    ).toBe("ack");
    expect(
      ServerMessageSchema.parse({ type: "error", detail: "set_solver requires 'solver'" }).type,
    ).toBe("error");
    expect(
      ServerMessageSchema.parse({ type: "error", detail: [{ loc: ["action"], msg: "bad" }] }).type,
    ).toBe("error");
    expect(ServerMessageSchema.parse({ type: "end", run_id: "r1", status: "done" }).type).toBe("end");
  });

  it("rejects unknown message types and malformed frames", () => {
    expect(ServerMessageSchema.safeParse({ type: "nope" }).success).toBe(false);
    expect(
      ServerMessageSchema.safeParse({ type: "frame", t: "not-a-number" }).success,
    ).toBe(false);
  });

  it("rejects a beam missing the 'from' key", () => {
    expect(BeamFrameSchema.safeParse({ to: "d1", power_frac: 0.5 }).success).toBe(false);
  });
});

// --- REST client ----------------------------------------------------------- //

describe("BeamRestClient", () => {
  it("derives ws base from http base and builds telemetry url", () => {
    const c = new BeamRestClient({ baseUrl: "http://localhost:8000/" });
    expect(c.baseUrl).toBe("http://localhost:8000");
    expect(c.wsBase).toBe("ws://localhost:8000");
    expect(c.telemetryUrl("run 1")).toBe("ws://localhost:8000/ws/run/run%201");
  });

  it("derives wss from https", () => {
    const c = new BeamRestClient({ baseUrl: "https://example.com" });
    expect(c.wsBase).toBe("wss://example.com");
  });

  it("POSTs run start with a JSON body", async () => {
    const fetchImpl: typeof fetch = vi.fn(async () =>
      new Response(JSON.stringify({ run_id: "r1", active_solver: "auction", enabled_solvers: ["auction"] }), {
        status: 200,
      }),
    ) as unknown as typeof fetch;
    const c = new BeamRestClient({ baseUrl: "http://h", fetchImpl });
    const res = await c.startRun({ scenario_id: "s1", solver: "auction" });
    expect(res.run_id).toBe("r1");
    const init = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ scenario_id: "s1", solver: "auction" });
  });

  it("stamps schema_version on REST control", async () => {
    const fetchImpl: typeof fetch = vi.fn(async () =>
      new Response(
        JSON.stringify({ run_id: "r1", action: "pause", status: "paused", active_solver: "ga", speed: 1 }),
        { status: 200 },
      ),
    ) as unknown as typeof fetch;
    const c = new BeamRestClient({ baseUrl: "http://h", fetchImpl });
    await c.controlRun("r1", { action: "pause" });
    const init = (fetchImpl as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ schema_version: SCHEMA_VERSION, action: "pause" });
  });

  it("throws ApiError on non-2xx with detail", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ detail: "no such run" }), { status: 404 }),
    );
    const c = new BeamRestClient({ baseUrl: "http://h", fetchImpl: fetchImpl as unknown as typeof fetch });
    await expect(c.runSummary("missing")).rejects.toMatchObject({ status: 404 });
  });
});

// --- TelemetryClient (fake WebSocket) -------------------------------------- //

class FakeWebSocket {
  static OPEN = 1;
  static CLOSED = 3;
  readyState = 0;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(public url: string) {
    FakeWebSocket.last = this;
  }
  static last: FakeWebSocket | null = null;
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }
  emit(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
  emitRaw(data: string) {
    this.onmessage?.({ data } as MessageEvent);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new CloseEvent("close"));
  }
}

describe("TelemetryClient", () => {
  it("validates inbound messages and dispatches typed payloads", () => {
    const onMessage = vi.fn();
    const onError = vi.fn();
    const tc = new TelemetryClient("ws://h/ws/run/r1", {
      onMessage,
      onError,
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    tc.connect();
    const ws = FakeWebSocket.last!;
    ws.open();
    ws.emit({ type: "epoch", t: 1, epoch: 1, active_solver: "ga", ledger: { cumulative_cost: 0, value_destroyed: 0, net: 0 } });
    expect(onMessage).toHaveBeenCalledOnce();
    expect(onMessage.mock.calls[0][0].type).toBe("epoch");
    expect(onError).not.toHaveBeenCalled();
  });

  it("routes invalid telemetry to onError, not onMessage", () => {
    const onMessage = vi.fn();
    const onError = vi.fn();
    const tc = new TelemetryClient("ws://h", {
      onMessage,
      onError,
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    tc.connect();
    const ws = FakeWebSocket.last!;
    ws.open();
    ws.emit({ type: "frame", t: "bad" });
    ws.emitRaw("{not json");
    expect(onMessage).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledTimes(2);
  });

  it("encodes control sends with schema_version", () => {
    const tc = new TelemetryClient("ws://h", {
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
    });
    tc.connect();
    const ws = FakeWebSocket.last!;
    ws.open();
    tc.setSpeed(2);
    tc.step(3);
    expect(ws.sent).toHaveLength(2);
    expect(JSON.parse(ws.sent[0])).toEqual({
      schema_version: SCHEMA_VERSION,
      action: "set_speed",
      multiplier: 2,
    });
    expect(JSON.parse(ws.sent[1])).toEqual({
      schema_version: SCHEMA_VERSION,
      action: "step",
      epochs: 3,
    });
  });

  it("does not reconnect after an 'end' message", () => {
    vi.useFakeTimers();
    const tc = new TelemetryClient("ws://h", {
      WebSocketImpl: FakeWebSocket as unknown as typeof WebSocket,
      autoReconnect: true,
      backoffMs: 10,
    });
    tc.connect();
    const first = FakeWebSocket.last!;
    first.open();
    first.emit({ type: "end", run_id: "r1", status: "done" });
    first.close();
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.last).toBe(first); // no new socket created
    vi.useRealTimers();
  });
});
