// Tests for the pure solver-selection policy in app.ts. These are the only
// non-DOM/Pixi decisions the app makes that have real branching, so they get
// direct coverage; the rest of BeamApp is wiring over already-tested modules
// (render/, panels/, charts/, net/).

import { describe, expect, it } from "vitest";
import {
  DEFAULT_RACE_PAIR,
  pickPreferredSolver,
  pickRacePair,
  WOW_PRESET,
} from "./app";

describe("pickPreferredSolver", () => {
  it("prefers auction when present (strong + fast, pdd 9.2 #4)", () => {
    expect(pickPreferredSolver(["greedy_nearest", "auction", "cp_sat"])).toBe(
      "auction",
    );
  });

  it("falls back to the first available solver", () => {
    expect(pickPreferredSolver(["greedy_threat", "ga"])).toBe("greedy_threat");
  });

  it("hard-falls back to auction on an empty catalog", () => {
    expect(pickPreferredSolver([])).toBe("auction");
  });
});

describe("pickRacePair", () => {
  it("uses the demo-effective default pair when both are available", () => {
    const full = [
      "greedy_nearest",
      "greedy_threat",
      "greedy_urgent",
      "auction",
      "ga",
      "cp_sat",
    ];
    expect(pickRacePair(full)).toEqual(DEFAULT_RACE_PAIR);
  });

  it("always returns two DISTINCT solvers when 2+ exist", () => {
    const [a, b] = pickRacePair(["auction", "ga"]);
    expect(a).not.toBe(b);
    expect(["auction", "ga"]).toContain(a);
    expect(["auction", "ga"]).toContain(b);
  });

  it("substitutes a present solver when a default-pair member is missing", () => {
    // greedy_nearest absent -> first available stands in for slot A.
    const [a, b] = pickRacePair(["greedy_urgent", "auction"]);
    expect(a).toBe("greedy_urgent");
    expect(b).toBe("auction");
    expect(a).not.toBe(b);
  });

  it("degrades to a duplicate only when a single solver exists", () => {
    expect(pickRacePair(["auction"])).toEqual(["auction", "auction"]);
  });
});

describe("WOW_PRESET", () => {
  it("points at the swarm_24 one-click demo scenario (pdd 4 / 15)", () => {
    expect(WOW_PRESET).toBe("swarm_24");
  });
});
