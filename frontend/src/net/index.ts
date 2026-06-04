// Public surface of the net layer: the typed REST + WebSocket client. types.ts holds
// the wire schemas/types; this package wraps them in a client the rest of the app uses.
export {
  ApiError,
  BeamClient,
  BeamRestClient,
  TelemetryClient,
} from "./client";
export type {
  BeamClientOptions,
  TelemetryClientOptions,
  TelemetryHandlers,
} from "./client";
