# backend/tests/test_pareto.py
import pytest
from beam.solvers.cp_sat import CpSatSolver
from beam.schemas import WorldState, Vec2, Drone, Turret, ThermalConfig


def _minimal_state() -> WorldState:
    turret = Turret(
        id="t0", pos=Vec2(x=0, y=0), aim=0.0, slew_rate=2.0, settle_time=0.05,
        power=100.0, range_max=5000.0, thermal=0.0,
        thermal_cfg=ThermalConfig(heat_rate=1.0, cool_rate=0.4, h_max=100.0, h_resume=30.0),
    )
    drone = Drone(
        id="d0", pos=Vec2(x=500, y=0), vel=Vec2(x=-10, y=0),
        value=2000.0, hardness=40.0, class_name="quad_small",
    )
    return WorldState(t=0.0, drones=[drone], turrets=[turret], weather_alpha=0.002,
                      asset_pos=Vec2(x=0, y=0))


def test_cp_sat_solve_with_zero_cost_weight():
    solver = CpSatSolver(seed=42)
    state = _minimal_state()
    result_default = solver.solve(state, deadline_ms=500)
    result_zero = solver.solve(state, deadline_ms=500, cost_weight=0.0)
    assert result_default.objective_estimate == pytest.approx(result_zero.objective_estimate)


def test_cp_sat_solve_with_nonzero_cost_weight():
    solver = CpSatSolver(seed=42)
    state = _minimal_state()
    result = solver.solve(state, deadline_ms=500, cost_weight=0.5)
    assert result.objective_estimate >= 0.0
