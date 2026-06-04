// Tests for the battlefield renderer. The GPU-backed Pixi Application is mocked so
// these run headless under vitest/node: we exercise the pure interpolation math plus
// the sprite-pooling contract (pdd.md 14.4 — never recreate display objects per
// frame; reuse freed slots) and the leak-flash trigger.

import { beforeEach, describe, expect, it, vi } from "vitest";

// --- Minimal headless Pixi stand-in ---------------------------------------- //
// Records construction counts so we can assert objects are pooled, not recreated.

let graphicsCreated = 0;
let containerCreated = 0;

vi.mock("pixi.js", () => {
  class FakeGraphics {
    visible = true;
    rotation = 0;
    position = { set: vi.fn() };
    children: unknown[] = [];
    constructor() {
      graphicsCreated++;
    }
    addChild(...c: unknown[]) {
      this.children.push(...c);
      return this;
    }
    clear() {
      return this;
    }
    moveTo() {
      return this;
    }
    lineTo() {
      return this;
    }
    closePath() {
      return this;
    }
    circle() {
      return this;
    }
    rect() {
      return this;
    }
    arc() {
      return this;
    }
    fill() {
      return this;
    }
    stroke() {
      return this;
    }
  }
  class FakeContainer {
    visible = true;
    position = { set: vi.fn() };
    children: unknown[] = [];
    constructor() {
      containerCreated++;
    }
    addChild(...c: unknown[]) {
      this.children.push(...c);
      return this;
    }
  }
  class FakeTicker {
    add = vi.fn();
    remove = vi.fn();
  }
  class FakeApplication {
    stage = new FakeContainer();
    ticker = new FakeTicker();
    canvas = {} as HTMLCanvasElement;
    renderer = { resize: vi.fn() };
    async init() {
      /* no GPU */
    }
    destroy() {
      /* noop */
    }
  }
  return {
    Application: FakeApplication,
    Container: FakeContainer,
    Graphics: FakeGraphics,
  };
});

// Imported after the mock is registered.
import type { FrameMessage } from "../types";
import { Battlefield, lerpAngle } from "./battlefield";

function frame(over: Partial<FrameMessage> = {}): FrameMessage {
  return {
    type: "frame",
    schema_version: "1.0",
    t: 0,
    drones: [],
    turrets: [],
    beams: [],
    leaks: 0,
    kills: 0,
    ...over,
  };
}

function drone(id: string, x: number, y: number, over: Partial<FrameMessage["drones"][number]> = {}) {
  return {
    id,
    x,
    y,
    v: [0, 0] as [number, number],
    value: 2000,
    hp_frac: 1,
    state: "alive" as const,
    tti: 5,
    ...over,
  };
}

function makeParent(): HTMLElement {
  return {
    clientWidth: 800,
    clientHeight: 600,
    appendChild: vi.fn(),
  } as unknown as HTMLElement;
}

async function newField(): Promise<Battlefield> {
  const bf = new Battlefield({ parent: makeParent(), worldExtent: 5000 });
  await bf.init();
  return bf;
}

// Drive the private interpolated render once (the ticker is mocked, so we invoke the
// same code path the ticker would, via a tiny escape hatch).
function tick(bf: Battlefield): void {
  // @ts-expect-error -- exercise the private render method headlessly.
  bf.renderInterpolated();
}

beforeEach(() => {
  graphicsCreated = 0;
  containerCreated = 0;
  vi.restoreAllMocks();
  // ResizeObserver is referenced in init(); provide a noop.
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
    observe() {}
    disconnect() {}
  };
  (globalThis as { performance?: { now(): number } }).performance = {
    now: () => mockNow,
  };
});

let mockNow = 0;

describe("lerpAngle", () => {
  it("interpolates linearly within the short arc", () => {
    expect(lerpAngle(0, Math.PI / 2, 0.5)).toBeCloseTo(Math.PI / 4, 6);
  });

  it("takes the short way across the +/-pi wrap", () => {
    // From 170deg to -170deg should cross +180, a +20deg move, not -340deg.
    const a = (170 * Math.PI) / 180;
    const b = (-170 * Math.PI) / 180;
    const mid = lerpAngle(a, b, 0.5);
    // Halfway is at +180deg (== -180deg).
    expect(Math.abs(Math.abs(mid) - Math.PI)).toBeLessThan(1e-6);
  });

  it("returns endpoints at t=0 and t=1", () => {
    expect(lerpAngle(0.3, 1.1, 0)).toBeCloseTo(0.3, 6);
    expect(lerpAngle(0.3, 1.1, 1)).toBeCloseTo(1.1, 6);
  });
});

