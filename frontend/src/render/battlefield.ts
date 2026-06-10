// Pixi.js battlefield renderer (pdd.md 14.1, 14.3, 14.4).
//
// Renders one seeded scenario's live telemetry in a 2.5D tactical perspective:
// the ground plane is tilted away from the camera (depth-squashed projection) so
// the scene reads as a battlespace seen from above-and-behind rather than a flat
// radar. The protected asset sits at the world origin; turret emplacements stand
// on the ground and visibly slew their barrels to aim; drones FLY above the plane
// - each casts a ground shadow and is connected to it by a faint tether, so
// altitude and depth are legible at a glance. Beams lance from elevated turret
// muzzles to the drones with a bright core + bloom; kills throw a particle burst
// and a shock ring; leaks flash the asset red and kick the camera (screen shake).
// A slow camera drift + breath keeps the frozen-on-projector demo feeling alive.
//
// Performance contract (14.4): display objects are pooled per entity id and never
// recreated per frame; only their transform / tint / geometry is mutated. Render
// position is interpolated between the two most recent telemetry frames so motion
// stays smooth at 60fps even when telemetry arrives at a lower cadence (the sim's
// decision period). The render resolution is capped (below) so high-DPI screens do
// not pay 4x fill cost. All wire shapes come from ../types - nothing is redefined
// here.

import {
  Application,
  Container,
  Graphics,
  type ColorSource,
} from "pixi.js";
import type { DroneFrame, FrameMessage, TurretFrame } from "../types";

// --------------------------------------------------------------------------- //
// Palette (14.3: tactical, dark, high-contrast - near-black bg, emerald        //
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
    { maxValue: 5000, color: 0xfbbf24 }, // amber - low value
    { maxValue: Infinity, color: 0xf87171 }, // hotter - high value
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
  /** Depth squash for the 2.5D ground plane (0..1; 1 = flat top-down). Default 0.62. */
  depthSquash?: number;
  /** Disable the idle camera drift/breath (e.g. for deterministic capture). */
  staticCamera?: boolean;
}

// --------------------------------------------------------------------------- //
// Pooled per-entity display objects                                            //
// --------------------------------------------------------------------------- //

/** One drone's pooled display objects. A flying marker above a ground shadow. */
interface DroneSprite {
  root: Container; // positioned at the drone's elevated screen point
  shadow: Graphics; // ground ellipse at the projected ground point
  tether: Graphics; // faint vertical line shadow -> craft (altitude cue)
  glow: Graphics; // soft halo behind the marker
  ring: Graphics; // shrinking kill-progress ring (redrawn only when hp changes)
  heading: Graphics; // thin velocity vector
  marker: Graphics; // value-class colored body
  /** Cached geometry inputs so we only redraw Graphics when they actually change. */
  drawnHp: number;
  drawnTint: number;
  drawnState: string;
  /** Per-drone bob phase so altitude oscillation is desynchronised. */
  phase: number;
}

/** One turret's pooled display objects. Emplacement + barrel that rotates to aim. */
interface TurretSprite {
  root: Container;
  shadowBase: Graphics; // ground footprint ellipse
  base: Graphics; // raised emplacement body
  barrel: Graphics; // redrawn toward the projected aim each frame
  muzzle: Graphics; // firing flash at the barrel tip
  drawnState: string;
}

/** A live particle from a kill burst. Pooled; reset on (re)acquire. */
interface Particle {
  gfx: Graphics;
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number; // seconds remaining
  maxLife: number;
  color: number;
  size: number;
}

/** A frame paired with the wall-clock time it was received, for interpolation. */
interface TimedFrame {
  frame: FrameMessage;
  recvMs: number;
}

/** A queued kill effect: spawn a burst at this world point on the next render. */
interface KillEvent {
  x: number;
  y: number;
  color: number;
}

// --------------------------------------------------------------------------- //
// Renderer                                                                     //
// --------------------------------------------------------------------------- //

