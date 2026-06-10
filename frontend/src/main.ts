// BEAM frontend entry point (pdd.md 14.1–14.3).
//
// Composes the whole UI: top scoreboard, left control panel, center battlefield
// (with the solver-race split mode), and right dashboards - all driven by the
// validated net/ telemetry stream. The heavy lifting lives in app/BeamApp; this
// file only mounts it into #app and surfaces fatal init errors.

import "./styles.css";
import { BeamApp } from "./app";

function mount(): void {
  const root = document.getElementById("app");
  if (!root) {
    throw new Error("BEAM: #app mount point not found");
  }
  const app = new BeamApp({ root });
  app.init().catch((err) => {
    // Keep the failure visible on the dark canvas rather than only in the console.
    root.textContent = `BEAM failed to start: ${String(err)}`;
    console.error("BEAM init failed", err);
  });
  // Expose for ad-hoc debugging in the browser console (non-load-bearing).
  (globalThis as unknown as { beam?: BeamApp }).beam = app;
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount, { once: true });
} else {
  mount();
}