describe("Battlefield sprite pooling (14.4)", () => {
  it("does not recreate drone display objects across frames", async () => {
    const bf = await newField();
    bf.pushFrame(frame({ drones: [drone("d1", 100, 100), drone("d2", -200, 50)] }));
    tick(bf);
    const afterFirst = graphicsCreated + containerCreated;
    expect(bf.pooledDroneCount).toBe(2);

    // Same drones move; no new objects should be constructed.
    mockNow += 500;
    bf.pushFrame(frame({ drones: [drone("d1", 110, 90), drone("d2", -190, 60)] }));
    tick(bf);
    expect(graphicsCreated + containerCreated).toBe(afterFirst);
    expect(bf.pooledDroneCount).toBe(2);
  });

  it("reuses freed sprites when drones disappear then reappear", async () => {
    const bf = await newField();
    bf.pushFrame(frame({ drones: [drone("d1", 100, 100), drone("d2", -50, 50)] }));
    tick(bf);
    const baseline = graphicsCreated;
    expect(bf.pooledDroneCount).toBe(2);

    // d2 leaks (removed from live set) -> its sprite is freed, not destroyed.
    mockNow += 500;
    bf.pushFrame(frame({ drones: [drone("d1", 110, 100)] }));
    tick(bf);
    expect(bf.pooledDroneCount).toBe(2); // pool size preserved (1 active + 1 free)

    // A new drone appears -> should reuse the freed sprite, allocating nothing new.
    mockNow += 500;
    bf.pushFrame(frame({ drones: [drone("d1", 120, 100), drone("d3", 300, 0)] }));
    tick(bf);
    expect(graphicsCreated).toBe(baseline);
    expect(bf.pooledDroneCount).toBe(2);
  });

  it("does not draw dead or leaked drones as live markers", async () => {
    const bf = await newField();
    bf.pushFrame(
      frame({
        drones: [
          drone("d1", 100, 100, { state: "alive" }),
          drone("d2", 50, 50, { state: "dead" }),
          drone("d3", -50, 50, { state: "leaked" }),
        ],
      }),
    );
    tick(bf);
    expect(bf.pooledDroneCount).toBe(1);
  });

  it("pools turrets once and keeps them across frames", async () => {
    const bf = await newField();
    const t = (id: string, aim: number) => ({
      id,
      x: 0,
      y: 0,
      aim,
      target: null,
      state: "idle" as const,
      thermal_frac: 0,
    });
    bf.pushFrame(frame({ turrets: [t("t1", 0), t("t2", 1)] }));
    tick(bf);
    expect(bf.pooledTurretCount).toBe(2);
    const after = graphicsCreated + containerCreated;

    mockNow += 500;
    bf.pushFrame(frame({ turrets: [t("t1", 0.5), t("t2", 1.2)] }));
    tick(bf);
    expect(bf.pooledTurretCount).toBe(2);
    expect(graphicsCreated + containerCreated).toBe(after);
  });
});

describe("Battlefield leak flash", () => {
  it("triggers only when cumulative leak count rises", async () => {
    const bf = await newField();
    bf.pushFrame(frame({ leaks: 0 }));
    // @ts-expect-error -- read private for assertion.
    const t0 = bf.lastLeakMs;
    mockNow += 500;
    bf.pushFrame(frame({ leaks: 2 }));
    // @ts-expect-error
    const t1 = bf.lastLeakMs;
    expect(t1).toBeGreaterThan(t0);

    mockNow += 500;
    bf.pushFrame(frame({ leaks: 2 })); // unchanged -> no new flash
    // @ts-expect-error
    const t2 = bf.lastLeakMs;
    expect(t2).toBe(t1);
  });
});

describe("Battlefield prewarm + destroy", () => {
  it("pre-allocates the requested drone pool and tears down cleanly", async () => {
    const bf = new Battlefield({ parent: makeParent(), prewarmDrones: 4 });
    await bf.init();
    expect(bf.pooledDroneCount).toBe(4); // all free, none active yet
    bf.destroy();
    bf.destroy(); // idempotent
  });
});
