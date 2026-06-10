# Rust Hot Loop (PyO3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the numpy physics hot path with a compiled Rust extension for 10-50x speedup at 1000+ drone scale. Pure-Python/numpy fallback preserved when Rust is not compiled.

**Architecture:** New `backend/beam_physics/` Rust crate using PyO3 + numpy crate. `maturin` builds the `.pyd`/`.so` into the venv. Python imports via `try/except ImportError`. The engine's `physics.py` and `kinematics.py` gain a fallback wrapper pattern.

**Tech Stack:** Rust stable, PyO3 0.21, numpy crate 0.21, ndarray 0.15, maturin 1.x

---

## Prerequisites

Install Rust toolchain (if not already present):
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup default stable
```

Install maturin:
```bash
pip install maturin
```

---

## File structure

| File | Action | Purpose |
|---|---|---|
| `backend/beam_physics/Cargo.toml` | Create | Rust crate manifest |
| `backend/beam_physics/src/lib.rs` | Create | PyO3 module registration |
| `backend/beam_physics/src/physics.rs` | Create | Beer-Lambert, dwell-to-kill functions |
| `backend/beam_physics/src/kinematics.rs` | Create | Position update, TTI computation |
| `backend/pyproject.toml` | Modify | Add `[rust]` optional dep group, maturin build config |
| `backend/beam/engine/physics.py` | Modify | Try-import Rust, fallback to numpy |
| `backend/beam/engine/kinematics.py` | Modify | Try-import Rust for position update |
| `backend/tests/test_rust_physics.py` | Create | Rust vs numpy output comparison tests |

---

### Task 1: Rust crate scaffold

**Files:**
- Create: `backend/beam_physics/Cargo.toml`
- Create: `backend/beam_physics/src/lib.rs`
- Create: `backend/beam_physics/src/physics.rs`
- Create: `backend/beam_physics/src/kinematics.rs`

- [ ] **Step 1: Create Cargo.toml**

```toml
# backend/beam_physics/Cargo.toml
[package]
name = "beam_physics"
version = "0.1.0"
edition = "2021"

[lib]
name = "beam_physics"
crate-type = ["cdylib"]

[dependencies]
pyo3 = { version = "0.21", features = ["extension-module", "abi3-py311"] }
numpy = "0.21"
ndarray = { version = "0.15", features = [] }

[profile.release]
opt-level = 3
lto = "thin"
```

- [ ] **Step 2: Create src/physics.rs**

```rust
// backend/beam_physics/src/physics.rs
use ndarray::{Array1, Array2, ArrayView1, ArrayView2};

const EPS: f64 = 1e-12;

/// Beer-Lambert attenuation over a 1D range array.
/// P_delivered[i] = power * exp(-alpha * ranges[i])
pub fn delivered_power_batch(power: f64, alpha: f64, ranges: ArrayView1<f64>) -> Array1<f64> {
    ranges.mapv(|r| power * (-alpha * r).exp())
}

/// Track efficiency over a 1D range array.
/// eta[i] = clamp(base * exp(-falloff * ranges[i]), 0, 1)
pub fn track_efficiency_batch(
    ranges: ArrayView1<f64>,
    base: f64,
    range_falloff: f64,
) -> Array1<f64> {
    ranges.mapv(|r| (base * (-range_falloff * r).exp()).clamp(0.0, 1.0))
}

/// Dwell-to-kill time in seconds over 1D arrays.
/// d[i] = e_kill[i] / (power_del[i] * eta[i])  (inf if denominator <= EPS)
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

