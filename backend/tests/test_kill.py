"""Tests for kill resolution + leak detection (beam.engine.kill, pdd.md 8.6 / 7.2).

Covers the four scenarios called out by the task:
  - an engaged-in-time drone dies,
  - an un-engaged drone leaks,
  - a beam break loses progress by default,
  - partial retention keeps progress when enabled.
Plus determinism and edge cases (clamping, no double state flips, fixed order).
"""

from __future__ import annotations

import pytest

from beam.engine.kill import (
    KillParams,
    break_beam,
    deposit_energy,
    resolve_leaks,
    resolve_step,
)
from beam.schemas import Drone, Vec2


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #


def make_drone(
    drone_id: str = "d1",
    *,
    pos: tuple[float, float] = (1000.0, 0.0),
    hardness: float = 40.0,
    value: float = 2000.0,
    state: str = "alive",
    energy_absorbed: float = 0.0,
) -> Drone:
    return Drone(
        id=drone_id,
        pos=Vec2(x=pos[0], y=pos[1]),
        vel=Vec2(x=0.0, y=0.0),
        value=value,
        hardness=hardness,
        class_name="quad_small",
        state=state,  # type: ignore[arg-type]
        energy_absorbed=energy_absorbed,
    )


ASSET = Vec2(x=0.0, y=0.0)


# --------------------------------------------------------------------------- #
# deposit_energy: engaged-in-time drone dies                                   #
# --------------------------------------------------------------------------- #


def test_engaged_drone_dies_when_energy_reaches_e_kill():
    d = make_drone(hardness=40.0)
    killed = deposit_energy(d, 40.0)
    assert killed is True
    assert d.state == "dead"
    assert d.energy_absorbed == 40.0


def test_partial_deposit_marks_engaged_not_dead():
    d = make_drone(hardness=40.0)
    killed = deposit_energy(d, 25.0)
    assert killed is False
    assert d.state == "engaged"
    assert d.energy_absorbed == 25.0


def test_accumulated_deposits_kill_across_substeps():
    d = make_drone(hardness=40.0)
    assert deposit_energy(d, 15.0) is False
    assert d.state == "engaged"
    assert deposit_energy(d, 15.0) is False
    killed = deposit_energy(d, 15.0)  # 45 >= 40
    assert killed is True
    assert d.state == "dead"
    # Over-deposit is clamped to E_kill, not carried.
    assert d.energy_absorbed == 40.0


def test_deposit_on_dead_drone_is_noop():
    d = make_drone(hardness=40.0, state="dead", energy_absorbed=40.0)
    assert deposit_energy(d, 100.0) is False
    assert d.state == "dead"
    assert d.energy_absorbed == 40.0


def test_negative_energy_rejected():
    d = make_drone()
    with pytest.raises(ValueError):
        deposit_energy(d, -1.0)


# --------------------------------------------------------------------------- #
# Leaks: un-engaged drone reaching the asset                                   #
# --------------------------------------------------------------------------- #


def test_unengaged_drone_at_asset_leaks():
    params = KillParams()  # leak_radius=0.0 -> exact reach
    d = make_drone(pos=(0.0, 0.0))
    leaked = resolve_leaks([d], ASSET, params)
    assert leaked == [d]
    assert d.state == "leaked"


def test_drone_outside_leak_radius_does_not_leak():
    params = KillParams(leak_radius=50.0)
    d = make_drone(pos=(100.0, 0.0))
    leaked = resolve_leaks([d], ASSET, params)
    assert leaked == []
    assert d.state == "alive"


def test_drone_within_leak_radius_leaks():
    params = KillParams(leak_radius=50.0)
    d = make_drone(pos=(30.0, 40.0))  # dist 50 == radius -> reached
    leaked = resolve_leaks([d], ASSET, params)
    assert leaked == [d]
    assert d.state == "leaked"


def test_dead_drone_does_not_leak():
    params = KillParams(leak_radius=100.0)
    d = make_drone(pos=(0.0, 0.0), state="dead")
    leaked = resolve_leaks([d], ASSET, params)
    assert leaked == []
    assert d.state == "dead"


# --------------------------------------------------------------------------- #
# Beam break: default loses progress; retention keeps it                       #
# --------------------------------------------------------------------------- #


def test_beam_break_default_loses_progress():
    params = KillParams(partial_energy_retention=False)
    d = make_drone(hardness=40.0, state="engaged", energy_absorbed=30.0)
    break_beam(d, params)
    assert d.energy_absorbed == 0.0
    assert d.state == "alive"


def test_beam_break_retention_keeps_progress():
    params = KillParams(partial_energy_retention=True)
    d = make_drone(hardness=40.0, state="engaged", energy_absorbed=30.0)
    break_beam(d, params)
    assert d.energy_absorbed == 30.0
    assert d.state == "alive"


def test_beam_break_on_dead_is_noop():
    params = KillParams(partial_energy_retention=False)
    d = make_drone(state="dead", energy_absorbed=40.0)
    break_beam(d, params)
    assert d.state == "dead"
    assert d.energy_absorbed == 40.0


