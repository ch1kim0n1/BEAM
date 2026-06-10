"""In-memory runtime: scenario/run/batch stores + the async run controller.

This is the stateful core behind the REST + WebSocket surface (pdd.md 12). It owns:

- :class:`ScenarioStore` — validated scenario overlays keyed by an opaque id;
- :class:`RunController` — wraps the synchronous :class:`beam.engine.loop.DecisionLoop`
  in an asyncio driver so a run can be paused/resumed/stepped/stopped and its speed
  changed at runtime (pdd.md 12.3), while streaming frame + epoch telemetry to any
  number of attached WebSocket subscribers (pdd.md 12.2);
- :class:`Registry` — the process-wide collection of scenarios, runs, and batches.

Determinism is preserved end-to-end: the controller never touches the simulation's
RNG; it only gates *when* the deterministic ``step_epoch`` runs and how fast frames are
emitted. Same seed + same active solver => identical telemetry regardless of pacing,
pausing, or how many clients are attached (the engine's repro contract, mvp.md §4-5).

No physics/cost constant lives here; everything flows from :mod:`beam.config`.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

from beam.config import BeamConfig, load_config
from beam.engine.loop import DecisionLoop, LoopConfig, build_scenario
from beam.engine.telemetry import TelemetryRecorder, build_epoch_message
from beam.schemas import RunSummary, Scenario
from beam.solvers import available_solvers

__all__ = [
    "DEFAULT_REFERENCE_SOLVER",
    "ScenarioStore",
    "RunController",
    "BatchJob",
    "Registry",
    "merge_overlay",
    "resolve_config",
]

# The exact reference solver the gap is measured against (pdd.md 9.3/9.4). Must match
# LoopConfig.reference_solver; kept here so the API can advertise it and pick a sane
# default active solver.
DEFAULT_REFERENCE_SOLVER = "cp_sat"


# --------------------------------------------------------------------------- #
# Config / scenario resolution helpers                                        #
# --------------------------------------------------------------------------- #


def merge_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overlay`` onto ``base`` (overlay wins). Returns a new dict.

    Mappings are merged recursively; every other type (including lists) is replaced
    wholesale by the overlay value. Used to layer a request's raw scenario dict on top
    of a named preset.
    """
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = merge_overlay(out[key], val)
        else:
            out[key] = val
    return out


