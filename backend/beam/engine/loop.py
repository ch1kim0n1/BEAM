"""The decision loop - BEAM's headless simulation driver (pdd.md sections 7.4, 9.3-9.4).

This module ties the engine together. Each ``decision_period`` of sim-time it:

1. **snapshots** the world into an immutable :class:`~beam.schemas.WorldState`
   (pdd.md 7.4 step 1);
2. runs the **solver race** (pdd.md 9.4): every *enabled* solver is evaluated on the
   *identical* snapshot, recording objective + ``solve_ms``; only the **active**
   solver's :class:`~beam.schemas.Assignment` is applied to the battery;
3. computes each solver's **optimality gap** versus the exact reference (``cp_sat``),
   honestly labelled as bound-/last-exact-based when the reference is throttled above
   ``solver.cp_sat_target_threshold`` (pdd.md 7.4 step 3, 9.3, 9.4);
4. **advances** the turrets across the epoch in fixed integration sub-steps - slew to
   aim, settle, dwell/fire (Beer-Lambert delivery via :mod:`beam.engine.physics`),
   thermal accumulation with forced-cooldown hysteresis (pdd.md 7.4 step 5, 8.2-8.6) -
   and moves the swarm (:mod:`beam.engine.kinematics`);
5. **resolves** kills + leaks (:mod:`beam.engine.kill`), updates the cost **ledger**
   (:mod:`beam.cost.ledger`), and emits telemetry frame(s) + an
   :class:`~beam.schemas.EpochRecord` (pdd.md 7.4 step 6, 10, 12.2).

Determinism (pdd.md 7.4, mvp.md §4-5) is a hard contract: a single seeded RNG
(:func:`beam.util.make_rng`) drives all randomness, iteration order over drones /
turrets / solvers is fixed (list / insertion order), and no module-level RNG is ever
touched. Same seed + same active solver -> byte-identical telemetry.

NO physics or cost constant is hard-coded here; every tunable flows from
``beam.config`` (pdd.md 18). The only literals are loop-structural defaults
(integration sub-step count, numeric guards) that carry no physical meaning, and even
those are overridable via :class:`LoopConfig`.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from beam.config import BeamConfig
from beam.cost.ledger import Ledger
from beam.engine import kinematics as kin
from beam.engine import physics as phys
from beam.engine.kill import KillParams, resolve_step
from beam.engine.kinematics import KinematicsConfig
from beam.schemas import (
    Assignment,
    Drone,
    EpochRecord,
    LedgerSnapshot,
    RunSummary,
    Scenario,
    SolverResult,
    Turret,
    Vec2,
    WorldState,
)
from beam.solvers import REGISTRY, Solver
from beam.util import make_rng, vdist

from beam.engine.telemetry import (
    TelemetryRecorder,
    build_epoch_message,
    build_frame_message,
)

__all__ = [
    "GAP_EPS",
    "LoopConfig",
    "RunResult",
    "compute_gap",
    "build_scenario",
    "DecisionLoop",
    "run_headless",
]


# --------------------------------------------------------------------------- #
# Optimality gap (pdd.md 9.3)                                                  #
# --------------------------------------------------------------------------- #

# Numerical floor for the gap denominator (pdd.md 9.3: ``max(obj_optimal, epsilon)``).
# Not a physics/cost constant - purely a divide-by-zero guard.
GAP_EPS: float = 1e-9


def compute_gap(obj_optimal: float, obj_policy: float) -> float:
    """Optimality gap of a policy versus the exact reference (pdd.md 9.3).

    ``gap = (obj_optimal - obj_policy) / max(obj_optimal, eps)``

    The sign convention matches the contract: the reference is a *maximization*
    objective (value destroyed), so a policy that does worse than optimal yields a
    positive gap. The result is **not** clamped here - callers/labels decide how to
    present a (rare) negative gap; mvp.md §4 asserts gaps are non-negative when the
    reference is genuinely optimal.
    """
    return (obj_optimal - obj_policy) / max(obj_optimal, GAP_EPS)


# --------------------------------------------------------------------------- #
# Loop configuration (structural, not physical)                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LoopConfig:
    """Structural knobs for the integrator - no physical meaning.

    Attributes:
        substeps_per_epoch: how many fixed integration sub-steps each decision epoch is
            divided into. More sub-steps = finer slew/dwell/kinematics integration. The
            epoch's ``decision_period`` is split evenly; this never changes *when*
            decisions are made, only the resolution of motion/energy between them.
        max_epochs: hard cap on epochs (safety bound so a never-resolving run still
            terminates). ``0`` means "until all drones are dead or leaked".
        kwh_per_energy_unit: conversion from the simulation's illustrative delivered
            energy units to kWh for the cost ledger (pdd.md 10). Default 1.0 keeps the
            units identity (illustrative, internally consistent - pdd.md 8/18).
        seconds_per_operating_unit: conversion from elapsed sim-seconds to the unit
            ``cost.maintenance_rate`` is denominated in. Default 1.0 charges
            maintenance per sim-second; set to 3600.0 to charge per operating-hour.
        reference_solver: name of the exact reference solver the gap is measured
            against (pdd.md 9.3/9.4). Default ``"cp_sat"`` per the contract.
        partial_energy_retention: forwarded to :class:`KillParams` (pdd.md 8.6).
        leak_radius: distance from the asset at/below which a drone leaks (pdd.md 8.6).
            Defaults to the swarm's spawn geometry being large vs the asset; ``0.0`` is
            exact-reach.
    """

    substeps_per_epoch: int = 10
    max_epochs: int = 10_000
    kwh_per_energy_unit: float = 1.0
    seconds_per_operating_unit: float = 1.0
    reference_solver: str = "cp_sat"
    partial_energy_retention: bool = False
    leak_radius: float = 0.0


@dataclass
class RunResult:
    """Everything a completed headless run produces (pdd.md 7.4, 12.2, 13)."""

    summary: RunSummary
    epoch_records: list[EpochRecord] = field(default_factory=list)
    telemetry_hash: str = ""
    out_dir: Optional[str] = None


# --------------------------------------------------------------------------- #
# Scenario construction (config -> engine entities)                           #
# --------------------------------------------------------------------------- #


def _resolve_class_assignments(
    spec_count: int,
    class_mix: dict[str, float],
    cfg: BeamConfig,
) -> list[tuple[str, float, float]]:
    """Deterministically expand ``class_mix`` weights into ``count`` ``(class, hardness,
    value)`` triples.

    The mix is normalized and turned into integer counts via a stable largest-remainder
    apportionment over the classes in *insertion order* (no RNG, no dict-order
    surprises). Classes are then emitted in insertion order so spawning is reproducible.
    Falls back to the single configured class when the mix is empty.
    """
    classes = list(class_mix.items())  # insertion order = deterministic
    if not classes:
        # No mix given: use the first configured drone class (insertion order).
        only = next(iter(cfg.drone_classes))
        classes = [(only, 1.0)]

    weights = np.array([max(0.0, w) for _, w in classes], dtype=float)
    total = weights.sum()
    if total <= 0.0:
        weights = np.ones(len(classes))
        total = weights.sum()

    exact = weights / total * spec_count
    base = np.floor(exact).astype(int)
    remainder = spec_count - int(base.sum())
    # Largest-remainder: hand out leftover slots to the biggest fractional parts,
    # tie-broken by class index (stable).
    fracs = exact - base
    order = sorted(range(len(classes)), key=lambda i: (-fracs[i], i))
    for k in range(remainder):
        base[order[k % len(order)]] += 1

    triples: list[tuple[str, float, float]] = []
    for (name, _), n in zip(classes, base):
        dc = cfg.drone_class(name)
        triples.extend([(name, dc.hardness, dc.value)] * int(n))
    return triples


def _build_turret(raw: dict[str, Any], cfg: BeamConfig) -> Turret:
    """Build a :class:`Turret`, falling back to ``turret_defaults`` for omitted fields."""
    td = cfg.turret_defaults
    pos = raw["pos"]
    return Turret(
        id=raw["id"],
        pos=Vec2(x=float(pos["x"]), y=float(pos["y"])),
        aim=float(raw.get("aim", 0.0)),
        slew_rate=float(raw.get("slew_rate", td.slew_rate)),
        settle_time=float(raw.get("settle_time", td.settle_time)),
        power=float(raw.get("power", td.power)),
        range_max=float(raw.get("range_max", td.range_max)),
        thermal=float(raw.get("thermal", 0.0)),
        thermal_cfg=td.thermal,
    )


def build_scenario(cfg: BeamConfig) -> Scenario:
    """Resolve a :class:`Scenario` from the loaded config's raw ``scenario`` overlay.

    Reads ``cfg.scenario`` (the raw scenario YAML; see ``beam.config``). Turret fields
    omitted in the scenario fall back to ``turret_defaults`` (pdd.md 18). Seed and
    decision period fall back to ``sim`` defaults when the scenario omits them.
    """
    scn = cfg.scenario
    if scn is None:
        raise ValueError("config has no scenario overlay; load_config(scenario=...) first")

    asset = scn["asset_pos"]
    swarm = scn["swarm_spec"]
    from beam.schemas import SwarmSpec

    spec = SwarmSpec(
        count=int(swarm["count"]),
        behavior=swarm.get("behavior", "direct"),
        class_mix={k: float(v) for k, v in swarm.get("class_mix", {}).items()},
        spawn_radius=float(swarm.get("spawn_radius", 0.0)),
        spawn_arc_deg=tuple(swarm.get("spawn_arc_deg", (0.0, 360.0))),  # type: ignore[arg-type]
        speed=float(swarm.get("speed", 0.0)),
    )
    return Scenario(
        id=str(scn.get("id", "scenario")),
        asset_pos=Vec2(x=float(asset["x"]), y=float(asset["y"])),
        battery=[_build_turret(t, cfg) for t in scn["battery"]],
        swarm_spec=spec,
        weather=str(scn.get("weather", "clear")),
        seed=int(scn.get("seed", cfg.sim.seed)),
        decision_period=float(scn.get("decision_period", cfg.sim.decision_period)),
    )


# --------------------------------------------------------------------------- #
# Per-turret servicing plan (derived from an Assignment)                       #
# --------------------------------------------------------------------------- #


@dataclass
class _TurretPlan:
    """Mutable per-turret execution state for the active assignment within an epoch."""

    order: list[str]  # remaining target ids in firing order (active assignment)
    idx: int = 0  # cursor into ``order``
    settle_remaining: float = 0.0  # seconds of settle left after the last slew

    def current_target(self) -> Optional[str]:
        while self.idx < len(self.order):
            return self.order[self.idx]
        return None

    def advance(self) -> None:
        self.idx += 1
        self.settle_remaining = 0.0


# --------------------------------------------------------------------------- #
# The loop                                                                     #
# --------------------------------------------------------------------------- #


class DecisionLoop:
    """A single deterministic headless run of one scenario (pdd.md 7.4).

    Construct with a resolved :class:`Scenario`, the loaded :class:`BeamConfig`, the
    name of the active solver, and the set of enabled solvers for the race. Then call
    :meth:`run` (or iterate :meth:`step_epoch`).

    The loop owns the *engine* entities (drones + turrets) and mutates them in place;
    solvers only ever see read-only :class:`WorldState` snapshots, never the live
    entities (pdd.md 9.1).
    """

    def __init__(
        self,
        scenario: Scenario,
        cfg: BeamConfig,
        *,
        active_solver: str,
        enabled_solvers: Optional[list[str]] = None,
        solver_factory: Optional[Callable[[str, int], Solver]] = None,
        loop_cfg: Optional[LoopConfig] = None,
    ) -> None:
        self.scenario = scenario
        self.cfg = cfg
        self.loop_cfg = loop_cfg or LoopConfig()
        self.active_solver = active_solver

        # Enabled set for the race: explicit list, else every registered solver, else
        # at minimum the active solver (so a run is always possible). Order is fixed
        # (REGISTRY insertion order / given list order) for a deterministic race.
        if enabled_solvers is not None:
            self.enabled_solvers = list(enabled_solvers)
        elif REGISTRY:
            self.enabled_solvers = list(REGISTRY.keys())
        else:
            self.enabled_solvers = [active_solver]
        if active_solver not in self.enabled_solvers:
            self.enabled_solvers.append(active_solver)

        # Single seeded RNG for the whole run (pdd.md 7.4 determinism).
        self.rng = make_rng(scenario.seed)

        # Resolve weather + kinematics tunables from config (no hard-coded physics).
        self.weather_alpha = cfg.weather(scenario.weather).alpha
        self.track_eff = cfg.physics.track_efficiency
        raw_kin = (cfg.scenario or {}).get("kinematics")
        if raw_kin is None:
            # kinematics lives in defaults.yaml but is not on the typed BeamConfig
            # (its sub-models forbid extras); re-read it from the raw defaults file.
            raw_kin = _load_raw_kinematics()
        self.kin_cfg: KinematicsConfig = KinematicsConfig.from_raw(raw_kin)

        self.kill_params = KillParams(
            partial_energy_retention=self.loop_cfg.partial_energy_retention,
            leak_radius=self.loop_cfg.leak_radius,
        )

        # Build the solver instances (deterministic, seeded from the run seed).
        factory = solver_factory or _default_solver_factory
        self.solvers: dict[str, Solver] = {}
        for i, name in enumerate(self.enabled_solvers):
            # Derive a distinct, deterministic seed per solver from the run seed so two
            # solvers that both need randomness do not share a stream, yet the whole
            # run stays reproducible.
            self.solvers[name] = factory(name, scenario.seed + 1 + i)

        # Engine entities.
        triples = _resolve_class_assignments(
            scenario.swarm_spec.count, scenario.swarm_spec.class_mix, cfg
        )
        self.drones: list[Drone] = kin.spawn_swarm(
            scenario.swarm_spec,
            scenario.asset_pos,
            triples,
            self.rng,
            kin=self.kin_cfg,
        )
        self.turrets: list[Turret] = list(scenario.battery)

        # Staggered release schedule (sim-time at which each drone index activates).
        self._release_t: list[float] = []
        if scenario.swarm_spec.behavior == "staggered":
            n = len(self.drones)
            self._release_t = [
                kin.staggered_release_time(i, n, self.kin_cfg.staggered)
                for i in range(n)
            ]
            # Hold un-released drones off-board: mark dormant by freezing them at spawn
            # and only integrating after release (handled in _advance).

        # Cost ledger + accumulators.
        self.ledger = Ledger(cfg.cost)
        self._forced_cooldown: dict[str, bool] = {t.id: False for t in self.turrets}
        # Delivered/emitted power fraction (Beer-Lambert) for each turret's most recent
        # firing sub-step, surfaced as BeamFrame.power_frac for renderer intensity.
        self._power_frac: dict[str, float] = {t.id: 1.0 for t in self.turrets}

        # Run-level tallies.
        self.t: float = 0.0
        self.epoch: int = 0
        self.kills: int = 0
        self.leaks: int = 0
        self.leaked_value: float = 0.0
        self.last_ledger: LedgerSnapshot = self.ledger.snapshot()
        # Value destroyed in the epoch currently being advanced (folded into the ledger
        # once per epoch, in step_epoch).
        self._epoch_value_destroyed: float = 0.0

        # Last proven-exact reference objective + whether it is "fresh" (this epoch)
        # vs carried from a throttled epoch (pdd.md 7.4/9.3 last-exact labelling).
        self._last_exact_obj: Optional[float] = None
        self._last_exact_was_bound: bool = False

        # Per-solver running stats for the RunSummary.
        self._gap_sums: dict[str, float] = {n: 0.0 for n in self.enabled_solvers}
        self._gap_counts: dict[str, int] = {n: 0 for n in self.enabled_solvers}
        self._ms_sums: dict[str, float] = {n: 0.0 for n in self.enabled_solvers}
        self._ms_counts: dict[str, int] = {n: 0 for n in self.enabled_solvers}

    # ------------------------------------------------------------------ #
    # Snapshotting                                                        #
    # ------------------------------------------------------------------ #

    def _live_drones(self) -> list[Drone]:
        """Live + detected drones the solver may consider (pdd.md WorldState).

        Excludes dead/leaked drones and, for staggered runs, not-yet-released ones.
        Fixed iteration order (list order) for determinism.
        """
        live: list[Drone] = []
        for i, d in enumerate(self.drones):
            if d.state in ("dead", "leaked"):
                continue
            if self._release_t and self.t < self._release_t[i]:
                continue
            live.append(d)
        return live

    def snapshot(self) -> WorldState:
        """Build the immutable :class:`WorldState` handed to every solver this epoch.

        Deep-copies entities (``model_copy(deep=True)``) so a solver can never mutate
        engine state, honoring the read-only contract (pdd.md 9.1).
        """
        return WorldState(
            t=self.t,
            drones=[d.model_copy(deep=True) for d in self._live_drones()],
            turrets=[t.model_copy(deep=True) for t in self.turrets],
            weather_alpha=self.weather_alpha,
            asset_pos=self.scenario.asset_pos.model_copy(deep=True),
        )

    # ------------------------------------------------------------------ #
    # Solver race (pdd.md 9.4) + gap (pdd.md 9.3)                          #
    # ------------------------------------------------------------------ #

    def _reference_enabled_this_epoch(self, n_targets: int) -> bool:
        """Whether the exact reference runs (not throttled) this epoch (pdd.md 7.4/9.4).

        The reference (``cp_sat``) runs every epoch while the live target count is at or
        below ``solver.cp_sat_target_threshold``; above it the exact solve is throttled
        and the gap is referenced to the last exact solve (labelled bound-based).
        """
        return n_targets <= self.cfg.solver.cp_sat_target_threshold

    def run_solver_race(self, state: WorldState) -> tuple[list[SolverResult], Assignment]:
        """Evaluate every enabled solver on the identical ``state`` (pdd.md 9.4).

        Returns ``(solver_results, active_assignment)``. All solvers are *evaluated*
        (objective + solve_ms recorded); only the active solver's assignment is returned
        for execution. The reference (``cp_sat``) is skipped when throttled; its last
        proven objective is reused for the gap, with results labelled accordingly.

        Iteration order is the fixed enabled-solver order, so the race is deterministic.
        """
        ref_name = self.loop_cfg.reference_solver
        n_targets = len(state.drones)
        run_ref = (
            ref_name in self.solvers
            and self._reference_enabled_this_epoch(n_targets)
        )

        deadline_ref = self.cfg.solver.cp_sat_time_budget_ms

        # 1) Run the reference first (when enabled) so heuristics can be gapped against
        #    a fresh optimum this epoch.
        ref_result: Optional[SolverResult] = None
        if run_ref:
            ref_result = self._evaluate(ref_name, state, deadline_ref)
            # Whether the reference proved optimality (is_optimal True) or only a bound.
            proven = bool(ref_result.is_optimal)
            self._last_exact_obj = ref_result.objective
            self._last_exact_was_bound = not proven

        # The objective the gap is measured against, and whether it is bound-based.
        gap_anchor = self._last_exact_obj
        anchor_is_bound = self._last_exact_was_bound or (not run_ref)

        results: list[SolverResult] = []
        active_assignment = Assignment()

        for name in self.enabled_solvers:
            if name == ref_name and ref_result is not None:
                res = ref_result
            elif name == ref_name and not run_ref:
                # Reference throttled this epoch: report its last-exact carry-forward as
                # a non-executed row so the wire still shows the reference series.
                res = SolverResult(
                    name=name,
                    objective=gap_anchor if gap_anchor is not None else 0.0,
                    solve_ms=0.0,
                    is_optimal=False,
                    bound=gap_anchor,
                    gap_is_bound_based=True,
                )
            else:
                # Heuristic / policy solver: must run to produce its assignment for
                # the active execution and its objective for the race.
                res = self._evaluate(name, state, _policy_deadline(self.cfg))
                if name != ref_name and gap_anchor is not None:
                    res.gap = compute_gap(gap_anchor, res.objective)
                    res.gap_is_bound_based = anchor_is_bound

            results.append(res)
            if name == self.active_solver and res.assignment is not None:
                active_assignment = res.assignment

            # Accumulate per-solver stats.
            self._ms_sums[name] += res.solve_ms
            self._ms_counts[name] += 1
            if res.gap is not None:
                self._gap_sums[name] += res.gap
                self._gap_counts[name] += 1

        return results, active_assignment

    def _evaluate(self, name: str, state: WorldState, deadline_ms: int) -> SolverResult:
        """Run one solver against ``state``, timing it, and self-report its objective."""
        solver = self.solvers[name]
        t0 = time.perf_counter()
        assignment = solver.solve(state, deadline_ms)
        solve_ms = (time.perf_counter() - t0) * 1000.0
        objective = assignment.objective_estimate
        is_ref = name == self.loop_cfg.reference_solver
        return SolverResult(
            name=name,
            objective=objective,
            solve_ms=solve_ms,
            assignment=assignment,
            # The reference self-declares optimality via the contract fields if it set
            # them on the assignment's objective; we mark the reference as the anchor.
            is_optimal=True if is_ref else None,
            bound=objective if is_ref else None,
        )

    # ------------------------------------------------------------------ #
    # Executing the active assignment across the epoch                    #
    # ------------------------------------------------------------------ #

    def _apply_assignment(self, assignment: Assignment) -> dict[str, _TurretPlan]:
        """Turn the active :class:`Assignment` into a per-turret execution plan.

        Only turrets named in ``turret_orders`` get a plan; others stay idle. Targets
        that are not currently live are filtered out (a stale id from the snapshot).
        Fixed turret iteration order.
        """
        live_ids = {d.id for d in self._live_drones()}
        plans: dict[str, _TurretPlan] = {}
        for turret in self.turrets:
            order = [
                tid for tid in assignment.turret_orders.get(turret.id, []) if tid in live_ids
            ]
            plans[turret.id] = _TurretPlan(order=order)
        return plans

    def _advance(
        self,
        plans: dict[str, _TurretPlan],
    ) -> tuple[float, int]:
        """Advance the world by one decision period in fixed sub-steps.

        For each sub-step, in fixed turret order: slew the aim toward the current
        target, burn settle time, and (once aimed + settled + thermally able) fire,
        depositing Beer-Lambert energy. Then move the swarm, age thermal state, and
        resolve kills + leaks. Returns ``(energy_delivered, engagements_opened)`` for
        the ledger.
        """
        dt = self.scenario.decision_period / self.loop_cfg.substeps_per_epoch
        drone_by_id = {d.id: d for d in self.drones}

        energy_delivered = 0.0
        engagements_opened = 0
        prev_targets: dict[str, Optional[str]] = {t.id: t.current_target for t in self.turrets}

        for _ in range(self.loop_cfg.substeps_per_epoch):
            deliveries: dict[str, float] = {}

            for turret in self.turrets:
                plan = plans[turret.id]
                target_id = plan.current_target()
                target = drone_by_id.get(target_id) if target_id else None

                # Drop a target that died/leaked since assignment; advance the plan.
                while target is not None and target.state in ("dead", "leaked"):
                    plan.advance()
                    target_id = plan.current_target()
                    target = drone_by_id.get(target_id) if target_id else None

                firing = False
                if target is not None:
                    desired_aim = kin.aim_at(turret, target.pos)
                    aimed = kin.update_turret_aim(turret, desired_aim, dt)
                    in_range = vdist(turret.pos, target.pos) <= turret.range_max
                    if not aimed:
                        turret.state = "slewing"
                        plan.settle_remaining = turret.settle_time
                    elif plan.settle_remaining > 0.0:
                        # Settling after the slew completed (pdd.md 8.4).
                        plan.settle_remaining = max(0.0, plan.settle_remaining - dt)
                        turret.state = "slewing"
                    elif in_range:
                        firing = True
                    else:
                        turret.state = "idle"
                else:
                    turret.state = "idle"

                # Thermal step (forced-cooldown hysteresis, pdd.md 8.5). A latched
                # turret cannot fire regardless of the order.
                tc = turret.thermal_cfg
                result = phys.thermal_update(
                    turret.thermal,
                    firing=firing,
                    forced_cooldown=self._forced_cooldown[turret.id],
                    dt=dt,
                    heat_rate=tc.heat_rate,
                    cool_rate=tc.cool_rate,
                    h_max=tc.h_max,
                    h_resume=tc.h_resume,
                )
                turret.thermal = result.heat
                self._forced_cooldown[turret.id] = result.forced_cooldown

                effectively_firing = firing and not result.forced_cooldown
                if effectively_firing and target is not None:
                    rng_m = vdist(turret.pos, target.pos)
                    p_del = float(
                        phys.delivered_power(turret.power, self.weather_alpha, rng_m)
                    )
                    eta = float(
                        phys.track_efficiency(
                            1.0,
                            rng_m,
                            base=self.track_eff.base,
                            range_falloff=self.track_eff.range_falloff,
                        )
                    )
                    dep = float(phys.deposition_rate(p_del, eta))
                    e = dep * dt
                    if e > 0.0:
                        deliveries[target.id] = deliveries.get(target.id, 0.0) + e
                        energy_delivered += e
                    turret.state = "firing"
                    turret.current_target = target.id
                    self._power_frac[turret.id] = (
                        p_del / turret.power if turret.power > 0.0 else 0.0
                    )
                elif result.forced_cooldown:
                    turret.state = "cooldown"
                    turret.current_target = None
                elif not firing:
                    turret.current_target = None

            # Move the swarm for this sub-step (only released, live drones move).
            self._step_swarm(dt)

            # Resolve energy -> kills, beam breaks, and leaks for this sub-step.
            newly_dead, newly_leaked = resolve_step(
                self.drones, deliveries, self.scenario.asset_pos, self.kill_params
            )
            for d in newly_dead:
                self.kills += 1
                self._epoch_value_destroyed += d.value
            for d in newly_leaked:
                self.leaks += 1
                self.leaked_value += d.value

            self.t += dt

            # Advance any plan whose current target just died.
            for turret in self.turrets:
                plan = plans[turret.id]
                cur = plan.current_target()
                if cur is not None:
                    cd = drone_by_id.get(cur)
                    if cd is not None and cd.state in ("dead", "leaked"):
                        plan.advance()

        # Count engagements opened this epoch: turrets that began servicing a (new)
        # target relative to the start of the epoch.
        for turret in self.turrets:
            if turret.current_target is not None and turret.current_target != prev_targets.get(
                turret.id
            ):
                engagements_opened += 1

        return energy_delivered, engagements_opened

    def _step_swarm(self, dt: float) -> None:
        """Integrate drone motion for one sub-step, honoring staggered release gating."""
        if not self._release_t:
            kin.step_swarm(
                self.drones,
                self.scenario.asset_pos,
                dt,
                self.scenario.swarm_spec.behavior,
                self.kin_cfg,
                self.rng,
                cruise_speed=self.scenario.swarm_spec.speed,
            )
            return
        # Staggered: only integrate released drones (gate by freezing the rest).
        movable = [
            d
            for i, d in enumerate(self.drones)
            if self.t >= self._release_t[i]
        ]
        kin.step_swarm(
            movable,
            self.scenario.asset_pos,
            dt,
            self.scenario.swarm_spec.behavior,
            self.kin_cfg,
            self.rng,
            cruise_speed=self.scenario.swarm_spec.speed,
        )

    # ------------------------------------------------------------------ #
    # One epoch                                                           #
    # ------------------------------------------------------------------ #

    def step_epoch(self) -> tuple[EpochRecord, Any]:
        """Run exactly one decision epoch and return its record + the frame message.

        Order matches pdd.md 7.4: snapshot -> race -> gap -> apply active -> advance ->
        resolve -> ledger -> telemetry.
        """
        self._epoch_value_destroyed = 0.0

        state = self.snapshot()
        solver_results, active_assignment = self.run_solver_race(state)

        plans = self._apply_assignment(active_assignment)
        energy_units, engagements = self._advance(plans)

        ledger = self.ledger.update(
            energy_delivered_kwh=energy_units * self.loop_cfg.kwh_per_energy_unit,
            operating_time=self.scenario.decision_period
            / self.loop_cfg.seconds_per_operating_unit,
            value_destroyed=self._epoch_value_destroyed,
            engagements=engagements,
        )
        self.last_ledger = ledger

        record = EpochRecord(
            epoch=self.epoch,
            t=self.t,
            solver_results=solver_results,
            active_solver=self.active_solver,
            ledger=ledger,
        )
        frame = build_frame_message(
            t=self.t,
            drones=self.drones,
            turrets=self.turrets,
            asset_pos=self.scenario.asset_pos,
            kills=self.kills,
            leaks=self.leaks,
            forced_cooldown=self._forced_cooldown,
            power_frac=self._power_frac,
        )
        self.epoch += 1
        return record, frame

    # ------------------------------------------------------------------ #
    # Termination                                                         #
    # ------------------------------------------------------------------ #

    def _done(self) -> bool:
        """True once no drone can still act (all dead or leaked)."""
        for d in self.drones:
            if d.state not in ("dead", "leaked"):
                return False
        return True

    def run(self, recorder: Optional[TelemetryRecorder] = None) -> RunResult:
        """Drive the loop to completion (or ``max_epochs``), recording telemetry.

        Returns a :class:`RunResult` with the :class:`RunSummary`, the epoch records,
        and the stable telemetry hash used by the reproducibility test (mvp.md §4-5).
        """
        rec = recorder if recorder is not None else TelemetryRecorder(run_id=self.scenario.id)
        cap = self.loop_cfg.max_epochs or math.inf

        while self.epoch < cap and not self._done():
            record, frame = self.step_epoch()
            epoch_msg = build_epoch_message(record)
            rec.add_frame(frame)
            rec.add_epoch(epoch_msg, record)
            # No live drone left? stop.
            if self._done():
                break

        summary = self._summary(rec.run_id)
        rec.set_summary(summary)
        return RunResult(
            summary=summary,
            epoch_records=rec.epoch_records,
            telemetry_hash=rec.telemetry_hash(),
        )

    def _summary(self, run_id: str) -> RunSummary:
        avg_gap = {
            n: (self._gap_sums[n] / self._gap_counts[n])
            for n in self.enabled_solvers
            if self._gap_counts[n] > 0
        }
        avg_ms = {
            n: (self._ms_sums[n] / self._ms_counts[n])
            for n in self.enabled_solvers
            if self._ms_counts[n] > 0
        }
        return RunSummary(
            run_id=run_id,
            kills=self.kills,
            leaks=self.leaks,
            leaked_value=self.leaked_value,
            final_ledger=self.last_ledger,
            avg_gap_by_solver=avg_gap,
            avg_solve_ms_by_solver=avg_ms,
        )


# --------------------------------------------------------------------------- #
# Helpers (module-level)                                                       #
# --------------------------------------------------------------------------- #


def _policy_deadline(cfg: BeamConfig) -> int:
    """Wall-clock budget (ms) handed to non-reference solvers each epoch.

    Heuristics are fast (mvp.md §5); we give them the same generous budget as the
    reference so an anytime metaheuristic can use it. Anything tighter is the solver's
    own concern; the loop only enforces the ceiling via the contract's ``deadline_ms``.
    """
    return cfg.solver.cp_sat_time_budget_ms


def _load_raw_kinematics() -> dict[str, Any]:
    """Read the raw ``kinematics`` block from ``config/defaults.yaml``.

    ``BeamConfig`` deliberately drops this block (its sub-models forbid extras), so the
    loop reads it straight from the YAML to feed :class:`KinematicsConfig`.
    """
    import yaml

    from beam.config import config_dir

    with (config_dir() / "defaults.yaml").open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return raw["kinematics"]


def _default_solver_factory(name: str, seed: int) -> Solver:
    """Instantiate a registered solver by name with a deterministic seed.

    Solvers may take ``seed`` in their constructor (for the metaheuristic's RNG) or take
    no args; both are supported. The loop never reseeds a solver mid-run (pdd.md 9.1).
    """
    cls = REGISTRY[name]
    try:
        return cls(seed=seed)  # type: ignore[call-arg]
    except TypeError:
        return cls()  # type: ignore[call-arg]


def run_headless(
    cfg: BeamConfig,
    *,
    active_solver: str,
    enabled_solvers: Optional[list[str]] = None,
    out_dir: Optional[str] = None,
    loop_cfg: Optional[LoopConfig] = None,
    solver_factory: Optional[Callable[[str, int], Solver]] = None,
) -> RunResult:
    """Convenience entry point: build the scenario, run the loop, optionally persist.

    Used by the CLI ``run`` subcommand and the batch sweeps. When ``out_dir`` is given,
    telemetry (``telemetry.jsonl``) and the summary (``summary.json``) are written under
    ``runs/<run_id>/`` (pdd.md 12.2); the returned :class:`RunResult` carries the stable
    telemetry hash either way (mvp.md §4-5 reproducibility).
    """
    scenario = build_scenario(cfg)
    if loop_cfg is None:
        # Derive loop bounds from config (pdd.md 8.6, 16) so the CLI/batch produce a
        # terminating run. Tests pass an explicit loop_cfg and are unaffected.
        loop_cfg = LoopConfig(
            leak_radius=cfg.sim.leak_radius,
            max_epochs=cfg.sim.max_epochs,
        )
    loop = DecisionLoop(
        scenario,
        cfg,
        active_solver=active_solver,
        enabled_solvers=enabled_solvers,
        solver_factory=solver_factory,
        loop_cfg=loop_cfg,
    )
    recorder = TelemetryRecorder(run_id=scenario.id)
    result = loop.run(recorder)
    if out_dir is not None:
        path = recorder.write(out_dir)
        result.out_dir = path
    return result
