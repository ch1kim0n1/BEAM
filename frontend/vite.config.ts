import { defineConfig } from "vite";

// BEAM frontend dev/build config. The backend serves REST on /api and WebSocket
// telemetry; proxy them in dev so the browser talks to a single origin.
export default defineConfig({
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
