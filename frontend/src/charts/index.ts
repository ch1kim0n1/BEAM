// Dashboards (pdd.md 14.1, right panel): the gap-vs-compute chart and the cost
// breakeven chart. Both are lightweight, dependency-free canvas renderers that
// consume the EpochMessage / BatchResultsResponse wire models from types.ts.
//
// Usage (per run):
//   const gap = new GapChart();
//   const cost = new BreakevenChart();
//   // on each inbound epoch message:
//   gap.push(msg); gap.render(gapCanvas);
//   cost.push(msg); cost.render(costCanvas);
//   // when batch results land:
//   cost.setBatch(batchResults); cost.render(costCanvas);

export { GapChart } from "./gap";
export type { GapChartOptions } from "./gap";

export { ParetoChart } from "./pareto";

export { BreakevenChart, interpCrossings, parseBatch } from "./breakeven";
export type { BreakevenChartOptions } from "./breakeven";

export {
  PALETTE,
  seriesColor,
  linearScale,
  niceStep,
  ticks,
  fmtCompact,
  fmtMs,
  setupCanvas,
  plotRect,
  clamp,
} from "./theme";
export type { ChartPalette, Margins, PlotRect, Scale } from "./theme";