def test_retention_resume_completes_kill():
    params = KillParams(partial_energy_retention=True)
    d = make_drone(hardness=40.0)
    deposit_energy(d, 30.0)
    break_beam(d, params)  # progress retained
    assert d.energy_absorbed == 30.0
    killed = deposit_energy(d, 10.0)  # resumes from 30 -> 40
    assert killed is True
    assert d.state == "dead"


def test_no_retention_resume_must_restart():
    params = KillParams(partial_energy_retention=False)
    d = make_drone(hardness=40.0)
    deposit_energy(d, 30.0)
    break_beam(d, params)  # progress lost
    assert d.energy_absorbed == 0.0
    killed = deposit_energy(d, 10.0)  # only 10 now
    assert killed is False
    assert d.state == "engaged"
    assert d.energy_absorbed == 10.0


# --------------------------------------------------------------------------- #
# resolve_step: integrated per-step behavior                                   #
# --------------------------------------------------------------------------- #


def test_resolve_step_kills_and_leaks():
    params = KillParams(leak_radius=10.0)
    killed_drone = make_drone("k", pos=(1000.0, 0.0), hardness=40.0)
    leaker = make_drone("l", pos=(0.0, 0.0), hardness=40.0)
    safe = make_drone("s", pos=(2000.0, 0.0), hardness=40.0)
    drones = [killed_drone, leaker, safe]

    newly_dead, newly_leaked = resolve_step(
        drones, {"k": 40.0}, ASSET, params
    )

    assert newly_dead == [killed_drone]
    assert newly_leaked == [leaker]
    assert killed_drone.state == "dead"
    assert leaker.state == "leaked"
    assert safe.state == "alive"


def test_resolve_step_killed_drone_does_not_also_leak():
    # Drone is at the asset AND killed this step -> death wins, no leak.
    params = KillParams(leak_radius=100.0)
    d = make_drone("d", pos=(0.0, 0.0), hardness=40.0)
    newly_dead, newly_leaked = resolve_step(d_list := [d], {"d": 40.0}, ASSET, params)
    assert newly_dead == [d]
    assert newly_leaked == []
    assert d.state == "dead"
    assert d_list[0].state == "dead"


def test_resolve_step_beam_break_default_resets_progress():
    params = KillParams(partial_energy_retention=False, leak_radius=1.0)
    d = make_drone("d", pos=(1000.0, 0.0), hardness=40.0, state="engaged",
                   energy_absorbed=30.0)
    # No delivery for "d" this step -> beam considered broken.
    newly_dead, newly_leaked = resolve_step([d], {}, ASSET, params)
    assert newly_dead == []
    assert newly_leaked == []
    assert d.state == "alive"
    assert d.energy_absorbed == 0.0


def test_resolve_step_beam_break_retention_keeps_progress():
    params = KillParams(partial_energy_retention=True, leak_radius=1.0)
    d = make_drone("d", pos=(1000.0, 0.0), hardness=40.0, state="engaged",
                   energy_absorbed=30.0)
    resolve_step([d], {}, ASSET, params)
    assert d.state == "alive"
    assert d.energy_absorbed == 30.0


def test_resolve_step_zero_delivery_breaks_beam():
    params = KillParams(partial_energy_retention=False, leak_radius=1.0)
    d = make_drone("d", pos=(1000.0, 0.0), hardness=40.0, state="engaged",
                   energy_absorbed=20.0)
    # Explicit zero delivery is "not engaged".
    resolve_step([d], {"d": 0.0}, ASSET, params)
    assert d.state == "alive"
    assert d.energy_absorbed == 0.0


def test_resolve_step_fixed_iteration_order_is_deterministic():
    params = KillParams(leak_radius=10.0)
    a = make_drone("a", pos=(0.0, 0.0))
    b = make_drone("b", pos=(0.0, 0.0))
    c = make_drone("c", pos=(0.0, 0.0))
    drones = [a, b, c]
    _, newly_leaked = resolve_step(drones, {}, ASSET, params)
    assert [d.id for d in newly_leaked] == ["a", "b", "c"]


def test_resolve_step_skips_already_terminal_drones():
    params = KillParams(leak_radius=1.0)
    dead = make_drone("dead", state="dead", energy_absorbed=40.0)
    leaked = make_drone("leaked", state="leaked")
    drones = [dead, leaked]
    newly_dead, newly_leaked = resolve_step(drones, {"dead": 10.0}, ASSET, params)
    assert newly_dead == []
    assert newly_leaked == []
    assert dead.state == "dead"
    assert leaked.state == "leaked"


def test_resolve_step_determinism_repeated_runs_match():
    params = KillParams(leak_radius=10.0)

    def run() -> list[tuple[str, str]]:
        drones = [
            make_drone("k", pos=(1000.0, 0.0), hardness=40.0),
            make_drone("l", pos=(0.0, 0.0), hardness=40.0),
            make_drone("p", pos=(500.0, 0.0), hardness=40.0),
        ]
        resolve_step(drones, {"k": 40.0, "p": 20.0}, ASSET, params)
        return [(d.id, d.state) for d in drones]

    assert run() == run()
