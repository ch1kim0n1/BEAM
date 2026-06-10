// Tests for the control panel + scoreboard.
//
// vitest runs in a node environment here (no jsdom installed), so the DOM-view
// classes are exercised against a tiny hand-rolled fake DOM that implements just
// the surface controls.ts / scoreboard.ts touch. The pure helpers are tested
// directly with plain data.

import { beforeEach, describe, expect, it } from "vitest";
import type { ControlMessage, EpochMessage, FrameMessage } from "../types";
import {
  clampNumber,
  clampSpeed,
  liveControlMessages,
  scenarioOverridesFromState,
  ControlPanel,
  DEFAULT_CONTROL_STATE,
  type ControlSink,
  type ControlState,
} from "./controls";
import {
  emptyValueAccumulator,
  foldFrameValue,
  formatClock,
  formatPercent,
  protectedValueFraction,
  Scoreboard,
} from "./scoreboard";

// --------------------------------------------------------------------------- //
// Minimal fake DOM                                                            //
// --------------------------------------------------------------------------- //

class FakeClassList {
  private set = new Set<string>();
  add(...names: string[]) {
    for (const n of names) this.set.add(n);
  }
  contains(n: string) {
    return this.set.has(n);
  }
}

class FakeElement {
  children: FakeElement[] = [];
  attrs: Record<string, string> = {};
  classList = new FakeClassList();
  className = "";
  textContent = "";
  value = "";
  disabled = false;
  private listeners: Record<string, Array<() => void>> = {};
  ownerDocument: FakeDocument;

  constructor(
    public tagName: string,
    doc: FakeDocument,
  ) {
    this.ownerDocument = doc;
  }
  setAttribute(k: string, v: string) {
    this.attrs[k] = v;
  }
  /** True if a class name is present via classList, the `className` field, or a
   *  `class` attribute - controls.ts uses the attr form, scoreboard.ts the field. */
  hasClass(name: string): boolean {
    if (this.classList.contains(name)) return true;
    const fromField = this.className.split(/\s+/);
    const fromAttr = (this.attrs.class ?? "").split(/\s+/);
    return fromField.includes(name) || fromAttr.includes(name);
  }
  appendChild(child: FakeElement) {
    this.children.push(child);
    return child;
  }
  replaceChildren() {
    this.children = [];
  }
  addEventListener(type: string, cb: () => void) {
    (this.listeners[type] ??= []).push(cb);
  }
  dispatch(type: string) {
    for (const cb of this.listeners[type] ?? []) cb();
  }
  /** Depth-first search for the first descendant matching a predicate. */
  find(pred: (el: FakeElement) => boolean): FakeElement | undefined {
    for (const c of this.children) {
      if (pred(c)) return c;
      const deep = c.find(pred);
      if (deep) return deep;
    }
    return undefined;
  }
  findAll(pred: (el: FakeElement) => boolean): FakeElement[] {
    const out: FakeElement[] = [];
    for (const c of this.children) {
      if (pred(c)) out.push(c);
      out.push(...c.findAll(pred));
    }
    return out;
  }
}

class FakeDocument {
  createElement(tag: string): FakeElement {
    return new FakeElement(tag, this);
  }
}

function makeRoot(): { doc: FakeDocument; root: FakeElement } {
  const doc = new FakeDocument();
  return { doc, root: doc.createElement("div") };
}

function buttonByLabel(root: FakeElement, label: string): FakeElement {
  const b = root.find(
    (e) => e.tagName === "button" && (e.textContent === label),
  );
  if (!b) throw new Error(`button not found: ${label}`);
  return b;
}

function fieldInput(root: FakeElement, labelText: string): FakeElement {
  const wrap = root.find(
    (e) => e.hasClass("beam-field") &&
      !!e.find((c) => c.tagName === "label" && c.textContent === labelText),
  );
  if (!wrap) throw new Error(`field not found: ${labelText}`);
  const inp = wrap.find((c) => c.tagName === "input" || c.tagName === "select");
  if (!inp) throw new Error(`input not found for: ${labelText}`);
  return inp;
}

