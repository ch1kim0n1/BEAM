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
  try {
    if (!encoded) return null;
    const parsed = JSON.parse(atob(encoded));
    if (parsed.v !== 1 || typeof parsed.solver !== "string") return null;
    return parsed as ShareToken;
  } catch {
    return null;
  }
}

export function shareUrl(token: ShareToken, base?: string): string {
  let resolvedBase: string;
  if (base !== undefined) {
    resolvedBase = base;
  } else if (typeof window !== "undefined") {
    resolvedBase = window.location.origin + window.location.pathname;
  } else {
    resolvedBase = "http://localhost:5173";
  }
  return `${resolvedBase}?run=${encodeURIComponent(encodeShareToken(token))}`;
}

export function parseUrlToken(): ShareToken | null {
  if (typeof window === "undefined") return null;
  const params = new URLSearchParams(window.location.search);
  const run = params.get("run");
  if (!run) return null;
  return decodeShareToken(run);
}
