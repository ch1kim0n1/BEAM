// Pixi.js battlefield renderer (pdd.md 14.1, 14.3, 14.4).
//
// Renders one seeded scenario's live telemetry: the protected asset at the world
// origin, turret emplacements that visibly rotate to their aim, beams drawn
// turret -> target with intensity scaled by delivered power, and drones colored by
// value class with a shrinking kill-progress ring and a thin heading vector. Leaks
// flash red at the asset.
//
// Performance contract (14.4): display objects are pooled per entity id and never
// recreated per frame; only their transform / tint / geometry is mutated. Render
// position is interpolated between the two most recent telemetry frames so motion
// stays smooth at 60fps even when telemetry arrives at a lower cadence (the sim's
// decision period). All wire shapes come from ../types — nothing is redefined here.

import {
  Application,
  Container,
  Graphics,
  type ColorSource,
} from "pixi.js";
import type { DroneFrame, FrameMessage, TurretFrame } from "../types";

// --------------------------------------------------------------------------- //
// Palette (14.3: tactical, dark, high-contrast — near-black bg, emerald        //
// friendly/turret/beam, amber threat urgency, red leaks).                      //
// --------------------------------------------------------------------------- //

export interface BattlefieldTheme {
  background: ColorSource;
  grid: ColorSource;
  asset: ColorSource;
  turret: ColorSource;
  turretFiring: ColorSource;
  turretCooldown: ColorSource;
  beam: ColorSource;
  leakFlash: ColorSource;
  /** Drone marker tint by value band. The wire `DroneFrame` carries `value`
   *  (not the class name), so high-value threats are distinguished by value:
   *  each band's `maxValue` is an inclusive upper bound, evaluated in order, and
   *  the first match wins. A final band with `maxValue: Infinity` is the catch-all.
   *  This keeps the renderer free of hardcoded class names while still reading
   *  "amber = cheap, hotter = expensive" per pdd.md 14.3. */
  droneValueBands: Array<{ maxValue: number; color: ColorSource }>;
  droneDefault: ColorSource;
  droneEngaged: ColorSource;
  killRing: ColorSource;
  heading: ColorSource;
}

export const DEFAULT_THEME: BattlefieldTheme = {
  background: 0x05080a,
  grid: 0x0e1c1a,
  asset: 0x34d399, // emerald
  turret: 0x10b981,
  turretFiring: 0x6ee7b7,
  turretCooldown: 0x0f766e,
  beam: 0x6ee7b7,
  leakFlash: 0xef4444, // red
  // Value bands (illustrative; the host app may pass exact config-derived bounds).
  // Cheap quads read amber, expensive fixed-wing read a hotter orange-red.
  droneValueBands: [
    { maxValue: 5000, color: 0xfbbf24 }, // amber — low value
    { maxValue: Infinity, color: 0xf87171 }, // hotter — high value
  ],
  droneDefault: 0xfbbf24,
  droneEngaged: 0xfde68a,
  killRing: 0x6ee7b7,
  heading: 0xe5e7eb,
};

export interface BattlefieldOptions {
  /** Container element the canvas is appended to. Sized to it and on resize. */
  parent: HTMLElement;
  theme?: Partial<BattlefieldTheme>;
  /** World half-extent (meters) initially fitted into view. Auto-grows to keep
   *  every entity on screen. Default 5000 (matches turret_defaults.range_max). */
  worldExtent?: number;
  /** Fraction of the smaller viewport dimension left as margin. Default 0.06. */
  marginFrac?: number;
  /** Expected telemetry cadence (s); the interpolation window. When omitted it is
   *  inferred from inter-frame arrival deltas. Maps to scenario.decision_period. */
  framePeriodS?: number;
  /** Pre-allocate this many drone slots in the pool. Default 0 (grows on demand). */
  prewarmDrones?: number;
}

// --------------------------------------------------------------------------- //
// Pooled per-entity display objects                                            //
// --------------------------------------------------------------------------- //