def resolve_config(
    scenario_overlay: Optional[dict[str, Any]],
    *,
    preset: Optional[str] = None,
) -> BeamConfig:
    """Build a :class:`BeamConfig` whose ``scenario`` overlay is fully resolved.

    Loads ``defaults.yaml`` (and the named preset, if any), then layers an explicit
    overlay dict on top. Validates by constructing the :class:`Scenario` eagerly so a
    bad payload fails at the API boundary, not mid-run.
    """
    cfg = load_config(scenario=preset)
    if scenario_overlay is not None:
        base = cfg.scenario or {}
        cfg.scenario = merge_overlay(base, scenario_overlay)
    if cfg.scenario is None:
        raise ValueError("no scenario provided: supply a preset name or a scenario overlay")
    # Eager validation (raises on a malformed scenario).
    build_scenario(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# Scenario store                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class _StoredScenario:
    scenario_id: str
    overlay: dict[str, Any]  # the raw scenario dict (preset+overlay, resolved)


class ScenarioStore:
    """Opaque-id keyed store of validated scenario overlays (process-local)."""

    def __init__(self) -> None:
        self._items: dict[str, _StoredScenario] = {}
        self._counter = itertools.count(1)

    def create(self, overlay: dict[str, Any]) -> str:
        scenario_id = f"scn_{next(self._counter)}"
        self._items[scenario_id] = _StoredScenario(scenario_id, overlay)
        return scenario_id

    def get(self, scenario_id: str) -> Optional[dict[str, Any]]:
        item = self._items.get(scenario_id)
        return item.overlay if item is not None else None

    def __contains__(self, scenario_id: str) -> bool:
        return scenario_id in self._items


# --------------------------------------------------------------------------- #
# Run controller                                                              #
# --------------------------------------------------------------------------- #


class RunController:
    """Drives one :class:`DecisionLoop` asynchronously with live control + streaming.

    Lifecycle / status values: ``created`` -> ``running`` <-> ``paused`` ->
    ``finished`` / ``stopped`` / ``error``. The driver coroutine ( :meth:`run` ) steps
    the deterministic loop one epoch at a time; between epochs it sleeps for
    ``decision_period / speed`` so the render cadence on the wire roughly tracks
    sim-time, scaled by the speed multiplier (pdd.md 12.3 ``set_speed``).

    Control actions are applied between epochs (never mid-step) so the simulation stays
    deterministic. ``step`` releases exactly N epochs while paused.

    Subscribers are asyncio queues; each emitted wire message (frame, then epoch) is
    fanned out to every subscriber. The first message a subscriber misses (slow client)
    is dropped for that client only — the simulation never blocks on a consumer.
    """

    MAX_QUEUE = 2048

    def __init__(
        self,
        run_id: str,
        cfg: BeamConfig,
        *,
        active_solver: str,
        enabled_solvers: Optional[list[str]] = None,
        loop_cfg: Optional[LoopConfig] = None,
    ) -> None:
        self.run_id = run_id
        self.cfg = cfg
        self.scenario: Scenario = build_scenario(cfg)
        self.loop = DecisionLoop(
            self.scenario,
            cfg,
            active_solver=active_solver,
            enabled_solvers=enabled_solvers,
            loop_cfg=loop_cfg,
        )
        self.recorder = TelemetryRecorder(run_id=run_id)

        self.status: str = "created"
        self.speed: float = 1.0
        self.summary: Optional[RunSummary] = None
        self.telemetry_hash: Optional[str] = None
        self.error: Optional[str] = None

        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._resume = asyncio.Event()
        self._resume.set()  # start un-paused; the driver flips to paused if asked
        self._step_budget = 0  # epochs released while paused (step action)
        self._stop = False
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------ #
    # Subscriptions (WebSocket fan-out)                                   #
    # ------------------------------------------------------------------ #

    def subscribe(self) -> "asyncio.Queue[dict[str, Any]]":
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.MAX_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[dict[str, Any]]") -> None:
        self._subscribers.discard(q)

    def _publish(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # Slow consumer: drop this message for them only; never block the sim.
                pass

    # ------------------------------------------------------------------ #
    # Active properties                                                   #
    # ------------------------------------------------------------------ #

    @property
    def active_solver(self) -> str:
        return self.loop.active_solver

    @property
    def enabled_solvers(self) -> list[str]:
        return list(self.loop.enabled_solvers)

    # ------------------------------------------------------------------ #
    # Control surface (pdd.md 12.3)                                       #
    # ------------------------------------------------------------------ #

    def pause(self) -> None:
        if self.status == "running":
            self.status = "paused"
        self._resume.clear()

    def resume(self) -> None:
        if self.status == "paused":
            self.status = "running"
        self._step_budget = 0
        self._resume.set()

    def step(self, epochs: int = 1) -> None:
        """Release exactly ``epochs`` epochs while paused, then re-pause."""
        self._step_budget = max(self._step_budget, max(1, int(epochs)))
        self._resume.set()

    def stop(self) -> None:
        self._stop = True
        self._resume.set()  # unblock the driver so it can observe the stop

    def set_solver(self, solver: str) -> None:
        """Switch the active policy applied to the battery (must be enabled)."""
        if solver not in self.loop.solvers:
            raise ValueError(
                f"solver {solver!r} is not enabled for this run; "
                f"enabled = {self.enabled_solvers}"
            )
        self.loop.active_solver = solver

    def set_speed(self, multiplier: float) -> None:
        m = float(multiplier)
        if m <= 0.0:
            raise ValueError("speed multiplier must be > 0")
        self.speed = m

    # ------------------------------------------------------------------ #
    # Driver                                                              #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """Step the loop to completion, streaming telemetry and honoring control.

        Runs as an asyncio task. Each iteration: gate on pause/step, run one
        deterministic epoch in a worker thread (the solve can be CPU-heavy), publish the
        epoch's frame then epoch message, then pace. Finalizes the summary + telemetry
        hash on natural completion, stop, or error.
        """
        self.status = "running"
        cap = self.loop.loop_cfg.max_epochs or float("inf")
        try:
            while self.loop.epoch < cap and not self.loop._done():
                if self._stop:
                    break

                # Pause/step gate: wait until resumed or a step is released.
                if self.status == "paused" and self._step_budget <= 0:
                    await self._resume.wait()
                if self._stop:
                    break

                # Run one deterministic epoch off the event loop.
                record, frame = await asyncio.to_thread(self.loop.step_epoch)
                epoch_msg = build_epoch_message(record)
                self.recorder.add_frame(frame)
                self.recorder.add_epoch(epoch_msg, record)

                self._publish(frame.model_dump(by_alias=True))
                self._publish(epoch_msg.model_dump(by_alias=True))

                # Step-mode accounting: consume one of the released epochs and re-pause
                # once the budget is exhausted.
                if self._step_budget > 0:
                    self._step_budget -= 1
                    if self._step_budget <= 0:
                        self.status = "paused"
                        self._resume.clear()

                await self._pace()

            if self._stop:
                self.status = "stopped"
            else:
                self.status = "finished"
        except Exception as exc:  # noqa: BLE001 — surface any engine error as run state
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.summary = self.loop._summary(self.run_id)
            self.recorder.set_summary(self.summary)
            self.telemetry_hash = self.recorder.telemetry_hash()
            # Wake any subscribers waiting on a sentinel-terminated stream.
            self._publish({"type": "end", "run_id": self.run_id, "status": self.status})

    async def _pace(self) -> None:
        """Sleep one render interval scaled by the speed multiplier (pdd.md 12.3)."""
        if self.status == "paused":
            return
        interval = self.scenario.decision_period / max(self.speed, 1e-9)
        # Cap the sleep so a tiny speed can't wedge the driver; floor avoids busy-loop.
        interval = min(max(interval, 0.0), 5.0)
        if interval > 0.0:
            await asyncio.sleep(interval)

    def start(self) -> "asyncio.Task[None]":
        """Schedule the driver on the running event loop (idempotent)."""
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())
        return self._task

    async def join(self) -> None:
        if self._task is not None:
            await self._task


# --------------------------------------------------------------------------- #
# Batch jobs                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class BatchJob:
    """A headless swarm-size (or arbitrary-parameter) sweep (pdd.md 10, 12.1).

    Runs the sweep synchronously off the event loop (one headless run per point per
    solver-as-active) and assembles the breakeven + gap-vs-scale series the charts
    consume. Reproducible: every point uses the sweep's fixed seed.
    """

    batch_id: str
    spec: dict[str, Any]
    status: str = "created"
    parameter: str = ""
    values: list[float] = field(default_factory=list)
    series: dict[str, Any] = field(default_factory=dict)
    breakeven_crossover: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def _set_by_path(d: dict[str, Any], dotted: str, value: Any) -> None:
    """Set ``d[a][b][c] = value`` for a dotted ``a.b.c`` path, creating dicts as needed."""
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def run_batch(job: BatchJob, store: "ScenarioStore") -> None:
    """Execute a sweep job in-place, populating its series (blocking; run off-loop).

    For each swept value and each listed solver, runs a headless simulation (full
    solver race, that solver active) and records the final net position and that
    solver's average optimality gap. The breakeven crossover per solver is the smallest
    swept value at which cumulative net first turns non-negative.
    """
    from beam.engine.loop import run_headless

    spec = job.spec
    sweep = spec.get("sweep", {})
    parameter = str(sweep.get("parameter", ""))
    raw_values = list(sweep.get("values", []))
    solvers = list(spec.get("solvers", available_solvers()))
    base_scenario = spec.get("base_scenario")
    seed = spec.get("seed")

    job.parameter = parameter
    job.values = [float(v) for v in raw_values]

    net_by_solver: dict[str, list[float]] = {s: [] for s in solvers}
    gap_by_solver: dict[str, list[Optional[float]]] = {s: [] for s in solvers}
    crossover: dict[str, Optional[float]] = {s: None for s in solvers}

    # Resolve the base overlay either from a stored scenario id or the preset name.
    stored_overlay: Optional[dict[str, Any]] = None
    preset: Optional[str] = None
    if isinstance(base_scenario, str) and base_scenario in store:
        stored_overlay = store.get(base_scenario)
    elif isinstance(base_scenario, str):
        preset = base_scenario

    for value in raw_values:
        overlay: dict[str, Any] = {}
        if parameter:
            _set_by_path(overlay, parameter, value)
        if seed is not None:
            overlay["seed"] = int(seed)
        combined = merge_overlay(stored_overlay or {}, overlay) if stored_overlay else overlay
        cfg = resolve_config(combined, preset=preset)

        for solver in solvers:
            result = run_headless(
                cfg,
                active_solver=solver,
                enabled_solvers=solvers,
            )
            net = result.summary.final_ledger.net
            net_by_solver[solver].append(net)
            gap_by_solver[solver].append(
                result.summary.avg_gap_by_solver.get(solver)
            )
            if crossover[solver] is None and net >= 0.0:
                crossover[solver] = float(value)

    job.series = {
        "net_position_vs_swarm_size": net_by_solver,
        "gap_vs_swarm_size": gap_by_solver,
    }
    job.breakeven_crossover = {k: v for k, v in crossover.items()}
    job.status = "finished"


# --------------------------------------------------------------------------- #
# Process-wide registry                                                        #
# --------------------------------------------------------------------------- #


class Registry:
    """Holds all live scenarios, runs, and batches for one server process."""

    def __init__(self) -> None:
        self.scenarios = ScenarioStore()
        self.runs: dict[str, RunController] = {}
        self.batches: dict[str, BatchJob] = {}
        self._run_counter = itertools.count(1)
        self._batch_counter = itertools.count(1)

    def new_run_id(self) -> str:
        return f"run_{next(self._run_counter)}"

    def new_batch_id(self) -> str:
        return f"batch_{next(self._batch_counter)}"
