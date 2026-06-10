import { describe, it, expect } from "vitest";
import { editorOverlayFromState, yamlPreview, type EditorState } from "./editor";

const BASE: EditorState = {
  weather: "clear",
  seed: 1337,
  turretCount: 4,
  decisionPeriod: 0.5,
  swarmCount: 24,
  spawnRadius: 4000,
  spawnArcStartDeg: 0,
  spawnArcEndDeg: 360,
  behavior: "flocking",
  droneSpeed: 18,
  quadSmallFraction: 0.8,
};

describe("editorOverlayFromState", () => {
  it("includes weather and seed at top level", () => {
    const overlay = editorOverlayFromState(BASE);
    expect(overlay.weather).toBe("clear");
    expect(overlay.seed).toBe(1337);
  });

  it("maps quadSmallFraction to class_mix", () => {
    const overlay = editorOverlayFromState(BASE);
    const mix = (overlay.swarm_spec as Record<string, unknown>).class_mix as Record<string, number>;
    expect(mix.quad_small).toBeCloseTo(0.8);
    expect(mix.fixed_wing).toBeCloseTo(0.2);
  });

  it("clamps quadSmallFraction to [0,1]", () => {
    const overlay = editorOverlayFromState({ ...BASE, quadSmallFraction: 1.5 });
    const mix = (overlay.swarm_spec as Record<string, unknown>).class_mix as Record<string, number>;
    expect(mix.quad_small).toBe(1.0);
    expect(mix.fixed_wing).toBe(0.0);
  });

  it("includes turret_count and decision_period", () => {
    const overlay = editorOverlayFromState(BASE);
    expect(overlay.turret_count).toBe(4);
    expect(overlay.decision_period).toBe(0.5);
  });
});

describe("yamlPreview", () => {
  it("returns a non-empty string containing weather", () => {
    const yaml = yamlPreview(editorOverlayFromState(BASE));
    expect(yaml).toContain("weather");
    expect(yaml.length).toBeGreaterThan(10);
  });
});