/** One drone's pooled display objects. Marker + kill-progress ring + heading. */
interface DroneSprite {
  root: Container;
  marker: Graphics; // value-class colored body
  ring: Graphics; // shrinking kill-progress ring (redrawn only when hp changes)
  heading: Graphics; // thin velocity vector
  /** Cached geometry inputs so we only redraw Graphics when they actually change. */
  drawnHp: number;
  drawnTint: number;
  drawnState: string;
}

/** One turret's pooled display objects. Body + barrel that rotates to aim. */
interface TurretSprite {
  root: Container;
  base: Graphics;
  barrel: Graphics; // rotated to `aim`
  drawnState: string;
}

/** A frame paired with the wall-clock time it was received, for interpolation. */
interface TimedFrame {
  frame: FrameMessage;
  recvMs: number;
}

// --------------------------------------------------------------------------- //
// Renderer                                                                     //
// --------------------------------------------------------------------------- //

const DRONE_MARKER_R = 5;
const DRONE_RING_R = 11;
const HEADING_LEN = 18;
const TURRET_R = 13;
const TURRET_BARREL_LEN = 22;
const ASSET_R = 18;
const LEAK_FLASH_MS = 600;

export class Battlefield {
  readonly app: Application;
  private readonly theme: BattlefieldTheme;
  private readonly opts: Required<
    Omit<BattlefieldOptions, "parent" | "theme" | "framePeriodS">
  > & { framePeriodS?: number };
  private readonly parent: HTMLElement;

  // Display layers (back -> front).
  private readonly world = new Container();
  private readonly gridLayer = new Graphics();
  private readonly assetLayer = new Container();
  private readonly leakFlashGfx = new Graphics();
  private readonly beamLayer = new Graphics();
  private readonly turretLayer = new Container();
  private readonly droneLayer = new Container();

  // Pools keyed by entity id (never recreated per frame).
  private readonly dronePool = new Map<string, DroneSprite>();
  private readonly turretPool = new Map<string, TurretSprite>();
  /** Free drone sprites available for reuse when a new id appears. */
  private readonly droneFree: DroneSprite[] = [];

  // Interpolation state: two most recent frames + their arrival times.
  private prev: TimedFrame | null = null;
  private curr: TimedFrame | null = null;
  private inferredPeriodMs = 500;

  // View transform: world meters -> screen px. Origin at asset_pos.
  private scale = 1;
  private viewW = 1;
  private viewH = 1;
  private worldExtent: number;

  // Leak flash: timestamp of the most recent leak observed.
  private lastLeakMs = -Infinity;
  private lastLeakCount = 0;

  private ready = false;
  private destroyed = false;
  private resizeObserver: ResizeObserver | null = null;
  private readonly tickFn = () => this.renderInterpolated();

  constructor(options: BattlefieldOptions) {
    this.parent = options.parent;
    this.theme = { ...DEFAULT_THEME, ...(options.theme ?? {}) };
    this.worldExtent = options.worldExtent ?? 5000;
    this.opts = {
      worldExtent: this.worldExtent,
      marginFrac: options.marginFrac ?? 0.06,
      prewarmDrones: options.prewarmDrones ?? 0,
      framePeriodS: options.framePeriodS,
    };
    if (options.framePeriodS && options.framePeriodS > 0) {
      this.inferredPeriodMs = options.framePeriodS * 1000;
    }
    this.app = new Application();
  }

