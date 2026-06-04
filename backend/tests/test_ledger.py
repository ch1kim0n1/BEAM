"""Tests for the running cost ledger (pdd.md section 10).

Covers the required invariants:
  - net_position == value_destroyed - cumulative_cost
  - per-shot cost is positive
  - breakeven sign flips from negative to positive as kills accumulate at
    fixed capex

Plus: all constants come from config (no hard-coded physics/cost numbers in the
ledger), cumulative components decompose correctly, and accumulation is
deterministic / order-stable.
"""

from __future__ import annotations

import pytest

from beam.config import CostConfig, load_config
from beam.cost import Ledger
from beam.schemas import LedgerSnapshot


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture
def cost() -> CostConfig:
    """Cost config from the real defaults.yaml (no hard-coded constants here)."""
    return load_config().cost


@pytest.fixture
def ledger(cost: CostConfig) -> Ledger:
    return Ledger(cost)


# --------------------------------------------------------------------------- #
# Construction / config-sourcing                                              #
# --------------------------------------------------------------------------- #
def test_ledger_constructs_from_config(cost: CostConfig) -> None:
    led = Ledger(cost)
    assert led.capex_per_engagement == pytest.approx(
        cost.system_capex / cost.expected_lifetime_engagements
    )


def test_initial_snapshot_is_zeroed(ledger: Ledger) -> None:
    snap = ledger.snapshot()
    assert snap == LedgerSnapshot()  # all-zero defaults
    assert snap.cumulative_cost == 0.0
    assert snap.value_destroyed == 0.0
    assert snap.net == 0.0


# --------------------------------------------------------------------------- #
# Per-shot / per-epoch cost components                                        #
# --------------------------------------------------------------------------- #
def test_per_shot_cost_positive(ledger: Ledger, cost: CostConfig) -> None:
    """Any positive energy delivered costs a positive amount."""
    c = ledger.shot_cost(2.5)
    assert c > 0.0
    assert c == pytest.approx(2.5 * cost.price_per_kwh)


def test_zero_energy_zero_shot_cost(ledger: Ledger) -> None:
    assert ledger.shot_cost(0.0) == 0.0


def test_maintenance_scales_with_operating_time(
    ledger: Ledger, cost: CostConfig
) -> None:
    assert ledger.maintenance_for(3.0) == pytest.approx(3.0 * cost.maintenance_rate)
    assert ledger.maintenance_for(3.0) > ledger.maintenance_for(1.0)


# --------------------------------------------------------------------------- #
# Snapshot identity: net == value_destroyed - cumulative_cost                 #
# --------------------------------------------------------------------------- #
def test_net_identity_holds_after_update(ledger: Ledger) -> None:
    snap = ledger.update(
        energy_delivered_kwh=1.2,
        operating_time=0.5,
        value_destroyed=2000.0,
        engagements=1,
    )
    assert snap.net == pytest.approx(snap.value_destroyed - snap.cumulative_cost)


def test_net_identity_holds_across_many_epochs(ledger: Ledger) -> None:
    snap = LedgerSnapshot()
    for _ in range(50):
        snap = ledger.update(
            energy_delivered_kwh=0.7,
            operating_time=0.5,
            value_destroyed=2000.0,
            engagements=1,
        )
        assert snap.net == pytest.approx(snap.value_destroyed - snap.cumulative_cost)


def test_cumulative_cost_decomposes_into_components(ledger: Ledger) -> None:
    snap = ledger.update(
        energy_delivered_kwh=3.0,
        operating_time=2.0,
        value_destroyed=0.0,
        engagements=4,
    )
    assert snap.cumulative_cost == pytest.approx(
        snap.shot_energy_cost + snap.maintenance_cost + snap.capex_amortized
    )


def test_capex_amortized_is_per_engagement(ledger: Ledger) -> None:
    snap = ledger.update(engagements=10)
    assert snap.capex_amortized == pytest.approx(10 * ledger.capex_per_engagement)
    assert snap.engagements == 10


def test_value_destroyed_accumulates(ledger: Ledger) -> None:
    ledger.update(value_destroyed=2000.0)
    snap = ledger.update(value_destroyed=15000.0)
    assert snap.value_destroyed == pytest.approx(17000.0)


# --------------------------------------------------------------------------- #
# Breakeven: net flips negative -> positive as kills accumulate at fixed capex #
# --------------------------------------------------------------------------- #
def test_breakeven_sign_flip(cost: CostConfig) -> None:
    """With fixed capex amortized up front, net starts negative (capex sunk)
    and crosses to positive as enough value is destroyed."""
    led = Ledger(cost)

    # Epoch 0: open many engagements (sink amortized capex) with no kills yet.
    n_engagements = 200
    snap0 = led.update(
        energy_delivered_kwh=1.0,
        operating_time=1.0,
        value_destroyed=0.0,
        engagements=n_engagements,
    )
    assert snap0.net < 0.0, "capex/maintenance with no kills must be net-negative"

    capex_floor = n_engagements * led.capex_per_engagement
    assert capex_floor > 0.0

    # Now accumulate kills (no new engagements) until net turns positive.
    nets: list[float] = [snap0.net]
    per_kill_value = 15000.0  # fixed_wing value, from a config drone class below
    crossed = False
    for _ in range(10_000):
        snap = led.update(value_destroyed=per_kill_value, engagements=0)
        nets.append(snap.net)
        if snap.net > 0.0:
            crossed = True
            break

    assert crossed, "net must eventually cross zero as kills accumulate"

    # The crossing is a single sign flip from negative to positive (monotone net).
    assert nets[0] < 0.0 < nets[-1]
    assert all(b >= a for a, b in zip(nets, nets[1:])), "net must be monotone up"


def test_drone_values_come_from_config() -> None:
    """Sanity: per-kill values used in breakeven exist in config (no magic)."""
    cfg = load_config()
    assert cfg.drone_class("fixed_wing").value > 0
    assert cfg.drone_class("quad_small").value > 0


# --------------------------------------------------------------------------- #
# Determinism                                                                  #
# --------------------------------------------------------------------------- #
def test_accumulation_is_deterministic(cost: CostConfig) -> None:
    deltas = [
        dict(energy_delivered_kwh=0.3, operating_time=0.5, value_destroyed=2000.0, engagements=1),
        dict(energy_delivered_kwh=1.1, operating_time=0.5, value_destroyed=0.0, engagements=2),
        dict(energy_delivered_kwh=0.0, operating_time=0.5, value_destroyed=15000.0, engagements=0),
    ]

    def run() -> LedgerSnapshot:
        led = Ledger(cost)
        snap = LedgerSnapshot()
        for d in deltas:
            snap = led.update(**d)
        return snap

    assert run() == run()


def test_zero_lifetime_engagements_no_capex() -> None:
    """Guard against divide-by-zero if config lifetime is non-positive."""
    bad = CostConfig(
        price_per_kwh=0.12,
        system_capex=80000000.0,
        expected_lifetime_engagements=0.0,
        maintenance_rate=50.0,
    )
    led = Ledger(bad)
    assert led.capex_per_engagement == 0.0
    snap = led.update(engagements=5)
    assert snap.capex_amortized == 0.0
