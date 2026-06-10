import { describe, it, expect, vi } from "vitest";

// Minimal parent mock - Battlefield3D only needs appendChild/contains/removeChild + style
function makeFakeParent() {
  const children: unknown[] = [];
  return {
    clientWidth: 800,
    clientHeight: 600,
    appendChild: vi.fn((child: unknown) => { children.push(child); }),
    removeChild: vi.fn(),
    contains: vi.fn(() => true),
  };
}

// Fake canvas element for renderer.domElement
function fakeCanvas() {
  return { style: {} as CSSStyleDeclaration };
}

vi.mock("three", () => ({
  WebGLRenderer: vi.fn(() => ({
    setSize: vi.fn(),
    setPixelRatio: vi.fn(),
    render: vi.fn(),
    dispose: vi.fn(),
    domElement: fakeCanvas(),
  })),
  Scene: vi.fn(() => ({ add: vi.fn(), remove: vi.fn(), background: null })),
  PerspectiveCamera: vi.fn(() => ({ position: { set: vi.fn() }, lookAt: vi.fn() })),
  Color: vi.fn(),
  AmbientLight: vi.fn(() => ({})),
  DirectionalLight: vi.fn(() => ({ position: { set: vi.fn() } })),
  GridHelper: vi.fn(() => ({})),
  Mesh: vi.fn(() => ({
    position: { set: vi.fn() },
    rotation: { y: 0 },
    material: { opacity: 1, color: 0 },
  })),
  MeshStandardMaterial: vi.fn(() => ({ opacity: 1, color: 0 })),
  ConeGeometry: vi.fn(() => ({})),
  CylinderGeometry: vi.fn(() => ({})),
  BoxGeometry: vi.fn(() => ({})),
  LineBasicMaterial: vi.fn(() => ({ opacity: 1 })),
  Line: vi.fn(() => ({
    geometry: { setFromPoints: vi.fn() },
    material: { opacity: 1 },
  })),
  BufferGeometry: vi.fn(() => ({ setFromPoints: vi.fn() })),
  Vector3: vi.fn((x: number, y: number, z: number) => ({ x, y, z })),
}));

import { Battlefield3D } from "./battlefield3d";

describe("Battlefield3D", () => {
  it("constructs without throwing", () => {
    const parent = makeFakeParent();
    expect(() => new Battlefield3D({ parent: parent as unknown as HTMLElement })).not.toThrow();
  });

  it("destroy() does not throw on uninitialized instance", () => {
    const parent = makeFakeParent();
    const bf = new Battlefield3D({ parent: parent as unknown as HTMLElement });
    expect(() => bf.destroy()).not.toThrow();
  });

  it("setActive(false) does not throw before init", () => {
    const parent = makeFakeParent();
    const bf = new Battlefield3D({ parent: parent as unknown as HTMLElement });
    expect(() => bf.setActive(false)).not.toThrow();
  });
});
