use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

mod kinematics;
mod physics;

#[pyfunction]
fn delivered_power_batch<'py>(
    py: Python<'py>,
    power: f64,
    alpha: f64,
    ranges: PyReadonlyArray1<f64>,
) -> &'py PyArray1<f64> {
    physics::delivered_power_batch(power, alpha, ranges.as_array()).into_pyarray(py)
}

#[pyfunction]
fn track_efficiency_batch<'py>(
    py: Python<'py>,
    ranges: PyReadonlyArray1<f64>,
    base: f64,
    range_falloff: f64,
) -> &'py PyArray1<f64> {
    physics::track_efficiency_batch(ranges.as_array(), base, range_falloff).into_pyarray(py)
}

#[pyfunction]
fn dwell_to_kill_batch<'py>(
    py: Python<'py>,
    e_kill: PyReadonlyArray1<f64>,
    power_del: PyReadonlyArray1<f64>,
    eta: PyReadonlyArray1<f64>,
) -> &'py PyArray1<f64> {
    physics::dwell_to_kill_batch(e_kill.as_array(), power_del.as_array(), eta.as_array())
        .into_pyarray(py)
}

#[pyfunction]
fn dwell_matrix<'py>(
    py: Python<'py>,
    powers: PyReadonlyArray1<f64>,
    ranges: PyReadonlyArray2<f64>,
    hardness: PyReadonlyArray1<f64>,
    alpha: f64,
    track_base: f64,
    track_falloff: f64,
) -> &'py PyArray2<f64> {
    physics::dwell_matrix(
        powers.as_array(),
        ranges.as_array(),
        hardness.as_array(),
        alpha,
        track_base,
        track_falloff,
    )
    .into_pyarray(py)
}

#[pyfunction]
fn step_direct_batch<'py>(
    py: Python<'py>,
    pos: PyReadonlyArray2<f64>,
    vel: PyReadonlyArray2<f64>,
    dt: f64,
) -> &'py PyArray2<f64> {
    kinematics::step_direct_batch(pos.as_array(), vel.as_array(), dt).into_pyarray(py)
}

#[pyfunction]
fn compute_tti_batch<'py>(
    py: Python<'py>,
    pos: PyReadonlyArray2<f64>,
    vel: PyReadonlyArray2<f64>,
    asset: PyReadonlyArray1<f64>,
) -> &'py PyArray1<f64> {
    kinematics::compute_tti_batch(pos.as_array(), vel.as_array(), asset.as_array())
        .into_pyarray(py)
}

#[pymodule]
fn beam_physics(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(delivered_power_batch, m)?)?;
    m.add_function(wrap_pyfunction!(track_efficiency_batch, m)?)?;
    m.add_function(wrap_pyfunction!(dwell_to_kill_batch, m)?)?;
    m.add_function(wrap_pyfunction!(dwell_matrix, m)?)?;
    m.add_function(wrap_pyfunction!(step_direct_batch, m)?)?;
    m.add_function(wrap_pyfunction!(compute_tti_batch, m)?)?;
    Ok(())
}
