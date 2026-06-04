"""Scaffold contract tests.

These guard the shared contract every downstream agent builds against: the schemas
import and instantiate, the config loads into typed objects, the solver registry +
Protocol behave, and the util helpers are deterministic. They do NOT test engine,
solver, api, or cost logic (those agents write their own tests).
"""

from __future__ import annotations

import math

import pytest

from beam.config import BeamConfig, load_config, load_sweep
from beam.schemas import (
    SCHEMA_VERSION,
    Assignment,
    BeamFrame,
    ControlMessage,
    Drone,
    EpochMessage,
    EpochLedger,
    FrameMessage,
    LedgerSnapshot,
    ThermalConfig,
    Turret,
    Vec2,
    WorldState,
)
from beam.solvers import REGISTRY, Solver, register
from beam.util import angular_distance, make_rng, vdist, wrap_angle


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #


def _make_turret(tid: str = "t1") -> Turret:
    return Turret(
        id=tid,
        pos=Vec2(x=0, y=0),
        aim=0.0,
        slew_rate=1.2,
        settle_time=0.1,
        power=100.0,
        range_max=5000.0,
        thermal=0.0,
        thermal_cfg=ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100, h_resume=30),
    )


def _make_drone(did: str = "d1") -> Drone:
    return Drone(
        id=did,
        pos=Vec2(x=100, y=0),
        vel=Vec2(x=-10, y=0),
        value=2000.0,
        hardness=40.0,
        class_name="quad_small",
    )


def test_core_entities_instantiate():
    t = _make_turret()
    d = _make_drone()
    assert t.state == "idle" and t.current_target is None
    assert d.state == "alive" and d.energy_absorbed == 0.0


def test_worldstate_and_assignment_shapes():
    state = WorldState(
        t=12.0,
        drones=[_make_drone("d1")],
        turrets=[_make_turret("t1")],
        weather_alpha=0.002,
        asset_pos=Vec2(x=0, y=0),
    )
    assert state.weather_alpha == 0.002
    a = Assignment(turret_orders={"t1": ["d1"]}, objective_estimate=2000.0)
    assert a.turret_orders["t1"] == ["d1"]


def test_telemetry_models_carry_schema_version_and_alias():
    frame = FrameMessage(
        t=1.0,
        beams=[BeamFrame(**{"from": "t1", "to": "d1", "power_frac": 0.9})],
    )
    assert frame.type == "frame"
    assert frame.schema_version == SCHEMA_VERSION
    # "from" alias round-trips on the wire.
    dumped = frame.model_dump(by_alias=True)
    assert dumped["beams"][0]["from"] == "t1"

    epoch = EpochMessage(
        t=12.0,
        epoch=24,
        active_solver="auction",
        ledger=EpochLedger(cumulative_cost=1.0, value_destroyed=2.0, net=1.0),
    )
    assert epoch.type == "epoch" and epoch.schema_version == SCHEMA_VERSION


def test_control_message_validates_action():
    ControlMessage(action="set_solver", solver="ga")
    ControlMessage(action="set_speed", multiplier=2.0)
    ControlMessage(action="pause")
    with pytest.raises(Exception):
        ControlMessage(action="not_an_action")


def test_ledger_snapshot_defaults():
    snap = LedgerSnapshot()
    assert snap.cumulative_cost == 0.0 and snap.net == 0.0


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #


def test_load_defaults():
    cfg = load_config()
    assert isinstance(cfg, BeamConfig)
    assert cfg.sim.seed == 1337
    assert cfg.weather("clear").alpha == pytest.approx(0.002)
    assert cfg.weather("clear").name == "clear"  # name injected from key
    assert cfg.drone_class("quad_small").hardness == 40
    assert cfg.turret_defaults.thermal.h_max == 100


def test_load_scenario_overlay():
    cfg = load_config(scenario="swarm_24")
    assert cfg.scenario is not None
    assert cfg.scenario["id"] == "swarm_24"
    assert cfg.scenario["swarm_spec"]["count"] == 24


def test_load_sweep_spec():
    spec = load_sweep("breakeven")
    assert spec["sweep"]["parameter"] == "swarm_spec.count"
    assert "cp_sat" in spec["solvers"]


# --------------------------------------------------------------------------- #
# Solver registry + Protocol                                                   #
# --------------------------------------------------------------------------- #


def test_register_and_protocol():
    @register("__test_dummy_solver__")
    class _Dummy:
        name = "__test_dummy_solver__"

        def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
            return Assignment()

    try:
        assert "__test_dummy_solver__" in REGISTRY
        inst = REGISTRY["__test_dummy_solver__"]()
        assert isinstance(inst, Solver)  # runtime_checkable Protocol
        assert isinstance(inst.solve(_dummy_state(), 100), Assignment)
    finally:
        REGISTRY.pop("__test_dummy_solver__", None)


def test_register_rejects_duplicates():
    @register("__dup__")
    class _A:
        name = "__dup__"

        def solve(self, state, deadline_ms):  # pragma: no cover
            return Assignment()

    try:
        with pytest.raises(ValueError):

            @register("__dup__")
            class _B:
                name = "__dup__"

                def solve(self, state, deadline_ms):  # pragma: no cover
                    return Assignment()

    finally:
        REGISTRY.pop("__dup__", None)


def _dummy_state() -> WorldState:
    return WorldState(
        t=0.0, drones=[], turrets=[], weather_alpha=0.002, asset_pos=Vec2()
    )


# --------------------------------------------------------------------------- #
# Util                                                                         #
# --------------------------------------------------------------------------- #


def test_rng_is_seeded_and_reproducible():
    a = make_rng(1337).random(5)
    b = make_rng(1337).random(5)
    assert (a == b).all()


def test_angular_distance_and_wrap():
    assert angular_distance(0.0, math.pi) == pytest.approx(math.pi)
    assert angular_distance(0.1, 0.1) == pytest.approx(0.0)
    # symmetric and bounded by pi
    assert angular_distance(0.0, 1.5 * math.pi) == pytest.approx(0.5 * math.pi)
    assert wrap_angle(3.0 * math.pi) == pytest.approx(math.pi)


def test_vdist():
    assert vdist(Vec2(x=0, y=0), Vec2(x=3, y=4)) == pytest.approx(5.0)