/// Full dwell-to-kill matrix: rows=turrets, cols=drones.
/// Combines Beer-Lambert + track efficiency + dwell in one pass.
pub fn dwell_matrix(
    powers: ArrayView1<f64>,      // shape [n_turrets]
    ranges: ArrayView2<f64>,      // shape [n_turrets, n_drones]
    hardness: ArrayView1<f64>,    // shape [n_drones]
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
```

- [ ] **Step 3: Create src/kinematics.rs**

```rust
// backend/beam_physics/src/kinematics.rs
use ndarray::{Array1, Array2, ArrayView1, ArrayView2};

/// Update drone positions (direct behavior): pos += vel * dt
/// pos and vel are [n_drones, 2] arrays; mutates pos in-place via returned array.
pub fn step_direct_batch(
    pos: ArrayView2<f64>,
    vel: ArrayView2<f64>,
    dt: f64,
) -> Array2<f64> {
    &pos + &(vel.to_owned() * dt)
}

/// Compute time-to-impact for each drone.
/// pos: [n, 2], vel: [n, 2], asset: [2]
/// Returns [n] TTI values (inf if not converging).
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
        let ux = dx / dist;
        let uy = dy / dist;
        let closing = vel[[i, 0]] * ux + vel[[i, 1]] * uy;
        if closing > 1e-12 {
            tti[i] = dist / closing;
        }
    }
    tti
}
```

- [ ] **Step 4: Create src/lib.rs**

```rust
// backend/beam_physics/src/lib.rs
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
```

- [ ] **Step 5: Build the extension to verify it compiles**

```bash
cd backend/beam_physics && maturin develop --release
```

Expected: `beam_physics` is importable from Python after this

- [ ] **Step 6: Verify import**

```bash
cd backend && python -c "import beam_physics; print('Rust OK:', beam_physics.__doc__)"
```

Expected: no error

- [ ] **Step 7: Commit**

```bash
git add backend/beam_physics/
git commit -m "feat: beam_physics Rust crate with physics/kinematics hot-path functions"
```

---

### Task 2: pyproject.toml build config

**Files:**
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: Add maturin build config and optional rust dep**

In `backend/pyproject.toml`, add or merge:

```toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[tool.maturin]
manifest-path = "beam_physics/Cargo.toml"
python-packages = ["beam"]
features = ["pyo3/extension-module"]

[project.optional-dependencies]
rust = ["maturin>=1.0,<2.0"]
```

Note: if `pyproject.toml` already uses a different build backend (e.g. `hatchling`), keep that backend for the Python package and document that `maturin develop` must be run separately to build the Rust extension. Only the `[project.optional-dependencies]` and `[tool.maturin]` sections are needed; do not replace the existing build backend.

- [ ] **Step 2: Commit**

```bash
git add backend/pyproject.toml
git commit -m "build: add maturin config and [rust] optional dep group"
```

---

### Task 3: Python fallback wrappers in physics.py

**Files:**
- Modify: `backend/beam/engine/physics.py`
- Create: `backend/tests/test_rust_physics.py`

- [ ] **Step 1: Write failing test**

```python
# backend/tests/test_rust_physics.py
import pytest
import numpy as np

# Tests run regardless of whether Rust extension is compiled.
# When Rust is present: verifies parity with numpy. When absent: tests numpy fallbacks.

try:
    import beam_physics as _rust
    HAS_RUST = True
except ImportError:
    HAS_RUST = False


def test_delivered_power_matches_numpy():
    """Rust delivered_power_batch matches numpy formula."""
    from beam.engine.physics import delivered_power
    ranges = np.array([100.0, 500.0, 1000.0, 2000.0])
    np_result = delivered_power(100.0, 0.002, ranges)

    if HAS_RUST:
        rust_result = _rust.delivered_power_batch(100.0, 0.002, ranges)
        np.testing.assert_allclose(rust_result, np_result, rtol=1e-10)
    else:
        # Just verify numpy path works
        assert np.all(np_result > 0)
        assert np_result[0] > np_result[-1]  # monotone decreasing


def test_dwell_to_kill_inf_when_deposition_zero():
    """dwell_to_kill returns inf for zero deposition (no Rust required)."""
    from beam.engine.physics import dwell_to_kill
    result = dwell_to_kill(np.array([40.0]), np.array([0.0]))
    assert np.isinf(result[0])


