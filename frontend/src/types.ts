// BEAM shared wire types — the single source of truth the rest of the frontend
// imports. Mirrors the backend pydantic contract in backend/beam/schemas/models.py
// and the REST envelopes in backend/beam/api/models.py (pdd.md sections 12 & 13).
//
// Every inbound wire message is described by a zod schema; the matching TypeScript
// type is *inferred* from that schema (`z.infer<...>`), so the validator and the type
// can never drift. Outbound (client -> server) shapes are plain interfaces — they are
// authored by us, validated by the server.
//
// IMPORTANT: keep SCHEMA_VERSION in sync with backend SCHEMA_VERSION
// (beam.schemas.SCHEMA_VERSION). Bump together when any wire model changes shape.

import { z } from "zod";

// Telemetry + control wire protocol version (pdd.md 12.2 / 12.3).
export const SCHEMA_VERSION = "1.0" as const;

// --------------------------------------------------------------------------- //
// Enumerated states (string literals — stable across the wire)                //
// --------------------------------------------------------------------------- //

export const DroneStateSchema = z.enum(["alive", "engaged", "dead", "leaked"]);
export type DroneState = z.infer<typeof DroneStateSchema>;

export const TurretStateSchema = z.enum(["idle", "slewing", "firing", "cooldown"]);
export type TurretState = z.infer<typeof TurretStateSchema>;

export const BehaviorProfileSchema = z.enum(["direct", "flocking", "staggered"]);
export type BehaviorProfile = z.infer<typeof BehaviorProfileSchema>;

// --------------------------------------------------------------------------- //
// Primitive geometry                                                          //
// --------------------------------------------------------------------------- //

export const Vec2Schema = z.object({
  x: z.number(),
  y: z.number(),
});
export type Vec2 = z.infer<typeof Vec2Schema>;

// --------------------------------------------------------------------------- //
// Configuration-backed value objects                                          //
// --------------------------------------------------------------------------- //

export const ThermalConfigSchema = z.object({
  heat_rate: z.number(),
  cool_rate: z.number(),
  h_max: z.number(),
  h_resume: z.number(),
});
export type ThermalConfig = z.infer<typeof ThermalConfigSchema>;

export const WeatherProfileSchema = z.object({
  name: z.string(),
  alpha: z.number(),
});
export type WeatherProfile = z.infer<typeof WeatherProfileSchema>;

// --------------------------------------------------------------------------- //
// Domain entities (pdd.md section 13)                                          //
// --------------------------------------------------------------------------- //

export const DroneSchema = z.object({
  id: z.string(),
  pos: Vec2Schema,
  vel: Vec2Schema,
  value: z.number(),
  hardness: z.number(),
  class_name: z.string(),
  state: DroneStateSchema.default("alive"),
  energy_absorbed: z.number().default(0),
});
export type Drone = z.infer<typeof DroneSchema>;

export const TurretSchema = z.object({
  id: z.string(),
  pos: Vec2Schema,
  aim: z.number(),
  slew_rate: z.number(),
  settle_time: z.number(),
  power: z.number(),
  range_max: z.number(),
  thermal: z.number(),
  thermal_cfg: ThermalConfigSchema,
  state: TurretStateSchema.default("idle"),
  current_target: z.string().nullable().default(null),
});
export type Turret = z.infer<typeof TurretSchema>;

export const SwarmSpecSchema = z.object({
  count: z.number().int(),
  behavior: BehaviorProfileSchema.default("direct"),
  class_mix: z.record(z.string(), z.number()).default({}),
  spawn_radius: z.number().default(0),
  spawn_arc_deg: z.tuple([z.number(), z.number()]).default([0, 360]),
  speed: z.number().default(0),
});
export type SwarmSpec = z.infer<typeof SwarmSpecSchema>;

export const ScenarioSchema = z.object({
  id: z.string(),
  asset_pos: Vec2Schema,
  battery: z.array(TurretSchema),
  swarm_spec: SwarmSpecSchema,
  weather: z.string(),
  seed: z.number().int(),
  decision_period: z.number(),
});
export type Scenario = z.infer<typeof ScenarioSchema>;

// --------------------------------------------------------------------------- //
// Solver contract types (pdd.md sections 9.1 / 13)                             //
// --------------------------------------------------------------------------- //

