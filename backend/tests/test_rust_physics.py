"""Tests for Rust hot-loop fallback wrappers.

When the Rust extension (beam_physics) is compiled and installed, verifies that
Rust output matches numpy output. When not compiled, verifies the numpy fallbacks
work correctly. Tests always pass regardless of whether Rust is available.
"""

import numpy as np
import pytest

try:
    import beam_physics as _rust
    HAS_RUST = True
except ImportError:
    HAS_RUST = False


def test_delivered_power_fallback_monotone():
    """Numpy fallback: delivered power decreases with range."""
    from beam.engine.physics import delivered_power_batch
    ranges = np.array([100.0, 500.0, 1000.0, 2000.0])
    result = delivered_power_batch(100.0, 0.002, ranges)
    assert np.all(result > 0)
    assert result[0] > result[-1]


def test_dwell_to_kill_inf_when_zero_deposition():
    """dwell_to_kill returns inf when deposition is zero."""
    from beam.engine.physics import dwell_to_kill
    result = dwell_to_kill(np.array([40.0]), np.array([0.0]))
    assert np.isinf(result[0])


@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not compiled")
def test_rust_delivered_power_matches_numpy():
    """Rust delivered_power_batch matches numpy formula exactly."""
    from beam.engine.physics import delivered_power
    ranges = np.array([100.0, 500.0, 1000.0, 2000.0])
    np_result = np.asarray(delivered_power(100.0, 0.002, ranges))
    rust_result = _rust.delivered_power_batch(100.0, 0.002, ranges)
    np.testing.assert_allclose(rust_result, np_result, rtol=1e-10)


@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not compiled")
def test_rust_dwell_matrix_shape():
    """Rust dwell_matrix returns correct (n_turrets, n_drones) shape."""
    powers = np.array([100.0, 100.0])
    ranges = np.array([[500.0, 1000.0, 1500.0], [600.0, 900.0, 1200.0]])
    hardness = np.array([40.0, 40.0, 120.0])
    result = _rust.dwell_matrix(powers, ranges, hardness, 0.002, 1.0, 0.0015)
    assert result.shape == (2, 3)
    assert np.all(result > 0)


@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not compiled")
def test_rust_step_direct_batch_matches_numpy():
    """Rust step_direct_batch matches explicit Euler pos += vel * dt."""
    pos = np.array([[0.0, 0.0], [100.0, 50.0]])
    vel = np.array([[-10.0, 0.0], [-5.0, -5.0]])
    dt = 0.1
    expected = pos + vel * dt
    result = _rust.step_direct_batch(pos, vel, dt)
    np.testing.assert_allclose(result, expected, rtol=1e-12)


def test_kinematics_step_direct_vectorized():
    """Vectorized step_direct produces expected direct-behavior motion."""
    from beam.engine.kinematics import step_direct
    from beam.schemas import Drone, Vec2
    drone = Drone(
        id="d0", pos=Vec2(x=1000.0, y=0.0), vel=Vec2(x=-18.0, y=0.0),
        value=2000.0, hardness=40.0, class_name="quad_small",
    )
    asset = Vec2(x=0.0, y=0.0)
    step_direct([drone], asset, dt=0.5)
    # Drone should have moved toward the asset
    assert drone.pos.x < 1000.0
    assert abs(drone.pos.y) < 1e-6