  /** Initialize the Pixi application and attach the canvas. Must be awaited once
   *  before pushing frames. Idempotent. */
  async init(): Promise<void> {
    if (this.ready || this.destroyed) return;
    const { clientWidth, clientHeight } = this.parent;
    this.viewW = Math.max(1, clientWidth || 800);
    this.viewH = Math.max(1, clientHeight || 600);
    await this.app.init({
      background: this.theme.background,
      width: this.viewW,
      height: this.viewH,
      antialias: true,
      resolution: globalThis.devicePixelRatio || 1,
      autoDensity: true,
      preference: "webgl",
    });
    this.parent.appendChild(this.app.canvas);

    // Layer order back -> front.
    this.world.addChild(this.gridLayer);
    this.assetLayer.addChild(this.leakFlashGfx);
    this.world.addChild(this.beamLayer);
    this.world.addChild(this.assetLayer);
    this.world.addChild(this.turretLayer);
    this.world.addChild(this.droneLayer);
    this.app.stage.addChild(this.world);

    this.drawAsset();
    this.recomputeView();
    this.drawGrid();
    for (let i = 0; i < this.opts.prewarmDrones; i++) {
      this.droneFree.push(this.createDroneSprite());
    }

    if (typeof ResizeObserver !== "undefined") {
      this.resizeObserver = new ResizeObserver(() => this.handleResize());
      this.resizeObserver.observe(this.parent);
    }
    this.app.ticker.add(this.tickFn);
    this.ready = true;
  }

  /** Push a validated telemetry frame. Cheap: stores it for interpolation and
   *  performs the heavy pool reconciliation on the next render tick. */
  pushFrame(frame: FrameMessage): void {
    const now = this.nowMs();
    if (this.curr) {
      // Track inter-arrival cadence unless an explicit period was supplied.
      if (!this.opts.framePeriodS) {
        const dt = now - this.curr.recvMs;
        if (dt > 1 && dt < 5000) {
          // Light EMA so a single stutter doesn't whipsaw interpolation.
          this.inferredPeriodMs = this.inferredPeriodMs * 0.7 + dt * 0.3;
        }
      }
      this.prev = this.curr;
    }
    this.curr = { frame, recvMs: now };

    // Leak flash trigger: cumulative leak count rose since the last frame.
    if (frame.leaks > this.lastLeakCount) {
      this.lastLeakMs = now;
    }
    this.lastLeakCount = frame.leaks;

    // Grow the visible world extent if any entity sits outside the current fit.
    this.maybeGrowExtent(frame);
  }

  /** Number of pooled (allocated) drone sprites — for tests / diagnostics. */
  get pooledDroneCount(): number {
    return this.dronePool.size + this.droneFree.length;
  }

  /** Number of pooled turret sprites. */
  get pooledTurretCount(): number {
    return this.turretPool.size;
  }