export const AssignmentSchema = z.object({
  turret_orders: z.record(z.string(), z.array(z.string())).default({}),
  objective_estimate: z.number().default(0),
});
export type Assignment = z.infer<typeof AssignmentSchema>;

export const SolverResultSchema = z.object({
  name: z.string(),
  objective: z.number(),
  solve_ms: z.number(),
  assignment: AssignmentSchema.nullable().default(null),
  gap: z.number().nullable().default(null),
  is_optimal: z.boolean().nullable().default(null),
  bound: z.number().nullable().default(null),
  gap_is_bound_based: z.boolean().default(false),
});
export type SolverResult = z.infer<typeof SolverResultSchema>;

// --------------------------------------------------------------------------- //
// Ledger + run records (pdd.md sections 10, 13)                                //
// --------------------------------------------------------------------------- //

export const LedgerSnapshotSchema = z.object({
  cumulative_cost: z.number().default(0),
  value_destroyed: z.number().default(0),
  net: z.number().default(0),
  shot_energy_cost: z.number().default(0),
  maintenance_cost: z.number().default(0),
  capex_amortized: z.number().default(0),
  engagements: z.number().int().default(0),
});
export type LedgerSnapshot = z.infer<typeof LedgerSnapshotSchema>;

export const EpochRecordSchema = z.object({
  epoch: z.number().int(),
  t: z.number(),
  solver_results: z.array(SolverResultSchema),
  active_solver: z.string(),
  ledger: LedgerSnapshotSchema,
});
export type EpochRecord = z.infer<typeof EpochRecordSchema>;

export const RunSummarySchema = z.object({
  run_id: z.string(),
  kills: z.number().int(),
  leaks: z.number().int(),
  leaked_value: z.number(),
  final_ledger: LedgerSnapshotSchema,
  avg_gap_by_solver: z.record(z.string(), z.number()).default({}),
  avg_solve_ms_by_solver: z.record(z.string(), z.number()).default({}),
});
export type RunSummary = z.infer<typeof RunSummarySchema>;

// --------------------------------------------------------------------------- //
// Telemetry wire models — server -> client (pdd.md section 12.2)               //
// --------------------------------------------------------------------------- //

export const DroneFrameSchema = z.object({
  id: z.string(),
  x: z.number(),
  y: z.number(),
  v: z.tuple([z.number(), z.number()]),
  value: z.number(),
  hp_frac: z.number(),
  state: DroneStateSchema,
  tti: z.number(),
});
export type DroneFrame = z.infer<typeof DroneFrameSchema>;

export const TurretFrameSchema = z.object({
  id: z.string(),
  x: z.number(),
  y: z.number(),
  aim: z.number(),
  target: z.string().nullable().default(null),
  state: TurretStateSchema,
  thermal_frac: z.number(),
});
export type TurretFrame = z.infer<typeof TurretFrameSchema>;

// BeamFrame serializes its source turret under the JSON key "from" (a reserved word
// in Python, aliased server-side). We expose it as `from` on the wire schema and
// also surface a `fromTurret` convenience alias after parsing.
export const BeamFrameSchema = z
  .object({
    from: z.string(),
    to: z.string(),
    power_frac: z.number(),
  })
  .transform((b) => ({ ...b, fromTurret: b.from }));
export type BeamFrame = z.infer<typeof BeamFrameSchema>;

export const FrameMessageSchema = z.object({
  type: z.literal("frame"),
  schema_version: z.string().default(SCHEMA_VERSION),
  t: z.number(),
  drones: z.array(DroneFrameSchema).default([]),
  turrets: z.array(TurretFrameSchema).default([]),
  beams: z.array(BeamFrameSchema).default([]),
  leaks: z.number().int().default(0),
  kills: z.number().int().default(0),
});
export type FrameMessage = z.infer<typeof FrameMessageSchema>;

export const EpochSolverEntrySchema = z.object({
  name: z.string(),
  objective: z.number(),
  solve_ms: z.number(),
  gap: z.number().nullable().default(null),
  is_optimal: z.boolean().nullable().default(null),
  bound: z.number().nullable().default(null),
  gap_is_bound_based: z.boolean().default(false),
});
export type EpochSolverEntry = z.infer<typeof EpochSolverEntrySchema>;