const DRONE_MARKER_R = 5;
const DRONE_RING_R = 11;
const HEADING_LEN = 18;
const TURRET_R = 12;
const TURRET_BARREL_LEN = 26;
const TURRET_HEIGHT = 14; // screen px the muzzle sits above the ground footprint
const DRONE_ALT = 34; // screen px a drone flies above its ground shadow
const DRONE_BOB = 4; // +/- screen px of altitude bob
const ASSET_R = 18;
const LEAK_FLASH_MS = 600;
const SHAKE_MS = 380;
const PARTICLES_PER_KILL = 14;
/** Cap render resolution: high-DPI screens otherwise pay 4x+ fill for no visible
 *  gain on this line-art scene, which is a real source of demo lag (esp. in the
 *  two-canvas solver-race view). 1.5 keeps edges crisp without the 4K tax. */
const MAX_RESOLUTION = 1.5;

export class Battlefield {
  readonly app: Application;
  private readonly theme: BattlefieldTheme;
  private readonly opts: Required<
    Omit<BattlefieldOptions, "parent" | "theme" | "framePeriodS">
  > & { framePeriodS?: number };
  private readonly parent: HTMLElement;

  // Display layers (back -> front). All live under `world`, which carries the
  // 2.5D camera transform (drift + shake) about the view centre.
  private readonly world = new Container();
  private readonly gridLayer = new Graphics();
  private readonly assetLayer = new Container();
  private readonly assetGlow = new Graphics(); // pulsing ground glow under asset
  private readonly leakFlashGfx = new Graphics();
  private readonly shadowLayer = new Container(); // all ground shadows (under everything)
  private readonly beamLayer = new Graphics();
  private readonly turretLayer = new Container();
  private readonly droneLayer = new Container();
  private readonly fxLayer = new Container(); // particles + shock rings (topmost)

  // Pools keyed by entity id (never recreated per frame).
  private readonly dronePool = new Map<string, DroneSprite>();
  private readonly turretPool = new Map<string, TurretSprite>();
  /** Free drone sprites available for reuse when a new id appears. */
  private readonly droneFree: DroneSprite[] = [];

  // Interpolation state: two most recent frames + their arrival times.
  private prev: TimedFrame | null = null;
  private curr: TimedFrame | null = null;
  private inferredPeriodMs = 500;

  // View transform: world meters -> screen px. Origin at asset_pos (view centre).
  private scale = 1;
  private viewW = 1;
  private viewH = 1;
  private worldExtent: number;
  private depthSquash: number;
  private cx = 0;
  private cy = 0;

  // Leak flash + camera shake timestamps.
  private lastLeakMs = -Infinity;
  private lastLeakCount = 0;

  // Kill detection + effects.
  private lastKills = 0;
  private readonly lastDronePos = new Map<string, { x: number; y: number; value: number }>();
  private readonly killQueue: KillEvent[] = [];
  private readonly particles: Particle[] = [];
  private readonly fxFree: Graphics[] = [];
  private readonly shockRings: { gfx: Graphics; x: number; y: number; t: number; color: number }[] = [];
  private readonly shockFree: Graphics[] = [];

  private lastRenderMs = 0;
  private ready = false;
  private destroyed = false;
  private resizeObserver: ResizeObserver | null = null;
  private readonly tickFn = () => this.renderInterpolated();

