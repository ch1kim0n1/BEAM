# Scenario Editor UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A slide-out drawer lets users build and save custom scenarios without touching YAML. Adds drone class mix and seed fields not present in the control panel.

**Architecture:** `ScenarioEditor` class mounts a drawer overlay. Form fields map to `POST /api/scenario` overlay shape. A read-only YAML tab previews the generated config. On save, the app gets a new `scenario_id` and can auto-start. The editor does not replace the control panel — it supplements it with less-frequently-changed fields.

**Tech Stack:** TypeScript DOM, existing `BeamClient.createScenario()`, `js-yaml` (already in devDeps via vite) or manual YAML serialization.

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `frontend/src/panels/editor.ts` | Create | `ScenarioEditor` class + `editorOverlayFromState()` pure helper |
| `frontend/src/panels/editor.test.ts` | Create | Unit tests for `editorOverlayFromState` and `yamlPreview` |
| `frontend/src/panels/index.ts` | Modify | Export `ScenarioEditor` |
| `frontend/src/panels/controls.ts` | Modify | Add "Edit Scenario" button |
| `frontend/src/app/app.ts` | Modify | Mount editor, wire save callback |

---

### Task 1: editorOverlayFromState pure helper

**Files:**
- Create: `frontend/src/panels/editor.ts` (pure functions only for now)
- Create: `frontend/src/panels/editor.test.ts`

- [ ] **Step 1: Write failing tests**

```typescript
// frontend/src/panels/editor.test.ts
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/panels/editor.test.ts
```

Expected: `Cannot find module './editor'`

- [ ] **Step 3: Implement pure functions in editor.ts**

```typescript
// frontend/src/panels/editor.ts
import type { BehaviorProfile } from "../types";

export interface EditorState {
  weather: string;
  seed: number;
  turretCount: number;
  decisionPeriod: number;
  swarmCount: number;
  spawnRadius: number;
  spawnArcStartDeg: number;
  spawnArcEndDeg: number;
  behavior: BehaviorProfile;
  droneSpeed: number;
  /** Fraction [0,1] of drones that are quad_small; remainder are fixed_wing. */
  quadSmallFraction: number;
}

export const DEFAULT_EDITOR_STATE: EditorState = {
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

export function editorOverlayFromState(s: EditorState): Record<string, unknown> {
  const frac = Math.min(1, Math.max(0, s.quadSmallFraction));
  return {
    weather: s.weather,
    seed: s.seed,
    turret_count: s.turretCount,
    decision_period: s.decisionPeriod,
    swarm_spec: {
      count: s.swarmCount,
      behavior: s.behavior,
      spawn_radius: s.spawnRadius,
      spawn_arc_deg: [s.spawnArcStartDeg, s.spawnArcEndDeg],
      speed: s.droneSpeed,
      class_mix: {
        quad_small: frac,
        fixed_wing: 1 - frac,
      },
    },
  };
}

/** Minimal YAML-like preview — no external dependency needed. */
export function yamlPreview(overlay: Record<string, unknown>): string {
  return _toYaml(overlay, 0);
}

function _toYaml(obj: unknown, indent: number): string {
  const pad = "  ".repeat(indent);
  if (obj === null || obj === undefined) return `${pad}null`;
  if (typeof obj === "number" || typeof obj === "boolean") return String(obj);
  if (typeof obj === "string") return obj;
  if (Array.isArray(obj)) {
    return `[${obj.map((v) => _toYaml(v, 0)).join(", ")}]`;
  }
  if (typeof obj === "object") {
    const lines: string[] = [];
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      if (typeof v === "object" && v !== null && !Array.isArray(v)) {
        lines.push(`${pad}${k}:`);
        lines.push(_toYaml(v, indent + 1));
      } else {
        lines.push(`${pad}${k}: ${_toYaml(v, 0)}`);
      }
    }
    return lines.join("\n");
  }
  return String(obj);
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/panels/editor.test.ts
```

Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/panels/editor.ts frontend/src/panels/editor.test.ts
git commit -m "feat: scenario editor pure helper functions (overlay + yaml preview)"
```

---

### Task 2: ScenarioEditor DOM class

**Files:**
- Modify: `frontend/src/panels/editor.ts` (add ScenarioEditor class)

- [ ] **Step 1: Add ScenarioEditor class at the bottom of editor.ts**

```typescript
// --- append to frontend/src/panels/editor.ts ---

