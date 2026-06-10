"""Decision-loop + telemetry tests (pdd.md 7.4, 9.3-9.4, 12.2; mvp.md §4-5).

The headline acceptance test is reproducibility (mvp.md §4-5 / Phase-1 acceptance):

    same seed + same active solver -> byte-identical telemetry hash.

These tests use small, fully-deterministic in-test solvers (no OR-Tools, no global
RNG) injected via the loop's ``solver_factory`` so the loop is exercised end to end
independently of the real solver agents. They also cover the solver-race contract
(all enabled solvers evaluated on the identical snapshot), the optimality-gap math and
its bound-based labelling, the throttle path above the target threshold, and the
telemetry wire shape (BeamFrame's ``from`` alias, frame/epoch interleaving).
"""

from __future__ import annotations

import json

import pytest

from beam.config import load_config
from beam.engine.loop import (
    DecisionLoop,
    LoopConfig,
    _load_raw_kinematics,
    build_scenario,
    compute_gap,
    run_headless,
)
from beam.engine.telemetry import (
    TelemetryRecorder,
)
from beam.schemas import Assignment, BeamFrame, WorldState
from beam.util import vdist


# --------------------------------------------------------------------------- #
# Deterministic in-test solvers (no RNG; pure function of the snapshot)        #
# --------------------------------------------------------------------------- #


class _GreedyNearest:
    """Each turret takes the closest still-unclaimed in-range target (pdd.md 9.2)."""

    name = "greedy_nearest"

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        orders: dict[str, list[str]] = {}
        claimed: set[str] = set()
        objective = 0.0
        for turret in state.turrets:
            best_id = None
            best_d = float("inf")
            for drone in state.drones:
                if drone.id in claimed:
                    continue
                d = vdist(turret.pos, drone.pos)
                if d < best_d:
                    best_d = d
                    best_id = drone.id
            if best_id is not None:
                orders[turret.id] = [best_id]
                claimed.add(best_id)
                objective += next(dr.value for dr in state.drones if dr.id == best_id)
        return Assignment(turret_orders=orders, objective_estimate=objective)


class _FakeExact(_GreedyNearest):
    """Stand-in 'optimal' reference: same assignment, a strictly higher objective.

    This lets us assert that heuristic gaps come out positive and correctly labelled
    without pulling in CP-SAT (which the solver agents own).
    """

    name = "cp_sat"

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        a = super().solve(state, deadline_ms)
        a.objective_estimate *= 1.25  # pretend the true optimum is 25% better
        return a


def _factory(name: str, seed: int):
    return _FakeExact(seed=seed) if name == "cp_sat" else _GreedyNearest(seed=seed)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def cfg():
    return load_config(scenario="swarm_24")


def _run(cfg, *, active="greedy_nearest", enabled=None, max_epochs=60, seed_override=None):
    scenario = build_scenario(cfg)
    if seed_override is not None:
        scenario = scenario.model_copy(update={"seed": seed_override})
    loop = DecisionLoop(
        scenario,
        cfg,
        active_solver=active,
        enabled_solvers=enabled or [active],
        solver_factory=_factory,
        loop_cfg=LoopConfig(max_epochs=max_epochs),
    )
    rec = TelemetryRecorder(run_id=scenario.id)
    result = loop.run(rec)
    return result, rec


# --------------------------------------------------------------------------- #
# 1) Reproducibility - the Phase-1 acceptance gate (mvp.md §4-5)               #
# --------------------------------------------------------------------------- #


def test_same_seed_same_solver_byte_identical_hash(cfg):
    r1, rec1 = _run(cfg)
    r2, rec2 = _run(cfg)
    assert r1.telemetry_hash == r2.telemetry_hash


def _enable_rng_jitter(cfg) -> None:
    """Turn on RNG-driven flocking heading jitter so the run seed actually bites.

    The default ``swarm_24`` config has ``jitter: 0.0`` and no staggered arc jitter, so
    nothing consumes the run RNG and any seed yields the same telemetry - itself a
    determinism property. To prove the *seed* drives randomness we feed a kinematics
    overlay (via the scenario dict the loop reads) that enables jitter.
    """
    overlay = dict(_load_raw_kinematics())
    overlay["flocking"] = dict(overlay["flocking"])
    overlay["flocking"]["jitter"] = 0.05  # rad std, RNG-driven
    cfg.scenario = dict(cfg.scenario or {})
    cfg.scenario["kinematics"] = overlay


def test_changing_seed_changes_telemetry(cfg):
    _enable_rng_jitter(cfg)
    r1, _ = _run(cfg, seed_override=1337)
    r2, _ = _run(cfg, seed_override=2024)
    assert r1.telemetry_hash != r2.telemetry_hash


def test_same_seed_with_jitter_still_reproducible(cfg):
    _enable_rng_jitter(cfg)
    r1, _ = _run(cfg, seed_override=99)
    r2, _ = _run(cfg, seed_override=99)
    assert r1.telemetry_hash == r2.telemetry_hash


def test_hash_is_invariant_to_timing(cfg):
    """The repro hash must exclude wall-clock ``solve_ms`` (machine-dependent)."""
    _, rec = _run(cfg, max_epochs=5)
    # Mutate every recorded solve_ms; the hash must not move.
    h_before = rec.telemetry_hash()
    for msg in rec.epoch_messages:
        for s in msg.solvers:
            s.solve_ms += 123.456
    assert rec.telemetry_hash() == h_before


# --------------------------------------------------------------------------- #
# 2) Solver race + gap (pdd.md 9.3-9.4)                                        #
# --------------------------------------------------------------------------- #


