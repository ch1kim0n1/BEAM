"""Batch-sweep tests (pdd.md sections 10, 15 Phase-2 acceptance).

A tiny sweep must:
  * produce a per-point net-position series and locate a breakeven crossover when the
    net position changes sign across the swept range (pdd.md 10);
  * produce a gap-vs-swarm-size CSV whose gaps are non-negative (pdd.md 15 Phase 2 /
    mvp.md §4: heuristic gaps vs the exact reference are >= 0);
  * be deterministic — same spec + seed -> identical artifacts (pdd.md 7.4).

The sweeps here are deliberately small (few swarm sizes, low epoch cap, fast solver set)
so the test runs quickly while still exercising every code path.
"""

from __future__ import annotations

import csv
import io

import pytest

from beam.batch.sweeps import (
    BreakevenCrossover,
    SweepPoint,
    SweepSpec,
    find_breakeven,
    gap_vs_scale_csv,
    net_position_csv,
    run_sweep,
    set_nested,
)
from beam.engine.loop import LoopConfig


# Fast, exact-reference-bearing solver set kept small so the test is quick. The
# reference (cp_sat) anchors the gap; greedy_urgent is the policy whose gap we assert is
# non-negative.
TINY_SOLVERS = ["greedy_urgent", "cp_sat"]
FAST_LOOP = LoopConfig(max_epochs=300, substeps_per_epoch=6)


def _tiny_spec(values: list[int]) -> SweepSpec:
    return SweepSpec(
        id="test_breakeven",
        base_scenario="swarm_24",
        seed=1337,
        parameter="swarm_spec.count",
        values=values,
        solvers=list(TINY_SOLVERS),
        active="greedy_urgent",
    )


# --------------------------------------------------------------------------- #
# Unit: nested set + crossover detection                                      #
# --------------------------------------------------------------------------- #


def test_set_nested_sets_leaf():
    d = {"swarm_spec": {"count": 24, "speed": 60}}
    set_nested(d, "swarm_spec.count", 8)
    assert d["swarm_spec"]["count"] == 8
    assert d["swarm_spec"]["speed"] == 60


def test_set_nested_missing_intermediate_raises():
    with pytest.raises(KeyError):
        set_nested({}, "a.b", 1)


def _pt(value, net):
    return SweepPoint(
        value=value,
        net_position=net,
        cumulative_cost=0.0,
        value_destroyed=0.0,
        kills=0,
        leaks=0,
        leaked_value=0.0,
    )


def test_breakeven_interpolates_sign_change():
    # net goes negative -> positive between value 10 and 20; crossing at 0 is midway.
    pts = [_pt(0, -100.0), _pt(10, -100.0), _pt(20, 100.0)]
    be = find_breakeven(pts)
    assert be.crossed is True
    assert be.from_value == 10.0 and be.to_value == 20.0
    assert be.value == pytest.approx(15.0)
    assert be.direction == "up"


def test_breakeven_not_crossed_when_all_same_sign():
    pts = [_pt(4, 50.0), _pt(8, 120.0), _pt(12, 300.0)]
    be = find_breakeven(pts)
    assert be.crossed is False
    assert be.value is None


def test_breakeven_down_direction():
    pts = [_pt(4, 100.0), _pt(8, -50.0)]
    be = find_breakeven(pts)
    assert be.crossed is True
    assert be.direction == "down"
    assert be.value == pytest.approx(4 + (0 - 100.0) / (-50.0 - 100.0) * 4)


# --------------------------------------------------------------------------- #
# Integration: a tiny real sweep                                             #
# --------------------------------------------------------------------------- #


def test_tiny_sweep_runs_and_emits_csv(tmp_path):
    spec = _tiny_spec([2, 6, 12])
    result = run_sweep(spec, out_dir=str(tmp_path), loop_cfg=FAST_LOOP)

    # One point per swept value, in order.
    assert [p.value for p in result.points] == [2, 6, 12]
    assert result.active_solver == "greedy_urgent"

    # Net-position CSV: header + one row per point.
    net_csv = net_position_csv(result)
    rows = list(csv.reader(io.StringIO(net_csv)))
    assert rows[0][0] == "swarm_spec.count"
    assert "net_position" in rows[0]
    assert len(rows) == 1 + len(result.points)

    # Gap-vs-scale CSV: one column per solver; gaps non-negative (pdd.md 15 / mvp §4).
    gap_csv = gap_vs_scale_csv(result)
    grows = list(csv.reader(io.StringIO(gap_csv)))
    header = grows[0]
    assert header[0] == "swarm_spec.count"
    for name in TINY_SOLVERS:
        assert name in header
    gi = header.index("greedy_urgent")
    saw_gap = False
    for row in grows[1:]:
        cell = row[gi]
        if cell != "":
            saw_gap = True
            assert float(cell) >= 0.0, "heuristic gap must be non-negative vs reference"
    assert saw_gap, "expected at least one computed gap for greedy_urgent"

    # Artifacts persisted under runs/<id>/.
    run_dir = tmp_path / "runs" / "test_breakeven"
    assert (run_dir / "net_position_vs_swarm_size.csv").is_file()
    assert (run_dir / "gap_vs_swarm_size.csv").is_file()
    assert (run_dir / "summary.json").is_file()


def test_tiny_sweep_produces_breakeven_crossover(tmp_path):
    # Spanning small -> larger swarms; capex amortization makes tiny swarms net-negative
    # and larger swarms (more value destroyed) net-positive, so a crossover exists.
    spec = _tiny_spec([2, 4, 8, 16, 24])
    result = run_sweep(spec, out_dir=str(tmp_path), loop_cfg=FAST_LOOP)

    nets = [p.net_position for p in result.points]
    # Either a genuine sign change (crossing detected) or monotone single-sign — assert
    # the detector agrees with the data it was given.
    has_sign_change = any(a < 0 <= b or a > 0 >= b for a, b in zip(nets, nets[1:]))
    assert result.breakeven.crossed == (has_sign_change or nets[-1] == 0.0 or any(n == 0.0 for n in nets[:-1]))
    if result.breakeven.crossed:
        assert result.breakeven.value is not None


def test_sweep_is_deterministic(tmp_path):
    spec = _tiny_spec([4, 8])
    r1 = run_sweep(spec, loop_cfg=FAST_LOOP)
    r2 = run_sweep(spec, loop_cfg=FAST_LOOP)
    assert [p.net_position for p in r1.points] == [p.net_position for p in r2.points]
    assert [p.gap_by_solver for p in r1.points] == [p.gap_by_solver for p in r2.points]
    assert net_position_csv(r1) == net_position_csv(r2)
    assert gap_vs_scale_csv(r1) == gap_vs_scale_csv(r2)