export interface ScenarioEditorOptions {
  root: HTMLElement;
  weatherNames: readonly string[];
  onSave: (overlay: Record<string, unknown>) => Promise<void>;
  document?: Document;
}

export class ScenarioEditor {
  private readonly doc: Document;
  private readonly root: HTMLElement;
  private readonly weatherNames: readonly string[];
  private readonly onSave: (overlay: Record<string, unknown>) => Promise<void>;

  private drawer!: HTMLElement;
  private formTab!: HTMLElement;
  private yamlTab!: HTMLElement;
  private yamlPre!: HTMLPreElement;
  private errorEl!: HTMLElement;
  private saveBtn!: HTMLButtonElement;
  private state: EditorState = { ...DEFAULT_EDITOR_STATE };
  private inputs: Partial<Record<keyof EditorState, HTMLInputElement | HTMLSelectElement>> = {};

  constructor(opts: ScenarioEditorOptions) {
    this.doc = opts.document ?? (typeof document !== "undefined" ? document : ({} as Document));
    this.root = opts.root;
    this.weatherNames = opts.weatherNames;
    this.onSave = opts.onSave;
    this.buildDrawer();
  }

  open(currentOverride?: Partial<EditorState>): void {
    if (currentOverride) Object.assign(this.state, currentOverride);
    this.syncToInputs();
    this.drawer.classList.add("beam-drawer-open");
  }

  close(): void {
    this.drawer.classList.remove("beam-drawer-open");
  }

  private buildDrawer(): void {
    this.drawer = this.el("div", "beam-drawer");

    const header = this.el("div", "beam-drawer-header");
    const title = this.el("h2", "beam-drawer-title");
    title.textContent = "Edit Scenario";
    const closeBtn = this.btn("✕", () => this.close());
    closeBtn.className = "beam-drawer-close";
    header.appendChild(title);
    header.appendChild(closeBtn);

    // Tabs
    const tabBar = this.el("div", "beam-drawer-tabs");
    const tabForm = this.btn("Form", () => this.showTab("form"));
    const tabYaml = this.btn("YAML", () => this.showTab("yaml"));
    tabBar.appendChild(tabForm);
    tabBar.appendChild(tabYaml);

    this.formTab = this.buildForm();
    this.yamlTab = this.el("div", "beam-drawer-yaml-pane");
    this.yamlPre = this.el("pre", "beam-yaml-pre") as HTMLPreElement;
    const copyBtn = this.btn("Copy YAML", () => {
      void navigator.clipboard.writeText(this.yamlPre.textContent ?? "");
    });
    this.yamlTab.appendChild(this.yamlPre);
    this.yamlTab.appendChild(copyBtn);
    this.yamlTab.style.display = "none";

    this.errorEl = this.el("div", "beam-drawer-error");
    this.saveBtn = this.btn("Save & Load", () => void this.submit());
    this.saveBtn.className = "beam-drawer-save";
    const cancelBtn = this.btn("Cancel", () => this.close());

    const footer = this.el("div", "beam-drawer-footer");
    footer.appendChild(this.errorEl);
    footer.appendChild(cancelBtn);
    footer.appendChild(this.saveBtn);

    this.drawer.appendChild(header);
    this.drawer.appendChild(tabBar);
    this.drawer.appendChild(this.formTab);
    this.drawer.appendChild(this.yamlTab);
    this.drawer.appendChild(footer);

    this.root.appendChild(this.drawer);
  }

