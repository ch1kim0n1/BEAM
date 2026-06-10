# Shareable Run Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any run can be shared as a URL that auto-loads and starts the identical configuration when opened.

**Architecture:** Encode `{ v:1, preset?, scenario?, solver, seed? }` as base64url into a `?run=` query param. On page load, parse and auto-start. No backend changes. Named presets encode as `preset` string; custom scenarios encode the full overlay dict from `GET /api/scenario/{id}`.

**Tech Stack:** TypeScript, existing `BeamClient.getScenario()`, `navigator.clipboard`

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `frontend/src/app/shareLink.ts` | Create | Pure encode/decode/parse functions |
| `frontend/src/app/shareLink.test.ts` | Create | Unit tests for all encode/decode cases |
| `frontend/src/app/app.ts` | Modify | Share button, `loadFromShareToken()`, read on init |
| `frontend/src/app/index.ts` | Modify | Re-export `ShareToken` |
| `frontend/src/panels/controls.ts` | Modify | Add `setSolver(name)` to `ControlPanel` |

---

### Task 1: shareLink pure functions

**Files:**
- Create: `frontend/src/app/shareLink.ts`
- Create: `frontend/src/app/shareLink.test.ts`

- [ ] **Step 1: Write the failing tests**

```typescript
// frontend/src/app/shareLink.test.ts
import { describe, it, expect } from "vitest";
import { encodeShareToken, decodeShareToken, shareUrl } from "./shareLink";

describe("encodeShareToken", () => {
  it("returns a non-empty base64 string", () => {
    const result = encodeShareToken({ v: 1, solver: "auction", preset: "swarm_24" });
    expect(typeof result).toBe("string");
    expect(result.length).toBeGreaterThan(0);
  });
});

describe("decodeShareToken", () => {
  it("round-trips a preset token", () => {
    const token = { v: 1 as const, solver: "auction", preset: "swarm_24", seed: 1337 };
    expect(decodeShareToken(encodeShareToken(token))).toEqual(token);
  });

  it("round-trips a custom scenario token", () => {
    const token = {
      v: 1 as const,
      solver: "ga",
      scenario: { weather: "rain", swarm_spec: { count: 12 } },
    };
    expect(decodeShareToken(encodeShareToken(token))).toEqual(token);
  });

  it("returns null for garbage input", () => {
    expect(decodeShareToken("not-valid-base64!!!")).toBeNull();
  });

  it("returns null for wrong version", () => {
    expect(decodeShareToken(btoa(JSON.stringify({ v: 2, solver: "auction" })))).toBeNull();
  });

  it("returns null for missing solver field", () => {
    expect(decodeShareToken(btoa(JSON.stringify({ v: 1 })))).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(decodeShareToken("")).toBeNull();
  });
});

describe("shareUrl", () => {
  it("includes the ?run= param", () => {
    const token = { v: 1 as const, solver: "auction", preset: "swarm_24" };
    const url = shareUrl(token, "http://localhost:5173");
    expect(url).toContain("?run=");
    expect(url.startsWith("http://localhost:5173")).toBe(true);
  });

  it("URL-encodes the token", () => {
    const token = { v: 1 as const, solver: "auction" };
    const url = shareUrl(token, "http://localhost:5173");
    expect(url).not.toContain("+");
    expect(url).not.toContain("=");
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd frontend && npx vitest run src/app/shareLink.test.ts
```

Expected: `Cannot find module './shareLink'`

- [ ] **Step 3: Implement shareLink.ts**