  /** Tear down Pixi resources and observers. */
  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    if (this.ready) {
      this.app.ticker.remove(this.tickFn);
      this.app.destroy(true, { children: true });
    }
    this.dronePool.clear();
    this.turretPool.clear();
    this.droneFree.length = 0;
  }

  // ----------------------------------------------------------------------- //
  // Coordinate transform                                                     //
  // ----------------------------------------------------------------------- //

  /** World (meters, origin = asset) -> screen px (origin = canvas center). */
  private worldToScreenX(x: number): number {
    return this.viewW / 2 + x * this.scale;
  }
  private worldToScreenY(y: number): number {
    // Flip Y: world +y is "up", screen +y is down.
    return this.viewH / 2 - y * this.scale;
  }

  private recomputeView(): void {
    const margin = Math.min(this.viewW, this.viewH) * this.opts.marginFrac;
    const usable = Math.min(this.viewW, this.viewH) - 2 * margin;
    // Fit a full diameter (2 * extent) into the usable square.
    this.scale = usable > 0 ? usable / (2 * this.worldExtent) : 1;
  }

  private maybeGrowExtent(frame: FrameMessage): void {
    let maxR = this.worldExtent;
    for (const d of frame.drones) {
      const r = Math.hypot(d.x, d.y);
      if (r > maxR) maxR = r;
    }
    for (const t of frame.turrets) {
      const r = Math.hypot(t.x, t.y);
      if (r > maxR) maxR = r;
    }
    // Add 8% headroom so a drone at the edge isn't clipped, and only ever grow.
    const target = maxR * 1.08;
    if (target > this.worldExtent) {
      this.worldExtent = target;
      this.recomputeView();
      this.drawGrid();
    }
  }

  private handleResize(): void {
    if (!this.ready || this.destroyed) return;
    const w = Math.max(1, this.parent.clientWidth);
    const h = Math.max(1, this.parent.clientHeight);
    if (w === this.viewW && h === this.viewH) return;
    this.viewW = w;
    this.viewH = h;
    this.app.renderer.resize(w, h);
    this.recomputeView();
    this.drawGrid();
    this.positionAsset();
  }

  // ----------------------------------------------------------------------- //
  // Static / semi-static drawing                                             //
  // ----------------------------------------------------------------------- //

  private assetGfx: Graphics | null = null;

  private drawAsset(): void {
    if (!this.assetGfx) {
      this.assetGfx = new Graphics();
      this.assetLayer.addChild(this.assetGfx);
    }
    const g = this.assetGfx;
    g.clear();
    // Diamond emplacement for the protected asset.
    g.moveTo(0, -ASSET_R)
      .lineTo(ASSET_R, 0)
      .lineTo(0, ASSET_R)
      .lineTo(-ASSET_R, 0)
      .closePath()
      .fill({ color: this.theme.asset, alpha: 0.18 })
      .stroke({ color: this.theme.asset, width: 2 });
    g.circle(0, 0, 3).fill({ color: this.theme.asset });
    this.positionAsset();
  }

  private positionAsset(): void {
    const cx = this.worldToScreenX(0);
    const cy = this.worldToScreenY(0);
    if (this.assetGfx) this.assetGfx.position.set(cx, cy);
    this.leakFlashGfx.position.set(cx, cy);
  }

  private drawGrid(): void {
    const g = this.gridLayer;
    g.clear();
    // Concentric range rings every quarter of the world extent, plus crosshair.
    const rings = 4;
    for (let i = 1; i <= rings; i++) {
      const rWorld = (this.worldExtent * i) / rings;
      const rPx = rWorld * this.scale;
      g.circle(this.worldToScreenX(0), this.worldToScreenY(0), rPx).stroke({
        color: this.theme.grid,
        width: 1,
        alpha: 0.8,
      });
    }
    const cx = this.worldToScreenX(0);
    const cy = this.worldToScreenY(0);
    const span = this.worldExtent * this.scale;
    g.moveTo(cx - span, cy)
      .lineTo(cx + span, cy)
      .moveTo(cx, cy - span)
      .lineTo(cx, cy + span)
      .stroke({ color: this.theme.grid, width: 1, alpha: 0.5 });
  }

  // ----------------------------------------------------------------------- //
  // Per-frame interpolated render                                            //
  // ----------------------------------------------------------------------- //

  private renderInterpolated(): void {
    if (!this.curr) return;
    const now = this.nowMs();
    // Interpolation factor in [0,1]: how far we are between prev and curr,
    // measured against the (inferred or configured) frame period.
    let alpha = 1;
    if (this.prev) {
      const span = Math.max(1, this.inferredPeriodMs);
      alpha = (now - this.curr.recvMs) / span;
      // Clamp: before curr we lerp prev->curr; once we pass the period we hold at
      // curr (extrapolation would drift drones through their target).
      alpha = alpha < 0 ? 0 : alpha > 1 ? 1 : alpha;
    }

    this.renderDrones(alpha);
    this.renderTurrets(alpha);
    this.renderBeams(alpha);
    this.renderLeakFlash(now);
  }

  private renderDrones(alpha: number): void {
    const curr = this.curr!.frame;
    const prevById = this.prevDroneIndex();
    const seen = new Set<string>();

    for (const d of curr.drones) {
      // Dead / leaked drones should not be drawn as live markers.
      if (d.state === "dead" || d.state === "leaked") continue;
      seen.add(d.id);
      const sprite = this.acquireDrone(d.id);

      // Interpolated position: lerp prev->curr when we have a prior sample,
      // otherwise dead-reckon from velocity over the elapsed fraction.
      let wx = d.x;
      let wy = d.y;
      const p = prevById.get(d.id);
      if (p) {
        wx = p.x + (d.x - p.x) * alpha;
        wy = p.y + (d.y - p.y) * alpha;
      } else {
        // New drone (no prior frame): extrapolate along velocity for smoothness.
        const dt = (this.inferredPeriodMs / 1000) * alpha;
        wx = d.x + d.v[0] * dt;
        wy = d.y + d.v[1] * dt;
      }
      sprite.root.position.set(this.worldToScreenX(wx), this.worldToScreenY(wy));
      sprite.root.visible = true;

      this.updateDroneMarker(sprite, d);
      this.updateDroneHeading(sprite, d);
      this.updateDroneRing(sprite, d);
    }

    // Release sprites whose drones are no longer present/live.
    for (const [id, sprite] of this.dronePool) {
      if (!seen.has(id)) {
        sprite.root.visible = false;
        this.dronePool.delete(id);
        this.droneFree.push(sprite);
      }
    }
  }

  private updateDroneMarker(sprite: DroneSprite, d: DroneFrame): void {
    const tint = this.droneTint(d);
    if (sprite.drawnTint === tint && sprite.drawnState === d.state) return;
    sprite.drawnTint = tint;
    sprite.drawnState = d.state;
    const g = sprite.marker;
    g.clear();
    // Engaged drones get a brighter core ring to read as "under fire".
    g.circle(0, 0, DRONE_MARKER_R).fill({ color: tint });
    if (d.state === "engaged") {
      g.circle(0, 0, DRONE_MARKER_R + 2).stroke({
        color: this.theme.droneEngaged,
        width: 1.5,
        alpha: 0.9,
      });
    }
  }

  private updateDroneHeading(sprite: DroneSprite, d: DroneFrame): void {
    // Heading vector reflects instantaneous velocity direction; thin line.
    const g = sprite.heading;
    const [vx, vy] = d.v;
    const speed = Math.hypot(vx, vy);
    if (speed < 1e-6) {
      g.clear();
      return;
    }
    // Direction in screen space (flip y). Scale length by a soft cap so fast and
    // slow drones both read clearly.
    const ux = vx / speed;
    const uy = -vy / speed;
    g.clear();
    g.moveTo(0, 0)
      .lineTo(ux * HEADING_LEN, uy * HEADING_LEN)
      .stroke({ color: this.theme.heading, width: 1, alpha: 0.8 });
  }

  private updateDroneRing(sprite: DroneSprite, d: DroneFrame): void {
    // Shrinking kill-progress ring: full circle when undamaged, shrinking arc as
    // energy is absorbed (hp_frac -> 0). hp_frac is already clamped server-side.
    const hp = d.hp_frac < 0 ? 0 : d.hp_frac > 1 ? 1 : d.hp_frac;
    if (Math.abs(sprite.drawnHp - hp) < 0.01) return;
    sprite.drawnHp = hp;
    const g = sprite.ring;
    g.clear();
    if (hp >= 0.999) {
      g.circle(0, 0, DRONE_RING_R).stroke({
        color: this.theme.killRing,
        width: 2,
        alpha: 0.7,
      });
      return;
    }
    // Arc from top, clockwise, spanning the remaining hp fraction.
    const start = -Math.PI / 2;
    const end = start + hp * Math.PI * 2;
    g.arc(0, 0, DRONE_RING_R, start, end).stroke({
      color: this.theme.killRing,
      width: 2.5,
      alpha: 0.95,
    });
  }

  private droneTint(d: DroneFrame): number {
    for (const band of this.theme.droneValueBands) {
      if (d.value <= band.maxValue) {
        return typeof band.color === "number" ? band.color : Number(band.color);
      }
    }
    const c = this.theme.droneDefault;
    return typeof c === "number" ? c : Number(c);
  }

  private renderTurrets(alpha: number): void {
    const curr = this.curr!.frame;
    const prevById = this.prevTurretIndex();
    const seen = new Set<string>();

    for (const t of curr.turrets) {
      seen.add(t.id);
      const sprite = this.acquireTurret(t.id);
      // Turrets are static emplacements; position from current frame.
      sprite.root.position.set(
        this.worldToScreenX(t.x),
        this.worldToScreenY(t.y),
      );
      sprite.root.visible = true;

      // Visibly rotate barrel to aim, interpolating the angle so slewing reads
      // as continuous motion. Aim is a world-frame angle (atan2 over world axes);
      // convert to screen by negating (y is flipped).
      const p = prevById.get(t.id);
      const aim = p ? lerpAngle(p.aim, t.aim, alpha) : t.aim;
      sprite.barrel.rotation = -aim;

      this.updateTurretBody(sprite, t);
    }

    for (const [id, sprite] of this.turretPool) {
      if (!seen.has(id)) sprite.root.visible = false;
    }
  }

  private updateTurretBody(sprite: TurretSprite, t: TurretFrame): void {
    if (sprite.drawnState === t.state) return;
    sprite.drawnState = t.state;
    const color =
      t.state === "firing"
        ? this.theme.turretFiring
        : t.state === "cooldown"
          ? this.theme.turretCooldown
          : this.theme.turret;
    const g = sprite.base;
    g.clear();
    g.circle(0, 0, TURRET_R)
      .fill({ color, alpha: 0.22 })
      .stroke({ color, width: 2 });
    // Thermal arc on the base: amber wedge proportional to thermal_frac.
    const tf = t.thermal_frac < 0 ? 0 : t.thermal_frac > 1 ? 1 : t.thermal_frac;
    if (tf > 0.001) {
      const start = -Math.PI / 2;
      g.arc(0, 0, TURRET_R + 4, start, start + tf * Math.PI * 2).stroke({
        color: 0xfbbf24,
        width: 2,
        alpha: 0.9,
      });
    }
  }

  private renderBeams(alpha: number): void {
    const curr = this.curr!.frame;
    const g = this.beamLayer;
    g.clear();
    if (curr.beams.length === 0) return;

    const turretById = this.turretFrameIndex(curr.turrets);
    const droneCurr = this.droneFrameIndex(curr.drones);
    const dronePrev = this.prevDroneIndex();

    for (const b of curr.beams) {
      const src = turretById.get(b.fromTurret) ?? turretById.get(b.from);
      const dstC = droneCurr.get(b.to);
      if (!src || !dstC) continue;
      // Beam endpoint tracks the interpolated drone position so it stays glued to
      // the moving target rather than snapping each telemetry frame.
      const dp = dronePrev.get(b.to);
      let tx = dstC.x;
      let ty = dstC.y;
      if (dp) {
        tx = dp.x + (dstC.x - dp.x) * alpha;
        ty = dp.y + (dstC.y - dp.y) * alpha;
      }
      const x0 = this.worldToScreenX(src.x);
      const y0 = this.worldToScreenY(src.y);
      const x1 = this.worldToScreenX(tx);
      const y1 = this.worldToScreenY(ty);
      // Intensity by delivered power: width and alpha both scale with power_frac.
      const pf = b.power_frac < 0 ? 0 : b.power_frac > 1 ? 1 : b.power_frac;
      const width = 1.5 + pf * 4.5;
      const alphaLine = 0.35 + pf * 0.55;
      // Outer glow then bright core.
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({
        color: this.theme.beam,
        width: width + 4,
        alpha: alphaLine * 0.3,
      });
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({
        color: this.theme.beam,
        width,
        alpha: alphaLine,
      });
      // Impact glow at the target.
      g.circle(x1, y1, 3 + pf * 4).fill({
        color: this.theme.beam,
        alpha: alphaLine,
      });
    }
  }

  private renderLeakFlash(now: number): void {
    const g = this.leakFlashGfx;
    const since = now - this.lastLeakMs;
    if (since > LEAK_FLASH_MS) {
      if (g.visible) {
        g.clear();
        g.visible = false;
      }
      return;
    }
    // Expanding, fading red ring at the asset.
    const k = since / LEAK_FLASH_MS; // 0 -> 1
    const r = ASSET_R + k * ASSET_R * 3;
    const a = (1 - k) * 0.8;
    g.visible = true;
    g.clear();
    g.circle(0, 0, r).stroke({
      color: this.theme.leakFlash,
      width: 3,
      alpha: a,
    });
    g.circle(0, 0, ASSET_R).fill({
      color: this.theme.leakFlash,
      alpha: a * 0.35,
    });
  }

  // ----------------------------------------------------------------------- //
  // Pool management                                                          //
  // ----------------------------------------------------------------------- //

  private acquireDrone(id: string): DroneSprite {
    let s = this.dronePool.get(id);
    if (s) return s;
    s = this.droneFree.pop() ?? this.createDroneSprite();
    // Force a redraw for the reused sprite under its new identity.
    s.drawnHp = -1;
    s.drawnTint = -1;
    s.drawnState = "";
    this.dronePool.set(id, s);
    return s;
  }

  private createDroneSprite(): DroneSprite {
    const root = new Container();
    const ring = new Graphics();
    const heading = new Graphics();
    const marker = new Graphics();
    // Draw order within a drone: ring (back) -> heading -> marker (front).
    root.addChild(ring, heading, marker);
    root.visible = false;
    this.droneLayer.addChild(root);
    return { root, marker, ring, heading, drawnHp: -1, drawnTint: -1, drawnState: "" };
  }

  private acquireTurret(id: string): TurretSprite {
    let s = this.turretPool.get(id);
    if (s) return s;
    const root = new Container();
    const base = new Graphics();
    const barrel = new Graphics();
    // Barrel as a fixed bar pointing +x (world 0 rad); rotated each frame.
    barrel
      .rect(0, -2, TURRET_BARREL_LEN, 4)
      .fill({ color: this.theme.turret })
      .circle(TURRET_BARREL_LEN, 0, 3)
      .fill({ color: this.theme.turretFiring });
    root.addChild(base, barrel);
    this.turretLayer.addChild(root);
    s = { root, base, barrel, drawnState: "" };
    this.turretPool.set(id, s);
    return s;
  }

  // ----------------------------------------------------------------------- //
  // Indexing helpers                                                         //
  // ----------------------------------------------------------------------- //

  private prevDroneIndex(): Map<string, DroneFrame> {
    return this.prev
      ? this.droneFrameIndex(this.prev.frame.drones)
      : EMPTY_DRONE_INDEX;
  }
  private prevTurretIndex(): Map<string, TurretFrame> {
    return this.prev
      ? this.turretFrameIndex(this.prev.frame.turrets)
      : EMPTY_TURRET_INDEX;
  }
  private droneFrameIndex(list: DroneFrame[]): Map<string, DroneFrame> {
    const m = new Map<string, DroneFrame>();
    for (const d of list) m.set(d.id, d);
    return m;
  }
  private turretFrameIndex(list: TurretFrame[]): Map<string, TurretFrame> {
    const m = new Map<string, TurretFrame>();
    for (const t of list) m.set(t.id, t);
    return m;
  }

  private nowMs(): number {
    return typeof performance !== "undefined" ? performance.now() : Date.now();
  }
}

const EMPTY_DRONE_INDEX: Map<string, DroneFrame> = new Map();
const EMPTY_TURRET_INDEX: Map<string, TurretFrame> = new Map();

/** Shortest-arc angular interpolation (radians), so a turret slews the short way. */
export function lerpAngle(a: number, b: number, t: number): number {
  let d = (b - a) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return a + d * t;
}
