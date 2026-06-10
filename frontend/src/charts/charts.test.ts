// Tests for the dashboard charts (pdd.md 14.1). We avoid a DOM/jsdom dependency
// by feeding the renderers a minimal fake <canvas> whose 2D context records the
// calls it receives - enough to assert the render path runs end-to-end and that
// the data-shaping (history windows, gap readout, crossover interpolation, batch
// parsing) is correct.

import { describe, expect, it } from "vitest";
import { GapChart } from "./gap";
import { BreakevenChart, interpCrossings, parseBatch } from "./breakeven";
import {
  fmtCompact,
  fmtMs,
  linearScale,
  niceStep,
  seriesColor,
  ticks,
} from "./theme";
import type {
  BatchResultsResponse,
  EpochMessage,
  EpochSolverEntry,
} from "../types";

// --- fake canvas ----------------------------------------------------------- //

function fakeCtx() {
  const calls: string[] = [];
  const rec =
    (name: string) =>
    (...args: unknown[]) => {
      calls.push(`${name}(${args.join(",")})`);
    };
  const ctx = {
    calls,
    canvas: {} as HTMLCanvasElement,
    setTransform: rec("setTransform"),
    fillRect: rec("fillRect"),
    strokeRect: rec("strokeRect"),
    beginPath: rec("beginPath"),
    moveTo: rec("moveTo"),
    lineTo: rec("lineTo"),
    closePath: rec("closePath"),
    stroke: rec("stroke"),
    fill: rec("fill"),
    arc: rec("arc"),
    fillText: rec("fillText"),
    setLineDash: rec("setLineDash"),
    measureText: (t: string) => ({ width: t.length * 6 }),
    save: rec("save"),
    restore: rec("restore"),
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 1,
    font: "",
    textAlign: "left",
    textBaseline: "alphabetic",
    globalAlpha: 1,
  };
  return ctx;
}

function fakeCanvas(): { canvas: HTMLCanvasElement; ctx: ReturnType<typeof fakeCtx> } {
  const ctx = fakeCtx();
  const canvas = {
    clientWidth: 320,
    clientHeight: 200,
    width: 320,
    height: 200,
    getContext: () => ctx,
  } as unknown as HTMLCanvasElement;
  return { canvas, ctx };
}

// --- builders -------------------------------------------------------------- //

function solver(
  name: string,
  objective: number,
  solve_ms: number,
  extra: Partial<EpochSolverEntry> = {},
): EpochSolverEntry {
  return {
    name,
    objective,
    solve_ms,
    gap: null,
    is_optimal: null,
    bound: null,
    gap_is_bound_based: false,
    ...extra,
  };
}

function epoch(
  e: number,
  t: number,
  solvers: EpochSolverEntry[],
  active: string,
  ledger = { cumulative_cost: 0, value_destroyed: 0, net: 0 },
): EpochMessage {
  return {
    type: "frame" as never, // overwritten below
    schema_version: "1.0",
    t,
    epoch: e,
    solvers,
    active_solver: active,
    ledger,
  } as unknown as EpochMessage;
}

// --- theme helpers --------------------------------------------------------- //

describe("theme helpers", () => {
  it("niceStep produces 1/2/5 progression", () => {
    expect(niceStep(100, 5)).toBe(20);
    expect(niceStep(10, 5)).toBe(2);
    expect(niceStep(1, 5)).toBeCloseTo(0.2);
    expect(niceStep(0, 5)).toBe(1); // degenerate guard
  });

  it("ticks span the range on a nice step", () => {
    const t = ticks(0, 100, 5);
    expect(t[0]).toBe(0);
    expect(t[t.length - 1]).toBeGreaterThanOrEqual(100);
    expect(t.every((v, i) => i === 0 || v > t[i - 1])).toBe(true);
  });

  it("ticks handles a flat domain", () => {
    expect(ticks(5, 5)).toEqual([5]);
  });

  it("linearScale maps endpoints and guards zero span", () => {
    const s = linearScale(0, 10, 0, 100);
    expect(s.apply(0)).toBe(0);
    expect(s.apply(10)).toBe(100);
    expect(s.apply(5)).toBe(50);
    const flat = linearScale(3, 3, 0, 100);
    expect(Number.isFinite(flat.apply(3))).toBe(true);
  });

  it("fmtCompact and fmtMs", () => {
    expect(fmtCompact(0)).toBe("0");
    expect(fmtCompact(1500)).toBe("1.5k");
    expect(fmtCompact(2_000_000)).toBe("2.0M");
    expect(fmtCompact(3_000_000_000)).toBe("3.0B");
    expect(fmtMs(0.5)).toBe("0.5ms");
    expect(fmtMs(1500)).toBe("1.5s");
  });

  it("seriesColor is deterministic and order-independent", () => {
    expect(seriesColor("auction")).toBe(seriesColor("auction"));
    expect(seriesColor("cp_sat")).not.toBe("");
  });
});

// --- gap chart ------------------------------------------------------------- //