  private buildForm(): HTMLElement {
    const form = this.el("div", "beam-drawer-form");

    const num = (key: keyof EditorState, label: string, min: number, max: number, step: number) => {
      const inp = this.el("input", "") as HTMLInputElement;
      inp.type = "number";
      inp.min = String(min);
      inp.max = String(max);
      inp.step = String(step);
      inp.value = String(this.state[key]);
      inp.addEventListener("input", () => this.syncFromInputs());
      this.inputs[key] = inp;
      return this.fieldRow(label, inp);
    };

    const sel = (key: keyof EditorState, label: string, options: readonly string[]) => {
      const s = this.el("select", "") as HTMLSelectElement;
      for (const opt of options) {
        const o = this.el("option", "") as HTMLOptionElement;
        o.value = opt;
        o.textContent = opt;
        s.appendChild(o);
      }
      s.value = String(this.state[key]);
      s.addEventListener("change", () => this.syncFromInputs());
      this.inputs[key] = s;
      return this.fieldRow(label, s);
    };

    const h = (text: string) => {
      const el = this.el("h3", "beam-drawer-section");
      el.textContent = text;
      return el;
    };

    form.appendChild(h("Swarm"));
    form.appendChild(num("swarmCount", "Drone count", 1, 512, 1));
    form.appendChild(num("spawnRadius", "Spawn radius (m)", 100, 20000, 100));
    form.appendChild(num("spawnArcStartDeg", "Arc start (deg)", 0, 360, 5));
    form.appendChild(num("spawnArcEndDeg", "Arc end (deg)", 0, 360, 5));
    form.appendChild(sel("behavior", "Behavior", ["direct", "flocking", "staggered"]));
    form.appendChild(num("droneSpeed", "Speed (m/s)", 1, 200, 1));
    form.appendChild(num("quadSmallFraction", "Quad-small fraction (0-1)", 0, 1, 0.05));

    form.appendChild(h("Environment"));
    form.appendChild(sel("weather", "Weather", this.weatherNames));
    form.appendChild(num("seed", "Seed", 0, 2147483647, 1));

    form.appendChild(h("Timing"));
    form.appendChild(num("turretCount", "Turrets", 1, 32, 1));
    form.appendChild(num("decisionPeriod", "Decision period (s)", 0.05, 5, 0.05));

    return form;
  }

  private fieldRow(label: string, input: HTMLElement): HTMLElement {
    const row = this.el("div", "beam-field");
    const lab = this.el("label", "beam-field-label");
    lab.textContent = label;
    row.appendChild(lab);
    row.appendChild(input);
    return row;
  }

  private showTab(tab: "form" | "yaml"): void {
    if (tab === "form") {
      this.formTab.style.display = "";
      this.yamlTab.style.display = "none";
    } else {
      this.syncFromInputs();
      this.yamlPre.textContent = yamlPreview(editorOverlayFromState(this.state));
      this.formTab.style.display = "none";
      this.yamlTab.style.display = "";
    }
  }

  private syncFromInputs(): void {
    const n = (key: keyof EditorState, lo: number, hi: number): number => {
      const inp = this.inputs[key] as HTMLInputElement | undefined;
      if (!inp) return this.state[key] as number;
      const v = Number(inp.value);
      return Number.isNaN(v) ? lo : Math.min(hi, Math.max(lo, v));
    };
    const s = (key: keyof EditorState): string => {
      const inp = this.inputs[key] as HTMLSelectElement | undefined;
      return inp ? inp.value : String(this.state[key]);
    };

    this.state = {
      weather: s("weather"),
      seed: Math.round(n("seed", 0, 2147483647)),
      turretCount: Math.round(n("turretCount", 1, 32)),
      decisionPeriod: n("decisionPeriod", 0.05, 5),
      swarmCount: Math.round(n("swarmCount", 1, 512)),
      spawnRadius: n("spawnRadius", 100, 20000),
      spawnArcStartDeg: n("spawnArcStartDeg", 0, 360),
      spawnArcEndDeg: n("spawnArcEndDeg", 0, 360),
      behavior: s("behavior") as BehaviorProfile,
      droneSpeed: n("droneSpeed", 1, 200),
      quadSmallFraction: n("quadSmallFraction", 0, 1),
    };
  }

  private syncToInputs(): void {
    for (const [key, inp] of Object.entries(this.inputs)) {
      if (!inp) continue;
      inp.value = String(this.state[key as keyof EditorState]);
    }
  }

