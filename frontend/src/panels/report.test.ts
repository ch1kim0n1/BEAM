// Tests for the after-action report's pure layer: metric derivation + formatting.
// The DOM view is thin glue over these, so locking the math + formatting here is
// what protects the report's correctness.

import { describe, expect, it } from "vitest";
import type { RunSummary } from "../types";
import {
  computeReportMetrics,
  fmtMoney,
  fmtMs,
  fmtPct,
  fmtRatio,
  shortHash,
  type RunReportContext,
} from "./report";

function summary(over: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "run_1",
    kills: 20,
    leaks: 4,
    leaked_value: 8000,
    final_ledger: {
      cumulative_cost: 5000,
      value_destroyed: 40000,
      net: 35000,
      shot_energy_cost: 1200,
      maintenance_cost: 800,
      capex_amortized: 3000,
      engagements: 24,
    },
    avg_gap_by_solver: { cp_sat: 0, auction: 0.03, greedy_nearest: 0.12 },
    avg_solve_ms_by_solver: { cp_sat: 45.2, auction: 2.1, greedy_nearest: 0.8 },
    ...over,
  };
}

const ctx: RunReportContext = {
  scenarioLabel: "swarm_24",
  seed: 1337,
  activeSolver: "auction",
  referenceSolver: "cp_sat",
  protectedValueFrac: 0.83,
  simSeconds: 95.4,
  epochs: 190,
};

describe("computeReportMetrics", () => {
  it("derives the cost-exchange headline figures", () => {
    const m = computeReportMetrics(summary(), ctx);
    expect(m.net).toBe(35000);
    expect(m.costExchangeRatio).toBeCloseTo(8, 6); // 40000 / 5000
    expect(m.costPerKill).toBeCloseTo(250, 6); // 5000 / 20
    expect(m.engaged).toBe(24);
  });

  it("guards against zero denominators (no kills / no cost)", () => {
    const m = computeReportMetrics(
      summary({
        kills: 0,
        final_ledger: {
          cumulative_cost: 0,
          value_destroyed: 0,
          net: 0,
          shot_energy_cost: 0,
          maintenance_cost: 0,
          capex_amortized: 0,
          engagements: 0,
        },
      }),
      ctx,
    );
    expect(m.costExchangeRatio).toBeNull();
    expect(m.costPerKill).toBeNull();
  });

  it("flags the active + reference solvers and surfaces the active latency/gap", () => {
    const m = computeReportMetrics(summary(), ctx);
    expect(m.activeSolveMs).toBeCloseTo(2.1, 6);
    expect(m.activeGap).toBeCloseTo(0.03, 6);
    const active = m.solvers.find((s) => s.isActive);
    expect(active?.name).toBe("auction");
    expect(m.solvers.find((s) => s.isReference)?.name).toBe("cp_sat");
  });

  it("sorts the leaderboard reference-first, then by ascending gap", () => {
    const m = computeReportMetrics(summary(), ctx);
    expect(m.solvers.map((s) => s.name)).toEqual([
      "cp_sat", // reference pinned first
      "auction", // 0.03
      "greedy_nearest", // 0.12
    ]);
  });
});

describe("formatters", () => {
  it("formats compact currency with optional sign", () => {
    expect(fmtMoney(40000)).toBe("$40k");
    expect(fmtMoney(1_250_000)).toBe("$1.25M");
    expect(fmtMoney(35000, true)).toBe("+$35k");
    expect(fmtMoney(-800)).toBe("-$800");
  });

  it("formats percentages, ratios, latency and hashes defensively", () => {
    expect(fmtPct(0.83)).toBe("83%");
    expect(fmtPct(null)).toBe("—");
    expect(fmtRatio(8)).toBe("8.0×");
    expect(fmtRatio(1.5)).toBe("1.5×");
    expect(fmtRatio(12)).toBe("12×");
    expect(fmtMs(2.1)).toBe("2.1 ms");
    expect(fmtMs(45.2)).toBe("45 ms");
    expect(fmtMs(null)).toBe("—");
    expect(shortHash("sha256:" + "a".repeat(64))).toBe("aaaaaaaaaa…aaaaaa");
    expect(shortHash(null)).toBe("—");
  });
});