@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not compiled")
def test_rust_dwell_matrix_shape():
    """Rust dwell_matrix returns correct shape."""
    powers = np.array([100.0, 100.0])
    ranges = np.array([[500.0, 1000.0, 1500.0], [600.0, 900.0, 1200.0]])
    hardness = np.array([40.0, 40.0, 120.0])
    result = _rust.dwell_matrix(powers, ranges, hardness, 0.002, 1.0, 0.0015)
    assert result.shape == (2, 3)
    assert np.all(result > 0)


@pytest.mark.skipif(not HAS_RUST, reason="Rust extension not compiled")
def test_rust_step_direct_batch():
    """Rust position update matches numpy explicit Euler."""
    pos = np.array([[0.0, 0.0], [100.0, 50.0]])
    vel = np.array([[-10.0, 0.0], [-5.0, -5.0]])
    dt = 0.1
    expected = pos + vel * dt
    result = _rust.step_direct_batch(pos, vel, dt)
    np.testing.assert_allclose(result, expected, rtol=1e-12)
```

- [ ] **Step 2: Run tests**

```bash
cd backend && python -m pytest tests/test_rust_physics.py -v
```

Expected: all PASS (Rust tests skipped if not compiled, numpy tests always pass)

- [ ] **Step 3: Add try-import to physics.py**

At the top of `backend/beam/engine/physics.py`, after imports, add:

```python
# Try to use the compiled Rust extension for hot-path functions.
# Falls back to numpy when the extension is not built (e.g. CI without Rust toolchain).
try:
    import beam_physics as _rust_physics  # type: ignore[import]
    _RUST = True
except ImportError:
    _rust_physics = None
    _RUST = False
```

Then add a batch wrapper that uses Rust when available:

```python
def delivered_power_batch(
    power_emitted: float,
    alpha: float,
    ranges: np.ndarray,
) -> np.ndarray:
    """Vectorized Beer-Lambert over a range array. Uses Rust extension when compiled."""
    if _RUST and _rust_physics is not None:
        return _rust_physics.delivered_power_batch(float(power_emitted), float(alpha), np.asarray(ranges, dtype=np.float64))
    # Numpy fallback (existing logic).
    return delivered_power(power_emitted, alpha, ranges)
```

Add similar wrappers for `dwell_to_kill_batch_fast` (calls `_rust_physics.dwell_matrix`).

- [ ] **Step 4: Run all backend tests with and without Rust**

With Rust:
```bash
cd backend && python -m pytest tests/ -v
```

Without Rust (simulate by temporarily renaming the extension):
```bash
cd backend && python -c "
import sys
sys.modules['beam_physics'] = None  # simulate missing
from beam.engine import physics
print('fallback active:', not physics._RUST)
"
```

Expected: both paths work

- [ ] **Step 5: Commit**

```bash
git add backend/beam/engine/physics.py backend/tests/test_rust_physics.py
git commit -m "feat: try-import Rust physics extension with numpy fallback"
```

---

### Task 4: Kinematics try-import

**Files:**
- Modify: `backend/beam/engine/kinematics.py`

- [ ] **Step 1: Add try-import at top of kinematics.py**

```python
try:
    import beam_physics as _rust_kin  # type: ignore[import]
    _RUST_KIN = True
except ImportError:
    _rust_kin = None
    _RUST_KIN = False
```

- [ ] **Step 2: Add Rust-accelerated step_direct**

After the existing `step_direct()` function, add:

```python
def step_direct_batch_fast(
    pos: np.ndarray,
    vel: np.ndarray,
    dt: float,
) -> np.ndarray:
    """Fast vectorized direct step. Uses Rust when compiled, numpy otherwise."""
    if _RUST_KIN and _rust_kin is not None:
        return _rust_kin.step_direct_batch(
            np.asarray(pos, dtype=np.float64),
            np.asarray(vel, dtype=np.float64),
            float(dt),
        )
    return pos + vel * dt
