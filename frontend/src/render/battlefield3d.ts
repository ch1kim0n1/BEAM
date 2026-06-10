// Battlefield3D - Three.js 3D renderer for the BEAM battlefield.
//
// Mirrors the same interface as Battlefield (init, pushFrame, destroy, setActive)
// so RunController can hold both and forward telemetry to each.
//
// Drone altitude is computed from TTI: drones start at MAX_ALTITUDE and descend
// linearly toward 0 as they approach the asset. No backend schema changes needed.

import * as THREE from "three";
import type { FrameMessage } from "../types";

const MAX_ALTITUDE = 200; // metres (world units; SCALE maps them to scene units)
const TTI_SCALE = 60;     // TTI cap for altitude mapping (s)
const SCALE = 0.05;       // world-metres -> scene units

function ttiToAltitude(tti: number): number {
  return Math.min(tti / TTI_SCALE, 1.0) * MAX_ALTITUDE * SCALE;
}

export interface Battlefield3DOptions {
  parent: HTMLElement;
}

export class Battlefield3D {
  private readonly parent: HTMLElement;
  private renderer: THREE.WebGLRenderer | null = null;
  private scene: THREE.Scene | null = null;
  private camera: THREE.PerspectiveCamera | null = null;
  private rafId: number | null = null;
  private active = false;

  private dronePool: Map<string, THREE.Mesh> = new Map();
  private turretPool: Map<string, { base: THREE.Mesh; barrel: THREE.Mesh }> = new Map();
  private beamLines: Map<string, THREE.Line> = new Map();

  private droneGeo!: THREE.ConeGeometry;
  private turretBaseGeo!: THREE.BoxGeometry;
  private turretBarrelGeo!: THREE.CylinderGeometry;

  constructor(opts: Battlefield3DOptions) {
    this.parent = opts.parent;
  }

  async init(): Promise<void> {
    if (this.renderer) return;

    const w = this.parent.clientWidth || 600;
    const h = this.parent.clientHeight || 400;

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.parent.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x0a0e14);

    this.camera = new THREE.PerspectiveCamera(50, w / h, 1, 100000);
    this.camera.position.set(0, 600, 400);
    this.camera.lookAt(0, 0, 0);

    // Lights
    this.scene.add(new THREE.AmbientLight(0x334455, 3));
    const sun = new THREE.DirectionalLight(0xffffff, 2);
    sun.position.set(200, 400, 200);
    this.scene.add(sun);

    // Grid
    const grid = new THREE.GridHelper(4000, 50, 0x1a2030, 0x1a2030);
    this.scene.add(grid);

    // Asset (defended point)
    const assetGeo = new THREE.CylinderGeometry(10, 10, 25, 16);
    const assetMat = new THREE.MeshStandardMaterial({ color: 0x00ff88, emissive: 0x003311 });
    this.scene.add(new THREE.Mesh(assetGeo, assetMat));

    // Shared geometries
    this.droneGeo = new THREE.ConeGeometry(4, 12, 6);
    this.turretBaseGeo = new THREE.BoxGeometry(14, 8, 14);
    this.turretBarrelGeo = new THREE.CylinderGeometry(2, 2, 28, 8);

