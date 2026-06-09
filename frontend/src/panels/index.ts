// Control panel + scoreboard (pdd.md section 14.1). Barrel re-export.
export {
  ControlPanel,
  DEFAULT_CONTROL_STATE,
  BEHAVIOR_PROFILES,
  SPEED_STEPS,
  clampNumber,
  clampSpeed,
  scenarioOverridesFromState,
  liveControlMessages,
  type ControlState,
  type ControlSink,
  type ControlPanelOptions,
} from "./controls";

export {
  Scoreboard,
  emptyScoreboardModel,
  emptyValueAccumulator,
  foldFrameValue,
  protectedValueFraction,
  formatClock,
  formatPercent,
  type ScoreboardModel,
  type ValueAccumulator,
  type ScoreboardOptions,
} from "./scoreboard";

export {
  RunReport,
  computeReportMetrics,
  fmtMoney,
  fmtPct,
  fmtGap,
  fmtMs,
  fmtRatio,
  fmtClock,
  shortHash,
  type RunReportData,
  type RunReportContext,
  type RunReportOptions,
  type ReportMetrics,
  type SolverRow,
} from "./report";
