// frontend/src/charts/pareto.ts
import type { ParetoPointResponse } from "../types";

export class ParetoChart {
  private points: ParetoPointResponse[] = [];

  setPoints(points: ParetoPointResponse[]): void {
    this.points = points;
  }

  reset(): void {
    this.points = [];
  }

  render(canvas: HTMLCanvasElement): void {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const W = canvas.width || canvas.offsetWidth || 400;
    const H = canvas.height || canvas.offsetHeight || 200;
    const PAD = { top: 20, right: 20, bottom: 40, left: 70 };
    const plotW = W - PAD.left - PAD.right;
    const plotH = H - PAD.top - PAD.bottom;

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#0a0e14";
    ctx.fillRect(0, 0, W, H);

    if (this.points.length === 0) {
      ctx.fillStyle = "#4a5568";
      ctx.font = "12px monospace";
      ctx.textAlign = "center";
      ctx.fillText("Click Run Pareto Analysis to generate frontier", W / 2, H / 2);
      return;
    }

    const costs = this.points.map((p) => p.total_cost);
    const values = this.points.map((p) => p.value_saved);
    const minCost = Math.min(...costs);
    const maxCost = Math.max(...costs);
    const minVal = Math.min(...values);
    const maxVal = Math.max(...values);
    const rangeC = maxCost - minCost || 1;
    const rangeV = maxVal - minVal || 1;

    const px = (cost: number) => PAD.left + ((cost - minCost) / rangeC) * plotW;
    const py = (val: number) => PAD.top + plotH - ((val - minVal) / rangeV) * plotH;

    // Grid
    ctx.strokeStyle = "#1a2030";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = PAD.top + (i / 4) * plotH;
      ctx.beginPath();
      ctx.moveTo(PAD.left, y);
      ctx.lineTo(W - PAD.right, y);
      ctx.stroke();
    }

    // Frontier line
    ctx.strokeStyle = "#00ff88";
    ctx.lineWidth = 2;
    ctx.beginPath();
    this.points.forEach((p, i) => {
      if (i === 0) ctx.moveTo(px(p.total_cost), py(p.value_saved));
      else ctx.lineTo(px(p.total_cost), py(p.value_saved));
    });
    ctx.stroke();

    // Points
    for (const p of this.points) {
      ctx.beginPath();
      ctx.arc(px(p.total_cost), py(p.value_saved), 4, 0, Math.PI * 2);
      ctx.fillStyle = "#00ff88";
      ctx.fill();
    }

    // Axis labels
    ctx.fillStyle = "#8899aa";
    ctx.font = "11px monospace";
    ctx.textAlign = "center";
    ctx.fillText("Total Cost", PAD.left + plotW / 2, H - 6);
    ctx.save();
    ctx.translate(14, PAD.top + plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText("Value Saved", 0, 0);
    ctx.restore();

    // Axis ticks
    ctx.fillStyle = "#4a5568";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";
    const fmtK = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(0)}k` : String(Math.round(n));
    for (let i = 0; i <= 4; i++) {
      const val = minVal + (i / 4) * rangeV;
      ctx.fillText(fmtK(val), PAD.left - 4, py(val) + 4);
    }
  }
}