    // Lazy-load OrbitControls (not available in test env)
    try {
      const { OrbitControls } = await import("three/addons/controls/OrbitControls.js" as never as string) as { OrbitControls: new (c: THREE.Camera, e: HTMLElement) => { update(): void; dispose(): void } };
      const controls = new OrbitControls(this.camera, this.renderer.domElement);
      (this as unknown as { _controls: typeof controls })._controls = controls;
    } catch {
      // OrbitControls not available (test environment)
    }
  }

  setActive(active: boolean): void {
    this.active = active;
    if (active) {
      this.startLoop();
    } else {
      this.stopLoop();
    }
  }

  pushFrame(msg: FrameMessage): void {
    if (!this.scene) return;
    this._syncDrones(msg);
    this._syncTurrets(msg);
    this._syncBeams(msg);
  }

  private _syncDrones(msg: FrameMessage): void {
    const seen = new Set<string>();
    for (const d of msg.drones) {
      if (d.state === "dead" || d.state === "leaked") continue;
      seen.add(d.id);
      let mesh = this.dronePool.get(d.id);
      if (!mesh) {
        const color = d.value > 5000 ? 0xffaa00 : 0xcccccc;
        mesh = new THREE.Mesh(
          this.droneGeo,
          new THREE.MeshStandardMaterial({ color, transparent: true }),
        );
        this.scene!.add(mesh);
        this.dronePool.set(d.id, mesh);
      }
      mesh.position.set(d.x * SCALE, ttiToAltitude(d.tti), d.y * SCALE);
      (mesh.material as THREE.MeshStandardMaterial).opacity = 0.4 + d.hp_frac * 0.6;
    }
    for (const [id, mesh] of this.dronePool) {
      if (!seen.has(id)) {
        this.scene!.remove(mesh);
        this.dronePool.delete(id);
      }
    }
  }

  private _syncTurrets(msg: FrameMessage): void {
    const seen = new Set<string>();
    for (const t of msg.turrets) {
      seen.add(t.id);
      let g = this.turretPool.get(t.id);
      if (!g) {
        const base = new THREE.Mesh(
          this.turretBaseGeo,
          new THREE.MeshStandardMaterial({ color: 0x334455 }),
        );
        const barrel = new THREE.Mesh(
          this.turretBarrelGeo,
          new THREE.MeshStandardMaterial({ color: 0x00ff88 }),
        );
        this.scene!.add(base);
        this.scene!.add(barrel);
        g = { base, barrel };
        this.turretPool.set(t.id, g);
      }
      g.base.position.set(t.x * SCALE, 4, t.y * SCALE);
      const aim = t.aim;
      g.barrel.position.set(
        t.x * SCALE + Math.cos(aim) * 16 * SCALE,
        8,
        t.y * SCALE + Math.sin(aim) * 16 * SCALE,
      );
      g.barrel.rotation.y = -aim;
    }
    for (const [id, g] of this.turretPool) {
      if (!seen.has(id)) {
        this.scene!.remove(g.base);
        this.scene!.remove(g.barrel);
        this.turretPool.delete(id);
      }
    }
  }

  private _syncBeams(msg: FrameMessage): void {
    const seen = new Set<string>();
    const dMap = new Map(msg.drones.map((d) => [d.id, d]));
    const tMap = new Map(msg.turrets.map((t) => [t.id, t]));

    for (const b of msg.beams) {
      const key = `${b.fromTurret}-${b.to}`;
      seen.add(key);
      const turret = tMap.get(b.fromTurret);
      const drone = dMap.get(b.to);
      if (!turret || !drone) continue;

      let line = this.beamLines.get(key);
      if (!line) {
        const geo = new THREE.BufferGeometry();
        line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0x00ff88 }));
        this.scene!.add(line);
        this.beamLines.set(key, line);
      }
      const pts = [
        new THREE.Vector3(turret.x * SCALE, 8, turret.y * SCALE),
        new THREE.Vector3(drone.x * SCALE, ttiToAltitude(drone.tti), drone.y * SCALE),
      ];
      (line.geometry as THREE.BufferGeometry).setFromPoints(pts);
      (line.material as THREE.LineBasicMaterial).opacity = b.power_frac;
    }
    for (const [key, line] of this.beamLines) {
      if (!seen.has(key)) {
        this.scene!.remove(line);
        this.beamLines.delete(key);
      }
    }
  }

  private startLoop(): void {
    if (this.rafId !== null) return;
    const controls = (this as unknown as { _controls?: { update(): void } })._controls;
    const tick = () => {
      controls?.update();
      if (this.renderer && this.scene && this.camera) {
        this.renderer.render(this.scene, this.camera);
      }
      this.rafId = requestAnimationFrame(tick);
    };
    this.rafId = requestAnimationFrame(tick);
  }

  private stopLoop(): void {
    if (this.rafId !== null) cancelAnimationFrame(this.rafId);
    this.rafId = null;
  }

  destroy(): void {
    this.stopLoop();
    (this as unknown as { _controls?: { dispose(): void } })._controls?.dispose();
    if (this.renderer && this.parent.contains(this.renderer.domElement)) {
      this.parent.removeChild(this.renderer.domElement);
    }
    this.renderer?.dispose();
    this.renderer = null;
    this.scene = null;
    this.camera = null;
  }
}