// A recording sink.
function makeSink() {
  const sent: ControlMessage[] = [];
  const starts: Record<string, unknown>[] = [];
  const resets: Record<string, unknown>[] = [];
  const sink: ControlSink = {
    start: (o) => {
      starts.push(o);
    },
    reset: (o) => {
      resets.push(o);
    },
    send: (m) => {
      sent.push(m);
    },
  };
  return { sink, sent, starts, resets };
}

const asDoc = (d: FakeDocument) => d as unknown as Document;
const asRoot = (e: FakeElement) => e as unknown as HTMLElement;

// --------------------------------------------------------------------------- //
// controls.ts - pure helpers                                                  //
// --------------------------------------------------------------------------- //

describe("controls pure helpers", () => {
  it("clampNumber clamps and collapses NaN to lo", () => {
    expect(clampNumber(5, 0, 10)).toBe(5);
    expect(clampNumber(-3, 0, 10)).toBe(0);
    expect(clampNumber(99, 0, 10)).toBe(10);
    expect(clampNumber(Number.NaN, 2, 10)).toBe(2);
  });

  it("clampSpeed snaps to the nearest offered multiplier (log scale)", () => {
    expect(clampSpeed(1)).toBe(1);
    expect(clampSpeed(3)).toBe(4); // 3 is closer to 4 than 2 on a log scale
    expect(clampSpeed(0.3)).toBe(0.25);
    expect(clampSpeed(100)).toBe(8);
    expect(clampSpeed(0)).toBe(1); // invalid -> 1
    expect(clampSpeed(Number.NaN)).toBe(1);
  });

  it("scenarioOverridesFromState emits only setup-time knobs", () => {
    const state: ControlState = {
      ...DEFAULT_CONTROL_STATE,
      swarmSize: 48,
      behavior: "staggered",
      weather: "fog",
      turretCount: 6,
      decisionPeriod: 0.25,
      spawnRadius: 5000,
      spawnArcStartDeg: 30,
      spawnArcEndDeg: 210,
      simSpeed: 4, // live, must NOT appear
      activeSolver: "cp_sat", // live, must NOT appear
    };
    const o = scenarioOverridesFromState(state);
    expect(o).toEqual({
      weather: "fog",
      decision_period: 0.25,
      turret_count: 6,
      swarm_spec: {
        count: 48,
        behavior: "staggered",
        spawn_radius: 5000,
        spawn_arc_deg: [30, 210],
      },
    });
    expect(JSON.stringify(o)).not.toContain("solver");
    expect(JSON.stringify(o)).not.toContain("speed");
  });

  it("liveControlMessages emits set_solver then set_speed (snapped)", () => {
    const msgs = liveControlMessages({ ...DEFAULT_CONTROL_STATE, activeSolver: "ga", simSpeed: 3 });
    expect(msgs).toEqual([
      { action: "set_solver", solver: "ga" },
      { action: "set_speed", multiplier: 4 },
    ]);
  });
});

// --------------------------------------------------------------------------- //
// controls.ts - DOM view behaviour                                            //
// --------------------------------------------------------------------------- //