  constructor(options: BattlefieldOptions) {
    this.parent = options.parent;
    this.theme = { ...DEFAULT_THEME, ...(options.theme ?? {}) };
    this.worldExtent = options.worldExtent ?? 5000;
    this.depthSquash = clamp01(options.depthSquash ?? 0.62) || 0.62;
    this.opts = {
      worldExtent: this.worldExtent,
      marginFrac: options.marginFrac ?? 0.06,
      prewarmDrones: options.prewarmDrones ?? 0,
      depthSquash: this.depthSquash,
      staticCamera: options.staticCamera ?? false,
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
    const dpr = globalThis.devicePixelRatio || 1;
    await this.app.init({
      background: this.theme.background,
      width: this.viewW,
      height: this.viewH,
      antialias: true,
      resolution: Math.min(dpr, MAX_RESOLUTION),
      autoDensity: true,
      preference: "webgl",
    });
    this.parent.appendChild(this.app.canvas);

    // Layer order back -> front, all under the camera-transformed `world`.
    this.assetLayer.addChild(this.assetGlow);
    this.assetLayer.addChild(this.leakFlashGfx);
    this.droneLayer.sortableChildren = true; // depth sort flying drones by ground Y
    this.turretLayer.sortableChildren = true;
    this.world.addChild(this.gridLayer);
    this.world.addChild(this.assetLayer);
    this.world.addChild(this.shadowLayer);
    this.world.addChild(this.beamLayer);
    this.world.addChild(this.turretLayer);
    this.world.addChild(this.droneLayer);
    this.world.addChild(this.fxLayer);
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

    // Kill detection: a drone that was live last frame and is gone this frame,
    // while the cumulative kill counter rose, was destroyed -> queue a burst at
    // its last-known position. (Leaked drones also disappear, but those are
    // accounted by the leak counter, not kills, so they don't get a burst.)
    this.detectKills(frame);

    // Grow the visible world extent if any entity sits outside the current fit.
    this.maybeGrowExtent(frame);
  }

  /** Number of pooled (allocated) drone sprites - for tests / diagnostics. */
  get pooledDroneCount(): number {
    return this.dronePool.size + this.droneFree.length;
  }

  /** Number of pooled turret sprites. */
  get pooledTurretCount(): number {
    return this.turretPool.size;
  }

  /** Pause or resume the Pixi ticker (used by 2D/3D toggle). */
  setActive(active: boolean): void {
    if (!this.ready) return;
    if (active) {
      this.app.ticker.start();
    } else {
      this.app.ticker.stop();
    }
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
    this.particles.length = 0;
    this.fxFree.length = 0;
    this.shockRings.length = 0;
    this.shockFree.length = 0;
  }

  // ----------------------------------------------------------------------- //
  // Coordinate transform (2.5D ground projection)                            //
  // ----------------------------------------------------------------------- //

  /** World x (meters) -> screen px on the ground plane. */
  private groundX(x: number): number {
    return this.cx + x * this.scale;
  }
  /** World y (meters) -> screen px on the ground plane (depth-squashed). World
   *  +y is "away/north"; on the tilted plane it both rises on screen and
   *  compresses, which is what reads as depth. */
  private groundY(y: number): number {
    return this.cy - y * this.scale * this.depthSquash;
  }

  private recomputeView(): void {
    this.cx = this.viewW / 2;
    this.cy = this.viewH / 2;
    const margin = Math.min(this.viewW, this.viewH) * this.opts.marginFrac;
    const usable = Math.min(this.viewW, this.viewH) - 2 * margin;
    // Fit a full diameter (2 * extent) into the usable square.
    this.scale = usable > 0 ? usable / (2 * this.worldExtent) : 1;
    // Camera transforms rotate/scale/shake the scene about the view centre.
    this.world.pivot.set(this.cx, this.cy);
    this.world.position.set(this.cx, this.cy);
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
    // Squashed ground footprint (reads as sitting on the tilted plane).
    g.ellipse(0, 0, ASSET_R * 1.5, ASSET_R * 1.5 * this.depthSquash).fill({
      color: this.theme.asset,
      alpha: 0.08,
    });
    // Diamond emplacement for the protected asset, lifted slightly off the plane.
    const lift = ASSET_R * 0.5;
    g.moveTo(0, -ASSET_R - lift)
      .lineTo(ASSET_R, -lift)
      .lineTo(0, ASSET_R - lift)
      .lineTo(-ASSET_R, -lift)
      .closePath()
      .fill({ color: this.theme.asset, alpha: 0.2 })
      .stroke({ color: this.theme.asset, width: 2 });
    g.circle(0, -lift, 3).fill({ color: this.theme.asset });
    this.positionAsset();
  }

  private positionAsset(): void {
    const cx = this.groundX(0);
    const cy = this.groundY(0);
    if (this.assetGfx) this.assetGfx.position.set(cx, cy);
    this.assetGlow.position.set(cx, cy);
    this.leakFlashGfx.position.set(cx, cy);
  }

  private drawGrid(): void {
    const g = this.gridLayer;
    g.clear();
    const cx = this.groundX(0);
    const cy = this.groundY(0);
    // Concentric range rings as depth-squashed ellipses every quarter extent.
    const rings = 4;
    for (let i = 1; i <= rings; i++) {
      const rWorld = (this.worldExtent * i) / rings;
      const rPx = rWorld * this.scale;
      g.ellipse(cx, cy, rPx, rPx * this.depthSquash).stroke({
        color: this.theme.grid,
        width: 1,
        alpha: 0.8,
      });
    }
    // Crosshair along the projected world axes (the +x axis is horizontal; the
    // +y/depth axis is vertical but compressed by the squash).
    const spanX = this.worldExtent * this.scale;
    const spanY = spanX * this.depthSquash;
    g.moveTo(cx - spanX, cy)
      .lineTo(cx + spanX, cy)
      .moveTo(cx, cy - spanY)
      .lineTo(cx, cy + spanY)
      .stroke({ color: this.theme.grid, width: 1, alpha: 0.5 });
  }

  // ----------------------------------------------------------------------- //
  // Per-frame interpolated render                                            //
  // ----------------------------------------------------------------------- //

  private renderInterpolated(): void {
    const now = this.nowMs();
    const dt = this.lastRenderMs ? Math.min(0.1, (now - this.lastRenderMs) / 1000) : 0;
    this.lastRenderMs = now;

    this.updateCamera(now);

    if (!this.curr) {
      // Still animate the asset glow + any lingering FX even before telemetry.
      this.renderAssetGlow(now);
      this.updateParticles(dt);
      this.updateShockRings(dt);
      return;
    }
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

    this.renderAssetGlow(now);
    this.renderTurrets(alpha, now);
    this.renderDrones(alpha, now);
    this.renderBeams(alpha);
    this.spawnQueuedKills();
    this.updateParticles(dt);
    this.updateShockRings(dt);
    this.renderLeakFlash(now);
  }

  /** Slow idle drift + breath, plus a decaying kick on leaks (screen shake). */
  private updateCamera(now: number): void {
    let shx = 0;
    let shy = 0;
    const sinceLeak = now - this.lastLeakMs;
    if (sinceLeak >= 0 && sinceLeak < SHAKE_MS) {
      const k = 1 - sinceLeak / SHAKE_MS;
      const amp = k * 7;
      shx = Math.sin(sinceLeak * 0.09) * amp;
      shy = Math.cos(sinceLeak * 0.13) * amp;
    }
    if (this.opts.staticCamera) {
      this.world.rotation = 0;
      this.world.scale.set(1);
    } else {
      const t = now / 1000;
      this.world.rotation = Math.sin(t * 0.07) * 0.012; // ~0.7deg sway
      const breath = 1 + Math.sin(t * 0.05) * 0.012;
      this.world.scale.set(breath);
    }
    this.world.position.set(this.cx + shx, this.cy + shy);
  }

  /** Pulsing emerald ground glow at the asset - a quiet "defended" heartbeat. */
  private renderAssetGlow(now: number): void {
    const g = this.assetGlow;
    g.clear();
    const pulse = 0.5 + 0.5 * Math.sin(now / 700);
    const r = ASSET_R * (2.4 + pulse * 0.8);
    g.ellipse(0, 0, r, r * this.depthSquash).fill({
      color: this.theme.asset,
      alpha: 0.05 + pulse * 0.05,
    });
  }

  private renderDrones(alpha: number, now: number): void {
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

      const gx = this.groundX(wx);
      const gy = this.groundY(wy);
      const bob = Math.sin(now / 600 + sprite.phase) * DRONE_BOB;
      const flyY = gy - DRONE_ALT - bob;

      // Depth sort: things lower on screen (nearer the camera) draw on top.
      sprite.root.zIndex = gy;
      sprite.root.position.set(gx, flyY);
      sprite.root.visible = true;

      // Ground shadow + tether are children of root, so place them relative to
      // the flying body (shadow sits back down at the ground, +alt+bob below).
      const drop = DRONE_ALT + bob;
      this.updateDroneShadow(sprite, drop);
      this.updateDroneTether(sprite, drop);

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

  private updateDroneShadow(sprite: DroneSprite, drop: number): void {
    // Shadow shrinks/dims a touch with altitude for a subtle depth cue. Cheap
    // enough to redraw each frame (a single ellipse) since `drop` varies.
    const g = sprite.shadow;
    const k = clamp01(1 - (drop - DRONE_ALT + DRONE_BOB) / (DRONE_ALT * 3));
    g.clear();
    g.ellipse(0, drop, DRONE_MARKER_R * 1.6, DRONE_MARKER_R * 1.6 * this.depthSquash).fill({
      color: 0x000000,
      alpha: 0.28 * (0.6 + 0.4 * k),
    });
  }

  private updateDroneTether(sprite: DroneSprite, drop: number): void {
    const g = sprite.tether;
    g.clear();
    g.moveTo(0, 0)
      .lineTo(0, drop)
      .stroke({ color: 0x9fb8b0, width: 1, alpha: 0.18 });
  }

  private updateDroneMarker(sprite: DroneSprite, d: DroneFrame): void {
    const tint = this.droneTint(d);
    if (sprite.drawnTint === tint && sprite.drawnState === d.state) return;
    sprite.drawnTint = tint;
    sprite.drawnState = d.state;
    // Soft glow halo behind the body (drawn once per state/tint change).
    const gl = sprite.glow;
    gl.clear();
    gl.circle(0, 0, DRONE_MARKER_R + 5).fill({ color: tint, alpha: 0.12 });
    gl.circle(0, 0, DRONE_MARKER_R + 2.5).fill({ color: tint, alpha: 0.18 });
    const g = sprite.marker;
    g.clear();
    // Diamond body reads as an aircraft silhouette rather than a dot.
    const r = DRONE_MARKER_R;
    g.moveTo(0, -r)
      .lineTo(r * 0.8, 0)
      .lineTo(0, r)
      .lineTo(-r * 0.8, 0)
      .closePath()
      .fill({ color: tint })
      .stroke({ color: 0xffffff, width: 0.75, alpha: 0.4 });
    // Engaged drones get a brighter core ring to read as "under fire".
    if (d.state === "engaged") {
      g.circle(0, 0, DRONE_MARKER_R + 2).stroke({
        color: this.theme.droneEngaged,
        width: 1.5,
        alpha: 0.95,
      });
    }
  }

  private updateDroneHeading(sprite: DroneSprite, d: DroneFrame): void {
    // Heading vector reflects instantaneous velocity direction; thin line. The
    // y component is depth-squashed so it lies in the ground plane visually.
    const g = sprite.heading;
    const [vx, vy] = d.v;
    const speed = Math.hypot(vx, vy);
    if (speed < 1e-6) {
      g.clear();
      return;
    }
    const ux = vx / speed;
    const uy = -(vy / speed) * this.depthSquash;
    const n = Math.hypot(ux, uy) || 1;
    g.clear();
    g.moveTo(0, 0)
      .lineTo((ux / n) * HEADING_LEN, (uy / n) * HEADING_LEN)
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
        alpha: 0.6,
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

  private renderTurrets(alpha: number, now: number): void {
    const curr = this.curr!.frame;
    const prevById = this.prevTurretIndex();
    const seen = new Set<string>();

    for (const t of curr.turrets) {
      seen.add(t.id);
      const sprite = this.acquireTurret(t.id);
      // Turrets are static emplacements; position from current frame.
      const gx = this.groundX(t.x);
      const gy = this.groundY(t.y);
      sprite.root.zIndex = gy;
      sprite.root.position.set(gx, gy);
      sprite.root.visible = true;

      // Project the aim direction onto the ground plane and draw the barrel from
      // the emplacement toward it (so the slew reads in perspective, not flat).
      const p = prevById.get(t.id);
      const aim = p ? lerpAngle(p.aim, t.aim, alpha) : t.aim;
      this.updateTurretBarrel(sprite, aim, t.state);
      this.updateTurretBody(sprite, t);
      this.updateTurretMuzzle(sprite, t, aim, now);
    }

    for (const [id, sprite] of this.turretPool) {
      if (!seen.has(id)) sprite.root.visible = false;
    }
  }

  private updateTurretBarrel(sprite: TurretSprite, aim: number, state: string): void {
    const g = sprite.barrel;
    g.clear();
    const dx = Math.cos(aim) * TURRET_BARREL_LEN;
    const dy = -Math.sin(aim) * TURRET_BARREL_LEN * this.depthSquash;
    const color =
      state === "firing" ? this.theme.turretFiring : this.theme.turret;
    // Barrel rises from the emplacement top (-TURRET_HEIGHT) toward the aim.
    g.moveTo(0, -TURRET_HEIGHT)
      .lineTo(dx, -TURRET_HEIGHT + dy)
      .stroke({ color, width: 3, alpha: 0.95 });
    g.circle(dx, -TURRET_HEIGHT + dy, 2.5).fill({ color });
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

    // Ground footprint (squashed ellipse) under the raised body.
    const sb = sprite.shadowBase;
    sb.clear();
    sb.ellipse(0, 0, TURRET_R, TURRET_R * this.depthSquash).fill({
      color,
      alpha: 0.16,
    });
    sb.ellipse(0, 0, TURRET_R, TURRET_R * this.depthSquash).stroke({
      color,
      width: 1.5,
      alpha: 0.7,
    });

    // Raised emplacement: a short "drum" between the footprint and its top.
    const g = sprite.base;
    g.clear();
    const rx = TURRET_R * 0.7;
    const ry = rx * this.depthSquash;
    // side walls
    g.moveTo(-rx, 0)
      .lineTo(-rx, -TURRET_HEIGHT)
      .lineTo(rx, -TURRET_HEIGHT)
      .lineTo(rx, 0)
      .closePath()
      .fill({ color, alpha: 0.28 });
    // top cap
    g.ellipse(0, -TURRET_HEIGHT, rx, ry)
      .fill({ color, alpha: 0.5 })
      .stroke({ color, width: 1.5, alpha: 0.9 });
    // Thermal arc on the top cap: amber wedge proportional to thermal_frac.
    const tf = t.thermal_frac < 0 ? 0 : t.thermal_frac > 1 ? 1 : t.thermal_frac;
    if (tf > 0.001) {
      const start = -Math.PI / 2;
      g.arc(0, -TURRET_HEIGHT, rx + 3, start, start + tf * Math.PI * 2).stroke({
        color: 0xfbbf24,
        width: 2,
        alpha: 0.9,
      });
    }
  }

  private updateTurretMuzzle(
    sprite: TurretSprite,
    t: TurretFrame,
    aim: number,
    now: number,
  ): void {
    const g = sprite.muzzle;
    g.clear();
    if (t.state !== "firing") return;
    const dx = Math.cos(aim) * TURRET_BARREL_LEN;
    const dy = -Math.sin(aim) * TURRET_BARREL_LEN * this.depthSquash - TURRET_HEIGHT;
    const flick = 0.6 + 0.4 * Math.sin(now / 40);
    g.circle(dx, dy, 4 + flick * 3).fill({
      color: this.theme.turretFiring,
      alpha: 0.5 * flick,
    });
    g.circle(dx, dy, 2).fill({ color: 0xffffff, alpha: 0.9 });
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
      // Muzzle (elevated) -> drone body (elevated above its ground point).
      const x0 = this.groundX(src.x);
      const y0 = this.groundY(src.y) - TURRET_HEIGHT;
      const x1 = this.groundX(tx);
      const y1 = this.groundY(ty) - DRONE_ALT;
      // Intensity by delivered power: width and alpha both scale with power_frac.
      const pf = b.power_frac < 0 ? 0 : b.power_frac > 1 ? 1 : b.power_frac;
      const width = 1.5 + pf * 4.5;
      const alphaLine = 0.35 + pf * 0.55;
      // Wide outer bloom -> mid -> bright white-hot core.
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({
        color: this.theme.beam,
        width: width + 8,
        alpha: alphaLine * 0.16,
      });
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({
        color: this.theme.beam,
        width: width + 3,
        alpha: alphaLine * 0.4,
      });
      g.moveTo(x0, y0).lineTo(x1, y1).stroke({
        color: 0xffffff,
        width: Math.max(1, width * 0.5),
        alpha: alphaLine,
      });
      // Impact glow at the target.
      g.circle(x1, y1, 4 + pf * 6).fill({
        color: this.theme.beam,
        alpha: alphaLine * 0.5,
      });
      g.circle(x1, y1, 2 + pf * 2).fill({ color: 0xffffff, alpha: alphaLine });
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
    // Expanding, fading red ring at the asset (squashed to the ground plane).
    const k = since / LEAK_FLASH_MS; // 0 -> 1
    const r = ASSET_R + k * ASSET_R * 3;
    const a = (1 - k) * 0.8;
    g.visible = true;
    g.clear();
    g.ellipse(0, 0, r, r * this.depthSquash).stroke({
      color: this.theme.leakFlash,
      width: 3,
      alpha: a,
    });
    g.ellipse(0, 0, ASSET_R, ASSET_R * this.depthSquash).fill({
      color: this.theme.leakFlash,
      alpha: a * 0.35,
    });
  }

  // ----------------------------------------------------------------------- //
  // Kill effects: particle bursts + shock rings                              //
  // ----------------------------------------------------------------------- //

  /** Diff this frame against the last to find drones that were destroyed. */
  private detectKills(frame: FrameMessage): void {
    const present = new Set<string>();
    for (const d of frame.drones) present.add(d.id);

    const deltaKills = Math.max(0, frame.kills - this.lastKills);
    if (deltaKills > 0 && this.lastDronePos.size > 0) {
      let budget = deltaKills;
      for (const [id, p] of this.lastDronePos) {
        if (budget <= 0) break;
        if (!present.has(id)) {
          this.killQueue.push({ x: p.x, y: p.y, color: this.colorForValue(p.value) });
          budget--;
        }
      }
    }
    this.lastKills = frame.kills;

    // Refresh last-known positions for the next diff.
    this.lastDronePos.clear();
    for (const d of frame.drones) {
      this.lastDronePos.set(d.id, { x: d.x, y: d.y, value: d.value });
    }
  }

  private colorForValue(value: number): number {
    for (const band of this.theme.droneValueBands) {
      if (value <= band.maxValue) {
        return typeof band.color === "number" ? band.color : Number(band.color);
      }
    }
    const c = this.theme.droneDefault;
    return typeof c === "number" ? c : Number(c);
  }

  /** Convert any queued kill events into live particles + a shock ring. */
  private spawnQueuedKills(): void {
    if (this.killQueue.length === 0) return;
    for (const k of this.killQueue) {
      const sx = this.groundX(k.x);
      const sy = this.groundY(k.y) - DRONE_ALT;
      this.spawnShockRing(sx, sy, k.color);
      for (let i = 0; i < PARTICLES_PER_KILL; i++) {
        // Deterministic-ish spread (no RNG needed): even fan + speed variation.
        const ang = (i / PARTICLES_PER_KILL) * Math.PI * 2 + i * 0.7;
        const spd = 60 + (i % 5) * 26;
        this.spawnParticle(sx, sy, Math.cos(ang) * spd, -Math.sin(ang) * spd * 0.8, k.color);
      }
    }
    this.killQueue.length = 0;
  }

  private spawnParticle(x: number, y: number, vx: number, vy: number, color: number): void {
    const gfx = this.fxFree.pop() ?? this.newFxGraphics();
    gfx.visible = true;
    this.particles.push({
      gfx,
      x,
      y,
      vx,
      vy,
      life: 0.55,
      maxLife: 0.55,
      color,
      size: 1.6 + (vx % 2 === 0 ? 1.2 : 0),
    });
  }

  private newFxGraphics(): Graphics {
    const g = new Graphics();
    this.fxLayer.addChild(g);
    return g;
  }

  private updateParticles(dt: number): void {
    if (this.particles.length === 0) return;
    for (let i = this.particles.length - 1; i >= 0; i--) {
      const p = this.particles[i];
      p.life -= dt;
      if (p.life <= 0) {
        p.gfx.clear();
        p.gfx.visible = false;
        this.fxFree.push(p.gfx);
        this.particles.splice(i, 1);
        continue;
      }
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      p.vy += 40 * dt; // slight settle so debris arcs downward
      const k = p.life / p.maxLife;
      const g = p.gfx;
      g.clear();
      g.circle(p.x, p.y, p.size * (0.4 + k)).fill({ color: p.color, alpha: k });
      g.circle(p.x, p.y, p.size * 0.5 * (0.4 + k)).fill({ color: 0xffffff, alpha: k * 0.8 });
    }
  }

  private spawnShockRing(x: number, y: number, color: number): void {
    const gfx = this.shockFree.pop() ?? this.newFxGraphics();
    gfx.visible = true;
    this.shockRings.push({ gfx, x, y, t: 0, color });
  }

  private updateShockRings(dt: number): void {
    if (this.shockRings.length === 0) return;
    const DUR = 0.4;
    for (let i = this.shockRings.length - 1; i >= 0; i--) {
      const s = this.shockRings[i];
      s.t += dt;
      if (s.t >= DUR) {
        s.gfx.clear();
        s.gfx.visible = false;
        this.shockFree.push(s.gfx);
        this.shockRings.splice(i, 1);
        continue;
      }
      const k = s.t / DUR; // 0 -> 1
      const r = 6 + k * 34;
      const g = s.gfx;
      g.clear();
      g.circle(s.x, s.y, r).stroke({ color: s.color, width: 2.5 * (1 - k), alpha: 1 - k });
    }
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
    const shadow = new Graphics();
    const tether = new Graphics();
    const glow = new Graphics();
    const ring = new Graphics();
    const heading = new Graphics();
    const marker = new Graphics();
    // Draw order within a drone: shadow + tether (down on the ground) -> glow ->
    // ring -> heading -> marker (front).
    root.addChild(shadow, tether, glow, ring, heading, marker);
    root.visible = false;
    this.droneLayer.addChild(root);
    return {
      root,
      shadow,
      tether,
      glow,
      ring,
      heading,
      marker,
      drawnHp: -1,
      drawnTint: -1,
      drawnState: "",
      phase: this.dronePool.size * 1.7 + this.droneFree.length * 0.9,
    };
  }

  private acquireTurret(id: string): TurretSprite {
    let s = this.turretPool.get(id);
    if (s) return s;
    const root = new Container();
    const shadowBase = new Graphics();
    const base = new Graphics();
    const barrel = new Graphics();
    const muzzle = new Graphics();
    // shadowBase (ground) -> base (raised body) -> barrel -> muzzle flash.
    root.addChild(shadowBase, base, barrel, muzzle);
    this.turretLayer.addChild(root);
    s = { root, shadowBase, base, barrel, muzzle, drawnState: "" };
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

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

/** Shortest-arc angular interpolation (radians), so a turret slews the short way. */
export function lerpAngle(a: number, b: number, t: number): number {
  let d = (b - a) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return a + d * t;
}