describe("GapChart", () => {
  it("tracks solver insertion order and windows history", () => {
    const c = new GapChart({ maxEpochs: 3 });
    for (let i = 0; i < 5; i++) {
      c.push(
        epoch(
          i,
          i * 0.5,
          [solver("greedy_nearest", 100 + i, 1), solver("cp_sat", 120 + i, 50)],
          "greedy_nearest",
        ),
      );
    }
    expect(c.solvers).toEqual(["greedy_nearest", "cp_sat"]);
    // window keeps only the last 3 epochs internally; render still works.
    const { canvas } = fakeCanvas();
    expect(() => c.render(canvas)).not.toThrow();
  });

  it("reports the active solver's gap, flagging bound-based", () => {
    const c = new GapChart();
    c.push(
      epoch(
        0,
        0,
        [
          solver("auction", 90, 5, { gap: 0.1 }),
          solver("cp_sat", 100, 50, { is_optimal: true }),
        ],
        "auction",
      ),
    );
    let g = c.latestGap();
    expect(g).not.toBeNull();
    expect(g!.value).toBeCloseTo(0.1);
    expect(g!.boundBased).toBe(false);
    expect(g!.solver).toBe("auction");

    c.push(
      epoch(
        1,
        0.5,
        [
          solver("auction", 90, 5, { gap: 0.2, gap_is_bound_based: true }),
          solver("cp_sat", 110, 250, { is_optimal: false, bound: 115 }),
        ],
        "auction",
      ),
    );
    g = c.latestGap();
    expect(g!.boundBased).toBe(true);
    expect(g!.value).toBeCloseTo(0.2);
  });

  it("renders empty state without data", () => {
    const c = new GapChart();
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    expect(ctx.calls.some((s) => s.startsWith("fillText"))).toBe(true);
  });

  it("renders gap band + lines + bars with data", () => {
    const c = new GapChart();
    for (let i = 0; i < 4; i++) {
      c.push(
        epoch(
          i,
          i * 0.5,
          [
            solver("greedy_nearest", 80 + i, 1),
            solver("auction", 90 + i, 6, { gap: 0.08 }),
            solver("cp_sat", 100 + i, 60, { is_optimal: true }),
          ],
          "auction",
        ),
      );
    }
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    // gap band uses closePath + fill; lines use stroke; bars use fillRect.
    expect(ctx.calls.some((s) => s.startsWith("closePath"))).toBe(true);
    expect(ctx.calls.some((s) => s.startsWith("stroke"))).toBe(true);
    expect(ctx.calls.some((s) => s.startsWith("fillRect"))).toBe(true);
  });

  it("reset clears state", () => {
    const c = new GapChart();
    c.push(epoch(0, 0, [solver("g", 1, 1)], "g"));
    c.reset();
    expect(c.solvers).toEqual([]);
    expect(c.latestGap()).toBeNull();
  });
});

// --- breakeven chart ------------------------------------------------------- //

describe("BreakevenChart live", () => {
  it("accumulates ledger points and renders", () => {
    const c = new BreakevenChart();
    const series = [
      { cost: 10, value: 0 },
      { cost: 20, value: 15 },
      { cost: 30, value: 50 }, // net crosses positive here
    ];
    series.forEach((s, i) =>
      c.push(
        epoch(i, i * 0.5, [], "g", {
          cumulative_cost: s.cost,
          value_destroyed: s.value,
          net: s.value - s.cost,
        }),
      ),
    );
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    // crossover marker draws an arc; net readout draws text.
    expect(ctx.calls.some((s) => s.startsWith("arc"))).toBe(true);
    expect(ctx.calls.some((s) => s.startsWith("fillText"))).toBe(true);
  });

  it("windows live history", () => {
    const c = new BreakevenChart({ maxPoints: 2 });
    for (let i = 0; i < 6; i++) {
      c.push(
        epoch(i, i, [], "g", {
          cumulative_cost: i,
          value_destroyed: i,
          net: 0,
        }),
      );
    }
    const { canvas } = fakeCanvas();
    expect(() => c.render(canvas)).not.toThrow();
  });

  it("renders empty state", () => {
    const c = new BreakevenChart();
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    expect(ctx.calls.some((s) => s.includes("awaiting"))).toBe(true);
  });
});

describe("BreakevenChart batch", () => {
  const res: BatchResultsResponse = {
    batch_id: "b1",
    status: "done",
    parameter: "swarm_spec.count",
    values: [4, 8, 16, 32, 64],
    series: { net: [-500, -200, 100, 800, 2000] },
    breakeven_crossover: { crossover: 12 },
  };

  it("parses net series and crossover", () => {
    const v = parseBatch(res);
    expect(v.values).toEqual([4, 8, 16, 32, 64]);
    expect(v.net).toEqual([-500, -200, 100, 800, 2000]);
    expect(v.crossovers).toEqual([12]);
  });

  it("derives net from cost/value when net absent", () => {
    const v = parseBatch({
      ...res,
      series: { cost: [100, 100], value: [50, 300] },
    });
    expect(v.net).toEqual([-50, 200]);
  });

  it("renders batch view with crossover marker", () => {
    const c = new BreakevenChart();
    c.setBatch(res);
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    expect(ctx.calls.some((s) => s.startsWith("arc"))).toBe(true);
    expect(ctx.calls.some((s) => s.includes("breakeven"))).toBe(true);
  });

  it("switches back to live mode", () => {
    const c = new BreakevenChart();
    c.setBatch(res);
    c.push(epoch(0, 0, [], "g", { cumulative_cost: 1, value_destroyed: 2, net: 1 }));
    const { canvas, ctx } = fakeCanvas();
    c.render(canvas);
    expect(ctx.calls.some((s) => s.startsWith("fillText"))).toBe(true);
  });
});

describe("interpCrossings", () => {
  it("interpolates the zero crossing of a net series", () => {
    const x = interpCrossings([
      { v: 0, n: -10 },
      { v: 10, n: 10 },
    ]);
    expect(x).toHaveLength(1);
    expect(x[0]).toBeCloseTo(5);
  });

  it("handles no crossing", () => {
    expect(interpCrossings([{ v: 0, n: 1 }, { v: 1, n: 2 }])).toEqual([]);
  });
});