describe("ControlPanel view", () => {
  let env: ReturnType<typeof makeRoot>;
  beforeEach(() => {
    env = makeRoot();
  });

  it("renders every 14.1 control and seeds defaults", () => {
    const { sink } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    // each labelled field present
    for (const label of [
      "Swarm size",
      "Spawn radius (m)",
      "Spawn arc start (deg)",
      "Spawn arc end (deg)",
      "Behavior",
      "Weather",
      "Turrets",
      "Decision period (s)",
      "Active solver",
      "Sim speed",
    ]) {
      expect(() => fieldInput(env.root, label)).not.toThrow();
    }
    expect(fieldInput(env.root, "Swarm size").value).toBe("24");
    expect(env.root.classList.contains("beam-controls")).toBe(true);
  });

  it("Start reads inputs and pushes scenario overrides to the sink", () => {
    const { sink, starts } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    const swarm = fieldInput(env.root, "Swarm size");
    swarm.value = "64";
    swarm.dispatch("change");
    buttonByLabel(env.root, "Start").dispatch("click");
    expect(starts).toHaveLength(1);
    expect((starts[0].swarm_spec as Record<string, unknown>).count).toBe(64);
  });

  it("Start disables itself and enables Pause/Step", () => {
    const { sink } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    const start = buttonByLabel(env.root, "Start");
    const pause = buttonByLabel(env.root, "Pause");
    expect(pause.disabled).toBe(true);
    start.dispatch("click");
    expect(start.disabled).toBe(true);
    expect(pause.disabled).toBe(false);
  });

  it("Pause toggles to Resume and emits pause/resume messages", () => {
    const { sink, sent } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    buttonByLabel(env.root, "Start").dispatch("click");
    const pause = buttonByLabel(env.root, "Pause");
    pause.dispatch("click");
    expect(sent.at(-1)).toEqual({ action: "pause" });
    expect(pause.textContent).toBe("Resume");
    pause.dispatch("click");
    expect(sent.at(-1)).toEqual({ action: "resume" });
    expect(pause.textContent).toBe("Pause");
  });

  it("Step emits a single-epoch step and leaves the stream paused", () => {
    const { sink, sent } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    buttonByLabel(env.root, "Start").dispatch("click");
    // Capture the pause/resume toggle before its label flips on step.
    const toggle = buttonByLabel(env.root, "Pause");
    buttonByLabel(env.root, "Step").dispatch("click");
    expect(sent.at(-1)).toEqual({ action: "step", epochs: 1 });
    expect(toggle.textContent).toBe("Resume");
  });

  it("Reset stops a running stream then rebuilds the scenario", () => {
    const { sink, sent, resets } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    buttonByLabel(env.root, "Start").dispatch("click");
    buttonByLabel(env.root, "Reset").dispatch("click");
    expect(sent.at(-1)).toEqual({ action: "stop" });
    expect(resets).toHaveLength(1);
    expect(buttonByLabel(env.root, "Start").disabled).toBe(false);
  });

  it("live solver change emits set_solver only while running", () => {
    const { sink, sent } = makeSink();
    new ControlPanel({
      root: asRoot(env.root),
      sink,
      document: asDoc(env.doc),
      solverNames: ["auction", "cp_sat", "ga"],
    });
    const solver = fieldInput(env.root, "Active solver");
    solver.value = "cp_sat";
    solver.dispatch("change"); // not running yet
    expect(sent).toHaveLength(0);
    buttonByLabel(env.root, "Start").dispatch("click");
    solver.value = "ga";
    solver.dispatch("change");
    expect(sent.at(-1)).toEqual({ action: "set_solver", solver: "ga" });
  });

  it("live speed change emits a snapped set_speed while running", () => {
    const { sink, sent } = makeSink();
    new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    buttonByLabel(env.root, "Start").dispatch("click");
    const speed = fieldInput(env.root, "Sim speed");
    speed.value = "4";
    speed.dispatch("change");
    expect(sent.at(-1)).toEqual({ action: "set_speed", multiplier: 4 });
  });

  it("getState reflects live input edits with clamping", () => {
    const { sink } = makeSink();
    const panel = new ControlPanel({ root: asRoot(env.root), sink, document: asDoc(env.doc) });
    const swarm = fieldInput(env.root, "Swarm size");
    swarm.value = "9999"; // above max 512
    const st = panel.getState();
    expect(st.swarmSize).toBe(512);
  });
});

// --------------------------------------------------------------------------- //
// scoreboard.ts - value accumulator + formatting                              //
// --------------------------------------------------------------------------- //

function frame(partial: Partial<FrameMessage> & Pick<FrameMessage, "t">): FrameMessage {
  return {
    type: "frame",
    schema_version: "1.0",
    drones: [],
    turrets: [],
    beams: [],
    leaks: 0,
    kills: 0,
    ...partial,
  } as FrameMessage;
}

function drone(id: string, value: number, state: FrameMessage["drones"][number]["state"]) {
  return {
    id,
    x: 0,
    y: 0,
    v: [0, 0] as [number, number],
    value,
    hp_frac: 1,
    state,
    tti: 0,
  };
}

