"""Kill resolution and leak detection (pdd.md sections 8.6, 7.2).

This module owns the rules that turn *delivered energy* into *dead drones* and that
turn *drones reaching the asset* into *leaks*. It is deliberately small, pure, and
deterministic so the decision loop can call it once per integration sub-step.

Model (pdd.md section 8.6):

- A target accumulates delivered energy on its ``energy_absorbed`` field while it is
  continuously engaged by a beam.
- It dies the instant cumulative delivered energy reaches ``E_kill = hardness``
  (``alive``/``engaged`` -> ``dead``).
- Breaking the beam (the turret re-slews away / stops firing this drone) loses all
  kill progress, UNLESS ``partial_energy_retention`` is enabled — a deliberate
  "cooling/repair" model that keeps the scheduling problem honest by default
  (pdd.md section 8.6).
- A drone that reaches the asset leaks (``alive``/``engaged`` -> ``leaked``); the
  objective penalizes it by its value (pdd.md sections 7.2, 7.3).

NO physics or policy constants are hard-coded here. Everything tunable — whether
partial energy is retained, and how close to the asset counts as "reached" — flows in
from config via :class:`KillParams`, supplied by the engine/decision-loop caller.
The energy *deposition rate* itself is computed by the physics module; this module
only integrates the already-delivered energy and applies the kill/leak/break rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from beam.schemas import Drone, Vec2
from beam.util import vdist

__all__ = [
    "KillParams",
    "deposit_energy",
    "break_beam",
    "resolve_leaks",
    "resolve_step",
]


# --------------------------------------------------------------------------- #
# Tunables (sourced from config by the caller — never hard-coded here)         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class KillParams:
    """Kill-resolution tunables, resolved from config by the engine caller.

    Attributes:
        partial_energy_retention: if ``True``, a drone keeps its accumulated
            ``energy_absorbed`` when the beam breaks; if ``False`` (the pdd.md 8.6
            default), breaking the beam resets progress to zero.
        leak_radius: distance from the asset at or below which a drone is considered
            to have *reached* the asset and leaks. Defaults to ``0.0`` (exact reach).
    """

    partial_energy_retention: bool = False
    leak_radius: float = 0.0


# --------------------------------------------------------------------------- #
# Energy accumulation + kill detection                                         #
# --------------------------------------------------------------------------- #


def deposit_energy(drone: Drone, energy: float) -> bool:
    """Add ``energy`` delivered this sub-step to a continuously-engaged ``drone``.

    Mutates ``drone`` in place. The drone transitions ``alive``/``engaged`` ->
    ``engaged`` while progress accrues, and ``-> dead`` the instant cumulative
    energy reaches its ``hardness`` (``E_kill``). Once dead (or already leaked) the
    call is a no-op.

    Args:
        drone: the engine-owned target being fired on (mutated in place).
        energy: delivered energy this sub-step (>= 0). The physics module computes
            this from delivered power x dt; this function does not model power.

    Returns:
        ``True`` iff this call killed the drone (the alive/engaged -> dead edge).
    """
    if drone.state in ("dead", "leaked"):
        return False
    if energy < 0.0:
        raise ValueError(f"delivered energy must be non-negative, got {energy!r}")

    # Engaging this drone marks it engaged even before the kill lands.
    drone.state = "engaged"
    drone.energy_absorbed += energy

    if drone.energy_absorbed >= drone.hardness:
        # Killed at the instant cumulative energy reaches E_kill (pdd.md 8.6).
        drone.energy_absorbed = drone.hardness  # clamp; no over-deposit accounting
        drone.state = "dead"
        return True
    return False


def break_beam(drone: Drone, params: KillParams) -> None:
    """Handle a beam break — the turret stopped firing this drone before kill.

    With ``partial_energy_retention`` disabled (the default), all kill progress is
    lost and the drone reverts to ``alive`` (pdd.md section 8.6: the cooling/repair
    model). With retention enabled, ``energy_absorbed`` is preserved; the drone still
    reverts to ``alive`` (it is no longer being actively engaged) so a later beam can
    resume from where it left off. Dead/leaked drones are untouched.

    Mutates ``drone`` in place.
    """
    if drone.state in ("dead", "leaked"):
        return
    if not params.partial_energy_retention:
        drone.energy_absorbed = 0.0
    drone.state = "alive"


# --------------------------------------------------------------------------- #
# Leak detection                                                               #
# --------------------------------------------------------------------------- #


def _has_reached_asset(drone: Drone, asset_pos: Vec2, leak_radius: float) -> bool:
    return vdist(drone.pos, asset_pos) <= leak_radius


def resolve_leaks(
    drones: list[Drone],
    asset_pos: Vec2,
    params: KillParams,
) -> list[Drone]:
    """Mark every live drone that has reached the asset as ``leaked``.

    Iterates ``drones`` in the given (fixed) order for determinism. A drone leaks
    when its distance to ``asset_pos`` is at or below ``params.leak_radius``. Dead and
    already-leaked drones are skipped. Mutates the matched drones in place.

    Returns:
        The list of drones that newly leaked on this call, in iteration order.
    """
    newly_leaked: list[Drone] = []
    for drone in drones:
        if drone.state in ("dead", "leaked"):
            continue
        if _has_reached_asset(drone, asset_pos, params.leak_radius):
            drone.state = "leaked"
            newly_leaked.append(drone)
    return newly_leaked


# --------------------------------------------------------------------------- #
# Combined per-step resolution                                                 #
# --------------------------------------------------------------------------- #


def resolve_step(
    drones: list[Drone],
    deliveries: dict[str, float],
    asset_pos: Vec2,
    params: KillParams,
) -> tuple[list[Drone], list[Drone]]:
    """Resolve one integration sub-step: deposit energy, break beams, detect leaks.

    For each live drone, in the given (fixed) iteration order:

    1. If it appears in ``deliveries`` with positive energy, that energy is deposited
       (it is being continuously engaged this sub-step) — it may die.
    2. If it does NOT appear in ``deliveries`` (or has zero delivery) and it currently
       carries kill progress, the beam is considered broken for it
       (:func:`break_beam`).
    3. Surviving drones that have reached the asset leak (:func:`resolve_leaks`).

    A killed drone never leaks in the same step (kills are resolved first). All inputs
    are mutated in place; iteration order is fixed for reproducibility.

    Args:
        drones: engine-owned live target list (mutated in place).
        deliveries: target_id -> delivered energy this sub-step (computed upstream by
            the physics module). Absent / non-positive entries mean "not engaged".
        asset_pos: the defended point.
        params: config-sourced kill tunables.

    Returns:
        ``(newly_dead, newly_leaked)`` — lists of drones whose state flipped this step,
        each in iteration order.
    """
    newly_dead: list[Drone] = []
    for drone in drones:
        if drone.state in ("dead", "leaked"):
            continue
        energy = deliveries.get(drone.id, 0.0)
        if energy > 0.0:
            if deposit_energy(drone, energy):
                newly_dead.append(drone)
        elif drone.energy_absorbed > 0.0 or drone.state == "engaged":
            # No delivery this step but the drone was mid-kill -> beam broke.
            break_beam(drone, params)

    newly_leaked = resolve_leaks(drones, asset_pos, params)
    return newly_dead, newly_leaked
