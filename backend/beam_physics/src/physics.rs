use ndarray::{Array1, Array2, ArrayView1, ArrayView2};

const EPS: f64 = 1e-12;

/// Beer-Lambert attenuation: P_delivered[i] = power * exp(-alpha * ranges[i])
pub fn delivered_power_batch(power: f64, alpha: f64, ranges: ArrayView1<f64>) -> Array1<f64> {
    ranges.mapv(|r| power * (-alpha * r).exp())
}

/// Track efficiency: eta[i] = clamp(base * exp(-falloff * ranges[i]), 0, 1)
pub fn track_efficiency_batch(
    ranges: ArrayView1<f64>,
    base: f64,
    range_falloff: f64,
) -> Array1<f64> {
    ranges.mapv(|r| (base * (-range_falloff * r).exp()).clamp(0.0, 1.0))
}

/// Dwell-to-kill: d[i] = e_kill[i] / (power_del[i] * eta[i]), inf when deposition <= EPS
pub fn dwell_to_kill_batch(
    e_kill: ArrayView1<f64>,
    power_del: ArrayView1<f64>,
    eta: ArrayView1<f64>,
) -> Array1<f64> {
    ndarray::Zip::from(&e_kill)
        .and(&power_del)
        .and(&eta)
        .map_collect(|&ek, &p, &e| {
            let dep = p * e;
            if dep > EPS { ek / dep } else { f64::INFINITY }
        })
}

/// Full n_turrets x n_drones dwell-to-kill matrix in one pass.
pub fn dwell_matrix(
    powers: ArrayView1<f64>,
    ranges: ArrayView2<f64>,
    hardness: ArrayView1<f64>,
    alpha: f64,
    track_base: f64,
    track_falloff: f64,
) -> Array2<f64> {
    let n_t = ranges.nrows();
    let n_d = ranges.ncols();
    let mut out = Array2::<f64>::zeros((n_t, n_d));
    for i in 0..n_t {
        for j in 0..n_d {
            let r = ranges[[i, j]];
            let p_del = powers[i] * (-alpha * r).exp();
            let eta = (track_base * (-track_falloff * r).exp()).clamp(0.0, 1.0);
            let dep = p_del * eta;
            out[[i, j]] = if dep > EPS { hardness[j] / dep } else { f64::INFINITY };
        }
    }
    out
}