def test_race_evaluates_all_enabled_solvers_on_same_snapshot(cfg):
    _, rec = _run(cfg, active="greedy_nearest", enabled=["cp_sat", "greedy_nearest"], max_epochs=3)
    first = rec.epoch_messages[0]
    names = [s.name for s in first.solvers]
    assert names == ["cp_sat", "greedy_nearest"]  # fixed race order preserved


def test_gap_is_correct_and_non_negative(cfg):
    _, rec = _run(cfg, active="greedy_nearest", enabled=["cp_sat", "greedy_nearest"], max_epochs=3)
    first = rec.epoch_messages[0]
    cps = next(s for s in first.solvers if s.name == "cp_sat")
    gn = next(s for s in first.solvers if s.name == "greedy_nearest")
    # Reference labelled optimal, no gap on itself.
    assert cps.is_optimal is True
    assert cps.gap is None
    # Heuristic gap matches the closed form and is non-negative (mvp.md §4).
    assert gn.gap is not None
    assert gn.gap == pytest.approx(compute_gap(cps.objective, gn.objective))
    assert gn.gap >= 0.0
    # Fresh exact solve -> not bound-based.
    assert gn.gap_is_bound_based is False


def test_compute_gap_formula():
    assert compute_gap(100.0, 80.0) == pytest.approx(0.2)
    assert compute_gap(100.0, 100.0) == pytest.approx(0.0)
    # Zero optimum uses the epsilon floor, never divides by zero.
    assert compute_gap(0.0, 0.0) == pytest.approx(0.0)


def test_reference_throttled_above_threshold_labels_bound_based(cfg):
    # Force the 24-drone swarm above the exact-solve threshold.
    cfg.solver.cp_sat_target_threshold = 1
    _, rec = _run(cfg, active="greedy_nearest", enabled=["cp_sat", "greedy_nearest"], max_epochs=2)
    first = rec.epoch_messages[0]
    cps = next(s for s in first.solvers if s.name == "cp_sat")
    # Reference did not run this epoch: not proven optimal, labelled bound-based.
    assert cps.is_optimal is False
    assert cps.gap_is_bound_based is True


def test_only_active_solver_assignment_is_executed(cfg):
    # Running with greedy active vs cp_sat active over the same seed must differ in
    # telemetry only if the assignments differ. Our _FakeExact returns the SAME orders
    # as greedy (only the objective differs), so kills should be identical, proving the
    # race itself does not perturb the executed world.
    r_g, _ = _run(cfg, active="greedy_nearest", enabled=["cp_sat", "greedy_nearest"], max_epochs=40)
    r_c, _ = _run(cfg, active="cp_sat", enabled=["cp_sat", "greedy_nearest"], max_epochs=40)
    assert r_g.summary.kills == r_c.summary.kills
    assert r_g.summary.leaks == r_c.summary.leaks


# --------------------------------------------------------------------------- #
# 3) Telemetry wire shape (pdd.md 12.2)                                        #
# --------------------------------------------------------------------------- #


def test_jsonl_interleaves_frames_and_epochs(cfg):
    _, rec = _run(cfg, max_epochs=4)
    lines = [json.loads(raw) for raw in rec.jsonl().split("\n")]
    types = [m["type"] for m in lines]
    # One frame + one epoch per decision epoch, in that order.
    assert types == ["frame", "epoch"] * 4


def test_beamframe_serializes_from_alias():
    beam = BeamFrame(from_="t1", to="d3", power_frac=0.5)
    dumped = beam.model_dump(by_alias=True)
    assert "from" in dumped and dumped["from"] == "t1"
    assert "from_" not in dumped


def test_frame_message_fractions_clamped_and_beams_drawn(cfg):
    _, rec = _run(cfg, max_epochs=80)
    saw_beam = False
    for msg in rec.frame_messages:
        for tf in msg.turrets:
            assert 0.0 <= tf.thermal_frac <= 1.0
        for df in msg.drones:
            assert 0.0 <= df.hp_frac <= 1.0
        for bf in msg.beams:
            assert 0.0 <= bf.power_frac <= 1.0
            saw_beam = True
    assert saw_beam, "expected at least one beam over an 80-epoch run"


def test_dead_and_leaked_drones_dropped_from_frames(cfg):
    _, rec = _run(cfg, max_epochs=120)
    for msg in rec.frame_messages:
        for df in msg.drones:
            assert df.state in ("alive", "engaged")


# --------------------------------------------------------------------------- #
# 4) End-to-end persistence + summary                                         #
# --------------------------------------------------------------------------- #


def test_run_headless_writes_telemetry_and_summary(cfg, tmp_path):
    result = run_headless(
        cfg,
        active_solver="greedy_nearest",
        enabled_solvers=["greedy_nearest"],
        out_dir=str(tmp_path),
        loop_cfg=LoopConfig(max_epochs=20),
        solver_factory=_factory,
    )
    run_dir = tmp_path / "runs" / "swarm_24"
    assert (run_dir / "telemetry.jsonl").exists()
    assert (run_dir / "summary.json").exists()
    summary_obj = json.loads((run_dir / "summary.json").read_text())
    assert summary_obj["telemetry_hash"] == result.telemetry_hash
    assert summary_obj["summary"]["run_id"] == "swarm_24"


def test_summary_carries_per_solver_stats(cfg):
    result, _ = _run(cfg, active="greedy_nearest", enabled=["cp_sat", "greedy_nearest"], max_epochs=10)
    s = result.summary
    assert "greedy_nearest" in s.avg_solve_ms_by_solver
    assert "cp_sat" in s.avg_solve_ms_by_solver
    # The heuristic has a recorded gap; the reference does not gap itself.
    assert "greedy_nearest" in s.avg_gap_by_solver
    assert s.avg_gap_by_solver["greedy_nearest"] >= 0.0