```typescript
// frontend/src/app/shareLink.ts

export interface ShareToken {
  v: 1;
  preset?: string;
  scenario?: Record<string, unknown>;
  solver: string;
  seed?: number;
}

export function encodeShareToken(token: ShareToken): string {
  return btoa(JSON.stringify(token));
}

export function decodeShareToken(encoded: string): ShareToken | null {
  if (!encoded) return null;
  try {
    const raw: unknown = JSON.parse(atob(encoded));
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
    const r = raw as Record<string, unknown>;
    if (r["v"] !== 1 || typeof r["solver"] !== "string") return null;
    return r as unknown as ShareToken;
  } catch {
    return null;
  }
}

export function shareUrl(token: ShareToken, base?: string): string {
  const b =
    base ??
    (typeof window !== "undefined"
      ? window.location.origin + window.location.pathname
      : "http://localhost:5173");
  return `${b}?run=${encodeURIComponent(encodeShareToken(token))}`;
}

export function parseUrlToken(): ShareToken | null {
  if (typeof window === "undefined") return null;
  const encoded = new URLSearchParams(window.location.search).get("run");
  return encoded ? decodeShareToken(encoded) : null;
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd frontend && npx vitest run src/app/shareLink.test.ts
```

Expected: all 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/app/shareLink.ts frontend/src/app/shareLink.test.ts
git commit -m "feat: add shareLink encode/decode for run URL sharing"
```

---

### Task 2: Add setSolver to ControlPanel

**Files:**
- Modify: `frontend/src/panels/controls.ts` (add public method after `setPaused`)

- [ ] **Step 1: Write the failing test**

Add to `frontend/src/panels/panels.test.ts` (or create a new describe block if file exists):

```typescript
// In frontend/src/panels/panels.test.ts — add this describe block
describe("ControlPanel.setSolver", () => {
  it("updates the active solver state", () => {
    const root = document.createElement("div");
    const sink = { start: () => {}, reset: () => {}, send: () => {} };
    const panel = new ControlPanel({
      root,
      sink,
      solverNames: ["auction", "cp_sat", "ga"],
      initial: { activeSolver: "auction" },
      document,
    });
    panel.setSolver("ga");
    expect(panel.getState().activeSolver).toBe("ga");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend && npx vitest run src/panels/panels.test.ts
```

Expected: `panel.setSolver is not a function`

- [ ] **Step 3: Add setSolver to ControlPanel**

In `frontend/src/panels/controls.ts`, add after the `setPaused` method (around line 234):

```typescript
  setSolver(name: string): void {
    const sel = this.inputs.activeSolver as HTMLSelectElement | undefined;
    if (sel) sel.value = name;
    this.state.activeSolver = name;
  }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd frontend && npx vitest run src/panels/panels.test.ts
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/panels/controls.ts frontend/src/panels/panels.test.ts
git commit -m "feat: add ControlPanel.setSolver for programmatic solver selection"
```

---

### Task 3: Share button and URL loading in BeamApp

**Files:**
- Modify: `frontend/src/app/app.ts`
- Modify: `frontend/src/app/index.ts`

- [ ] **Step 1: Write the failing test**

In `frontend/src/app/app.test.ts`, add:

```typescript
// Add import at top
import { encodeShareToken } from "./shareLink";

describe("BeamApp share link", () => {
  it("Share button is disabled before a run loads", async () => {
    const root = document.createElement("div");
    const app = new BeamApp({ root, client: makeMockClient() });
    await app.init();
    const btn = root.querySelector<HTMLButtonElement>(".beam-share");
    expect(btn).toBeTruthy();
    expect(btn!.disabled).toBe(true);
  });
});
```

Where `makeMockClient()` returns a `BeamClient` with mocked methods returning minimal valid responses.

- [ ] **Step 2: Run test to verify it fails**

```bash
cd frontend && npx vitest run src/app/app.test.ts
```

Expected: fails because `.beam-share` button does not exist

- [ ] **Step 3: Add Share button and loadFromShareToken to app.ts**

**3a.** Add to imports at top of `frontend/src/app/app.ts`:

```typescript
import { parseUrlToken, shareUrl, type ShareToken } from "./shareLink";
```

**3b.** Add `elShareBtn` field declaration after `elReportBtn`:

```typescript
  private elShareBtn!: HTMLButtonElement;
```

**3c.** Add Share button in `buildLayout()`, inside `topActions` block after the Report button:

```typescript
    this.elShareBtn = this.doc.createElement("button");
    this.elShareBtn.className = "beam-share";
    this.elShareBtn.type = "button";
    this.elShareBtn.textContent = "⧉ Share";
    this.elShareBtn.disabled = true;
    this.elShareBtn.addEventListener("click", () => void this.copyShareLink());
    topActions.appendChild(this.elShareBtn);
```

**3d.** In `init()`, add URL token check before `setStatus`:

```typescript
  async init(): Promise<void> {
    this.buildLayout();
    this.report = new RunReport({
      root: this.root,
      document: this.doc,
      onReplay: () => void this.loadWowPreset(),
    });
    await this.loadCatalog();
    this.mountPanel();
    this.rebuildStage();

    const token = parseUrlToken();
    if (token) {
      await this.loadFromShareToken(token);
    } else {
      this.setStatus("ready - press “Load demo” to begin");
    }
  }
```

**3e.** Add `loadFromShareToken` method after `loadWowPreset`:

```typescript
  private async loadFromShareToken(token: ShareToken): Promise<void> {
    try {
      if (this.solverNames.length === 0) await this.loadCatalog();
      this.setStatus("loading shared run…");
      const req =
        token.preset != null
          ? { preset: token.preset }
          : { scenario: token.scenario ?? {} };
      const created = await this.client.createScenario(req);
      this.scenarioId = created.scenario_id;
      this.scenarioLabel = token.preset ?? "shared scenario";
      this.lastSeed = token.seed;
      this.displaySeed = token.seed;
      this.lastActiveSolver = token.solver;
      this.panel.setSolver(token.solver);
      await this.startRuns();
      this.setStatus(`running shared run - solver: ${token.solver}`);
    } catch (e) {
      this.setStatus(`could not load shared run: ${String(e)}`);
    }
  }
```

**3f.** Add `copyShareLink` method after `loadFromShareToken`:

```typescript
  private async copyShareLink(): Promise<void> {
    if (!this.scenarioId) return;
    const state = this.panel.getState();
    const token: ShareToken = {
      v: 1,
      solver: this.lastActiveSolver || state.activeSolver,
      seed: this.displaySeed,
    };
    if (this.scenarioLabel === WOW_PRESET) {
      token.preset = WOW_PRESET;
    } else {
      try {
        const res = await this.client.getScenario(this.scenarioId);
        token.scenario = res.scenario;
      } catch {
        this.setStatus("could not generate share link");
        return;
      }
    }
    try {
      await navigator.clipboard.writeText(shareUrl(token));
      this.setStatus("share link copied to clipboard");
    } catch {
      this.setStatus("clipboard unavailable - open DevTools console for URL");
      console.info("BEAM share URL:", shareUrl(token));
    }
  }
```

**3g.** Enable Share button after a successful run starts. In `startRuns()`, add after `this.panel.setRunning(true)`:

```typescript
    this.elShareBtn.disabled = false;
```

**3h.** Disable Share button on `rebuildStage()`:

Add at the start of `rebuildStage()`:

```typescript
    if (this.elShareBtn) this.elShareBtn.disabled = true;
```

- [ ] **Step 4: Re-export from index**

In `frontend/src/app/index.ts`, add:

```typescript
export type { ShareToken } from "./shareLink";
export { encodeShareToken, decodeShareToken, shareUrl, parseUrlToken } from "./shareLink";
```

- [ ] **Step 5: Run all frontend tests**

```bash
cd frontend && npx vitest run
```

Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add frontend/src/app/app.ts frontend/src/app/index.ts
git commit -m "feat: share button and URL auto-load for run sharing"
```

---

## Self-review checklist

- [x] `encodeShareToken` / `decodeShareToken` round-trips all token shapes
- [x] `parseUrlToken` returns `null` in non-browser context (SSR safe)
- [x] Invalid URL tokens silently fall back (no error modal)
- [x] Share button disabled until a run is active
- [x] Clipboard error falls back to console.info (Safari/HTTP restriction)
- [x] `setSolver` needed by `loadFromShareToken` - implemented in Task 2
- [x] `getScenario` already exists on `BeamRestClient` - no new API methods needed
