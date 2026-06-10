use ndarray::{Array1, Array2, ArrayView1, ArrayView2};

/// Direct behavior position update: new_pos = pos + vel * dt
pub fn step_direct_batch(pos: ArrayView2<f64>, vel: ArrayView2<f64>, dt: f64) -> Array2<f64> {
    &pos + &(vel.to_owned() * dt)
}

/// Time-to-impact for each drone: pos [n,2], vel [n,2], asset [2] -> tti [n]
pub fn compute_tti_batch(
    pos: ArrayView2<f64>,
    vel: ArrayView2<f64>,
    asset: ArrayView1<f64>,
) -> Array1<f64> {
    let n = pos.nrows();
    let mut tti = Array1::<f64>::from_elem(n, f64::INFINITY);
    for i in 0..n {
        let dx = asset[0] - pos[[i, 0]];
        let dy = asset[1] - pos[[i, 1]];
        let dist = (dx * dx + dy * dy).sqrt();
        if dist <= 1e-12 {
            tti[i] = 0.0;
            continue;
        }
        let closing = vel[[i, 0]] * (dx / dist) + vel[[i, 1]] * (dy / dist);
        if closing > 1e-12 {
            tti[i] = dist / closing;
        }
    }
    tti
}