```

Note: the engine's `step_direct()` currently mutates `Drone` objects in a Python loop (line 229-238 in kinematics.py). For the Rust speedup to kick in, the caller must use the batch form. The engine loop in `loop.py` calls `step_swarm()` which calls `step_direct()`. Replacing the inner loop with a batch call requires extracting pos/vel arrays, calling `step_direct_batch_fast`, and writing back to drone objects. This is a separate sub-task:

In `step_direct()`, after `movable` is built:
```python
def step_direct(drones: list[Drone], asset_pos: Vec2, dt: float) -> None:
    movable = [d for d in drones if _is_movable(d)]
    if not movable:
        return
    pos = np.array([[d.pos.x, d.pos.y] for d in movable], dtype=np.float64)
    vel = np.array([[d.vel.x, d.vel.y] for d in movable], dtype=np.float64)
    # Re-aim velocities toward asset
    to_asset = np.array([asset_pos.x, asset_pos.y]) - pos
    dists = np.hypot(to_asset[:, 0], to_asset[:, 1])
    dists = np.where(dists > 1e-12, dists, 1.0)
    unit = to_asset / dists[:, None]
    speeds = np.hypot(vel[:, 0], vel[:, 1])
    vel = unit * speeds[:, None]
    # Rust-accelerated position update
    new_pos = step_direct_batch_fast(pos, vel, dt)
    for k, d in enumerate(movable):
        d.vel = Vec2(x=float(vel[k, 0]), y=float(vel[k, 1]))
        d.pos = Vec2(x=float(new_pos[k, 0]), y=float(new_pos[k, 1]))
```

- [ ] **Step 3: Run all backend tests**

```bash
cd backend && python -m pytest tests/ -v
```

Expected: all pass (including reproducibility tests — output must be identical to pre-Rust)

- [ ] **Step 4: Commit**

```bash
git add backend/beam/engine/kinematics.py
git commit -m "feat: vectorized step_direct with Rust-accelerated batch update"
```

---

### Task 5: CI integration

**Files:**
- Modify: `.github/workflows/` (existing CI pipeline file)

- [ ] **Step 1: Add Rust build step to CI**

In the existing GitHub Actions workflow (`.github/workflows/ci.yml` or similar), add a step before Python tests:

```yaml
      - name: Install Rust stable
        uses: dtolnay/rust-toolchain@stable

      - name: Build Rust extension
        working-directory: backend
        run: |
          pip install maturin
          cd beam_physics && maturin develop --release
```

And add a separate job that runs tests WITHOUT Rust to verify the fallback:

```yaml
  test-no-rust:
    name: Python tests (numpy fallback)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install Python deps (no Rust)
        working-directory: backend
        run: pip install -e "."
      - name: Run tests
        working-directory: backend
        run: python -m pytest tests/ -v
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/
git commit -m "ci: add Rust build step and no-rust fallback test job"
```

---

## Self-review checklist

- [x] PyO3 `abi3-py311` feature — the `.so` works on Python 3.11+ without recompiling
- [x] Rust tests are `#[cfg(test)]` optional — the crate compiles in release without them
- [x] `_RUST` flag checked before every call — no AttributeError when extension missing
- [x] `step_direct` refactored to vectorized form — must run reproducibility test (`pytest -k repro`) to confirm telemetry hash unchanged after this refactor
- [x] `ndarray` version 0.15 compatible with `numpy` crate 0.21 — verify in Cargo.lock after build
- [x] CI workflow path — check the actual CI filename in `.github/workflows/` before editing; the existing workflow is at `.github/workflows/ci.yml` (verify with `ls .github/workflows/`)
- [x] `maturin develop` writes the extension to the active venv — ensure CI's pip install and maturin develop share the same venv (they do when run sequentially in the same step)