describe("scoreboard value accumulator", () => {
  it("attributes leaked value once and computes protected fraction", () => {
    const acc = emptyValueAccumulator();
    // two drones enter: total = 3000
    foldFrameValue(acc, frame({ t: 1, drones: [drone("a", 2000, "alive"), drone("b", 1000, "alive")] }));
    expect(acc.totalValue).toBe(3000);
    expect(protectedValueFraction(acc)).toBe(1);
    // a leaks
    foldFrameValue(acc, frame({ t: 2, drones: [drone("a", 2000, "leaked"), drone("b", 1000, "alive")] }));
    expect(acc.leakedValue).toBe(2000);
    expect(protectedValueFraction(acc)).toBeCloseTo(1 / 3);
  });

  it("does not double-count a leaked drone across replayed frames", () => {
    const acc = emptyValueAccumulator();
    foldFrameValue(acc, frame({ t: 1, drones: [drone("a", 2000, "leaked")] }));
    foldFrameValue(acc, frame({ t: 2, drones: [drone("a", 2000, "leaked")] }));
    expect(acc.leakedValue).toBe(2000);
    expect(acc.totalValue).toBe(2000);
  });

  it("killed drones reduce protected loss (only leaks count against it)", () => {
    const acc = emptyValueAccumulator();
    foldFrameValue(acc, frame({ t: 1, drones: [drone("a", 5000, "dead"), drone("b", 5000, "leaked")] }));
    expect(acc.leakedValue).toBe(5000);
    expect(protectedValueFraction(acc)).toBeCloseTo(0.5);
  });

  it("empty accumulator protects 100%", () => {
    expect(protectedValueFraction(emptyValueAccumulator())).toBe(1);
  });

  it("formatClock renders mm:ss.t", () => {
    expect(formatClock(0)).toBe("00:00.0");
    expect(formatClock(65.4)).toBe("01:05.4");
    expect(formatClock(-3)).toBe("00:00.0");
  });

  it("formatPercent rounds and clamps", () => {
    expect(formatPercent(0.873)).toBe("87%");
    expect(formatPercent(1.2)).toBe("100%");
    expect(formatPercent(-0.5)).toBe("0%");
  });
});

// --------------------------------------------------------------------------- //
// scoreboard.ts - DOM view                                                    //
// --------------------------------------------------------------------------- //

describe("Scoreboard view", () => {
  it("paints frame + epoch telemetry into stat cells", () => {
    const { doc, root } = makeRoot();
    const sb = new Scoreboard({ root: asRoot(root), document: asDoc(doc), title: "auction" });
    sb.applyFrame(
      frame({ t: 12.3, kills: 7, leaks: 2, drones: [drone("a", 2000, "leaked"), drone("b", 8000, "alive")] }),
    );
    sb.applyEpoch({
      type: "epoch",
      schema_version: "1.0",
      t: 12.5,
      epoch: 25,
      solvers: [],
      active_solver: "auction",
      ledger: { cumulative_cost: 0, value_destroyed: 0, net: 0 },
    } as EpochMessage);

    const valueOf = (statClass: string) => {
      const cell = root.find((e) => e.hasClass(statClass));
      return cell?.find((c) => c.hasClass("beam-stat-value"))?.textContent;
    };
    expect(valueOf("beam-stat-kills")).toBe("7");
    expect(valueOf("beam-stat-leaks")).toBe("2");
    expect(valueOf("beam-stat-protected")).toBe("80%"); // 1 - 2000/10000
    expect(valueOf("beam-stat-solver")).toBe("auction");
    expect(valueOf("beam-stat-clock")).toBe("00:12.5");
    expect(sb.model.epoch).toBe(25);
  });

  it("reset clears model and accumulator", () => {
    const { doc, root } = makeRoot();
    const sb = new Scoreboard({ root: asRoot(root), document: asDoc(doc) });
    sb.applyFrame(frame({ t: 5, kills: 3, leaks: 1, drones: [drone("a", 1000, "leaked")] }));
    sb.reset();
    expect(sb.model.kills).toBe(0);
    expect(sb.model.leaks).toBe(0);
    expect(sb.model.protectedValueFrac).toBe(1);
    expect(sb.model.simClock).toBe(0);
  });
});
