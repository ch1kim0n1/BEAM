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

// --------------------------------------------------------------------------- //
// ScenarioEditor - slide-out drawer with form + YAML tab                      //
// --------------------------------------------------------------------------- //

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
  private formPane!: HTMLElement;
  private yamlPane!: HTMLElement;
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
    const closeBtn = this.btn("X", () => this.close());
    closeBtn.className = "beam-drawer-close";
    header.appendChild(title);
    header.appendChild(closeBtn);

    const tabBar = this.el("div", "beam-drawer-tabs");
    const tabForm = this.btn("Form", () => this.showTab("form"));
    const tabYaml = this.btn("YAML", () => this.showTab("yaml"));
    tabBar.appendChild(tabForm);
    tabBar.appendChild(tabYaml);

    this.formPane = this.buildForm();
    this.yamlPane = this.el("div", "beam-drawer-yaml-pane");
    this.yamlPre = this.el("pre", "beam-yaml-pre") as HTMLPreElement;
    const copyBtn = this.btn("Copy YAML", () => {
      void navigator.clipboard?.writeText(this.yamlPre.textContent ?? "");
    });
    this.yamlPane.appendChild(this.yamlPre);
    this.yamlPane.appendChild(copyBtn);
    this.yamlPane.style.display = "none";

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
    this.drawer.appendChild(this.formPane);
    this.drawer.appendChild(this.yamlPane);
    this.drawer.appendChild(footer);

    this.root.appendChild(this.drawer);
  }

  private buildForm(): HTMLElement {
    const form = this.el("div", "beam-drawer-form");

    const num = (
      key: keyof EditorState,
      label: string,
      min: number,
      max: number,
      step: number,
    ) => {
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
      this.formPane.style.display = "";
      this.yamlPane.style.display = "none";
    } else {
      this.syncFromInputs();
      this.yamlPre.textContent = yamlPreview(editorOverlayFromState(this.state));
      this.formPane.style.display = "none";
      this.yamlPane.style.display = "";
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
      (inp as HTMLInputElement | HTMLSelectElement).value = String(
        this.state[key as keyof EditorState],
      );
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

  private el<K extends keyof HTMLElementTagNameMap>(
    tag: K,
    className: string,
  ): HTMLElementTagNameMap[K] {
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