export const EpochLedgerSchema = z.object({
  cumulative_cost: z.number(),
  value_destroyed: z.number(),
  net: z.number(),
});
export type EpochLedger = z.infer<typeof EpochLedgerSchema>;

export const EpochMessageSchema = z.object({
  type: z.literal("epoch"),
  schema_version: z.string().default(SCHEMA_VERSION),
  t: z.number(),
  epoch: z.number().int(),
  solvers: z.array(EpochSolverEntrySchema).default([]),
  active_solver: z.string(),
  ledger: EpochLedgerSchema,
});
export type EpochMessage = z.infer<typeof EpochMessageSchema>;

// --------------------------------------------------------------------------- //
// Non-telemetry control-plane messages on the same socket                      //
// (emitted by beam.api.ws / runtime: ack, error, end)                          //
// --------------------------------------------------------------------------- //

export const AckMessageSchema = z.object({
  type: z.literal("ack"),
  action: z.string(),
  status: z.string(),
  active_solver: z.string(),
  speed: z.number(),
});
export type AckMessage = z.infer<typeof AckMessageSchema>;

export const ErrorMessageSchema = z.object({
  type: z.literal("error"),
  // pydantic ValidationError.errors() is an array; ValueError str is a string.
  detail: z.union([z.string(), z.array(z.unknown())]),
});
export type ErrorMessage = z.infer<typeof ErrorMessageSchema>;

export const EndMessageSchema = z.object({
  type: z.literal("end"),
  run_id: z.string(),
  status: z.string(),
});
export type EndMessage = z.infer<typeof EndMessageSchema>;

// Discriminated union of everything the server can push down the socket. Parsing
// against this is the single validation entry point for inbound telemetry.
export const ServerMessageSchema = z.discriminatedUnion("type", [
  FrameMessageSchema,
  EpochMessageSchema,
  AckMessageSchema,
  ErrorMessageSchema,
  EndMessageSchema,
]);
export type ServerMessage = z.infer<typeof ServerMessageSchema>;

// --------------------------------------------------------------------------- //
// Control wire model — client -> server (pdd.md section 12.3)                  //
// --------------------------------------------------------------------------- //

export type ControlAction =
  | "pause"
  | "resume"
  | "step"
  | "stop"
  | "set_solver"
  | "set_speed";

export interface ControlMessage {
  action: ControlAction;
  schema_version?: string;
  solver?: string | null;
  multiplier?: number | null;
  epochs?: number | null;
}

// --------------------------------------------------------------------------- //
// REST transport envelopes — mirrors backend/beam/api/models.py (pdd.md 12.1)  //
// These are request/response shapes only; not part of the telemetry contract.  //
// --------------------------------------------------------------------------- //

export interface ScenarioCreateRequest {
  preset?: string | null;
  scenario?: Record<string, unknown> | null;
}

export interface ScenarioCreateResponse {
  scenario_id: string;
}

export interface ScenarioGetResponse {
  scenario_id: string;
  scenario: Record<string, unknown>;
}

export interface RunStartRequest {
  scenario_id: string;
  solver?: string | null;
  seed?: number | null;
  enabled_solvers?: string[] | null;
}

export interface RunStartResponse {
  run_id: string;
  active_solver: string;
  enabled_solvers: string[];
}

export interface RunControlResponse {
  run_id: string;
  action: string;
  status: string;
  active_solver: string;
  speed: number;
}

export interface RunSummaryResponse {
  run_id: string;
  status: string;
  summary?: RunSummary | null;
  telemetry_hash?: string | null;
  artifacts: Record<string, string>;
}

export interface BatchStartRequest {
  sweep?: string | null;
  sweep_spec?: Record<string, unknown> | null;
  scenario_id?: string | null;
}

export interface BatchStartResponse {
  batch_id: string;
  status: string;
}

export interface BatchResultsResponse {
  batch_id: string;
  status: string;
  parameter: string;
  values: number[];
  series: Record<string, unknown>;
  breakeven_crossover: Record<string, unknown>;
}

export interface SolverMeta {
  name: string;
  is_reference: boolean;
}

export interface SolversListResponse {
  schema_version: string;
  reference: string;
  solvers: SolverMeta[];
}

export interface WeatherMeta {
  name: string;
  alpha: number;
}

export interface WeatherListResponse {
  profiles: WeatherMeta[];
}
