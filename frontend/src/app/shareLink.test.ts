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

  it("URL-encodes the token (no raw + or = chars from btoa)", () => {
    const token = { v: 1 as const, solver: "auction" };
    const url = shareUrl(token, "http://localhost:5173");
    // The ?run= value should be percent-encoded (no raw btoa chars)
    const runParam = new URL(url).searchParams.get("run");
    expect(runParam).not.toBeNull();
    expect(decodeShareToken(runParam!)).toEqual(token);
  });
});