  private async submit(): Promise<void> {
    this.syncFromInputs();
    this.errorEl.textContent = "";
    this.saveBtn.disabled = true;
    try {
      await this.onSave(editorOverlayFromState(this.state));
      this.close();
    } catch (e) {
      this.errorEl.textContent = `Error: ${String(e)}`;
    } finally {
      this.saveBtn.disabled = false;
    }
  }

  private el<K extends keyof HTMLElementTagNameMap>(tag: K, className: string): HTMLElementTagNameMap[K] {
    const node = this.doc.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  private btn(label: string, onClick: () => void): HTMLButtonElement {
    const b = this.el("button", "");
    b.type = "button";
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }
}
```

- [ ] **Step 2: Run all panel tests**

```bash
cd frontend && npx vitest run src/panels/
```

Expected: all PASS (new class has no tests to break; existing pure-function tests still pass)

- [ ] **Step 3: Commit**

```bash
git add frontend/src/panels/editor.ts
git commit -m "feat: ScenarioEditor drawer class with form + YAML tab"
```

---

### Task 3: Wire editor into app

**Files:**
- Modify: `frontend/src/panels/index.ts`
- Modify: `frontend/src/panels/controls.ts`
- Modify: `frontend/src/app/app.ts`

- [ ] **Step 1: Export ScenarioEditor from panels/index.ts**

In `frontend/src/panels/index.ts`, add:

```typescript
export { ScenarioEditor, type EditorState, editorOverlayFromState, yamlPreview } from "./editor";
```

- [ ] **Step 2: Add "Edit Scenario" button to ControlPanel**

At the end of `mount()` in `frontend/src/panels/controls.ts`, before `this.refreshButtons()`:

```typescript
    const editBtn = this.button("Edit Scenario", () => {
      this.sink.send({ action: "stop" } as ControlMessage);
      this.sink.openEditor?.();
    });
    editBtn.className = "beam-edit-scenario";
    this.root.appendChild(editBtn);
```

Also extend `ControlSink` interface:

```typescript
export interface ControlSink {
  start(overrides: Record<string, unknown>): void | Promise<void>;
  reset(overrides: Record<string, unknown>): void | Promise<void>;
  send(msg: ControlMessage): void;
  openEditor?(): void;  // optional — app wires this when editor is mounted
}
```

- [ ] **Step 3: Mount editor in BeamApp**

In `frontend/src/app/app.ts`, add import:

```typescript
import { ScenarioEditor } from "../panels";
```

Add field declaration:

```typescript
  private editor!: ScenarioEditor;
```

In `init()`, after `this.mountPanel()`:

```typescript
    this.mountEditor();
```

Add `mountEditor()` method:

```typescript
  private mountEditor(): void {
    this.editor = new ScenarioEditor({
      root: this.root,
      weatherNames: this.weatherNames,
      document: this.doc,
      onSave: async (overlay) => {
        this.setStatus("creating scenario from editor…");
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
```

In `mountPanel()`, extend the sink to wire `openEditor`:

```typescript
    const sink: ControlSink = {
      start: (overrides) => void this.startFromControls(overrides),
      reset: (overrides) => void this.startFromControls(overrides),
      send: (msg) => this.broadcastControl(msg),
      openEditor: () => this.editor.open(),
    };
```

- [ ] **Step 4: Run all tests**

```bash
cd frontend && npx vitest run
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/panels/index.ts frontend/src/panels/controls.ts frontend/src/app/app.ts
git commit -m "feat: wire ScenarioEditor into BeamApp with Edit Scenario button"
```

---

## Self-review checklist

- [x] `editorOverlayFromState` tested — maps `quadSmallFraction` to `class_mix` dict correctly
- [x] `yamlPreview` does not depend on external libraries — plain recursive serializer
- [x] `ControlSink.openEditor` is optional so existing tests that mock the sink still pass
- [x] `onSave` error surfaces in drawer error element — user does not lose form state
- [x] `EditorState.droneSpeed` maps to `SwarmSpec.speed` — already a valid schema field
- [x] `class_mix` with `quad_small` + `fixed_wing` — both keys exist in `config/defaults.yaml` `drone_classes`
