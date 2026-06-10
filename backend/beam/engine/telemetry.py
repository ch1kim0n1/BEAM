"""Telemetry construction, persistence, and reproducibility hashing (pdd.md 12.2).

Two responsibilities:

1. **Build wire messages** from engine state - render-cadence
   :class:`~beam.schemas.FrameMessage` (one per simulation frame) and decision-cadence
   :class:`~beam.schemas.EpochMessage` (one per decision epoch) - exactly per the
   contract in pdd.md 12.2. Frame/epoch fractions (``hp_frac``, ``thermal_frac``,
   ``power_frac``) are derived and clamped here so every number on the wire is bounded.

2. **Persist + hash the stream.** :class:`TelemetryRecorder` collects the messages,
   writes ``telemetry.jsonl`` + ``summary.json`` under ``runs/<run_id>/`` (pdd.md 12.2),
   and produces a *stable* SHA-256 hash of the telemetry stream. The hash is the basis
   of the reproducibility gate (mvp.md §4-5): same seed + same active solver ->
   byte-identical telemetry -> identical hash.

Determinism rules honored here:

- JSON is emitted with ``sort_keys=False`` but from pydantic ``model_dump`` whose field
  order is the (fixed) declaration order, and floats are serialized via Python's
  ``repr``-stable ``json`` encoder. No wall-clock, no RNG, no set iteration leaks into
  the bytes - ``solve_ms`` (a timing measurement) is deliberately **excluded** from the
  hash so the repro hash is invariant to machine speed while the full JSONL on disk
  still carries it for analysis.
- ``BeamFrame`` is serialized ``by_alias=True`` so the wire key is ``"from"`` (pdd.md
  12.2 / schema contract).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from beam.schemas import (
    BeamFrame,
    Drone,
    DroneFrame,
    EpochLedger,
    EpochMessage,
    EpochRecord,
    EpochSolverEntry,
    FrameMessage,
    RunSummary,
    Turret,
    TurretFrame,
    Vec2,
)
from beam.engine.kinematics import time_to_impact

__all__ = [
    "build_drone_frame",
    "build_turret_frame",
    "build_frame_message",
    "build_epoch_message",
    "TelemetryRecorder",
]


# --------------------------------------------------------------------------- #
# Clamping helpers                                                            #
# --------------------------------------------------------------------------- #


def _clamp01(x: float) -> float:
    """Clamp to [0, 1] (wire fractions must be bounded; pdd.md 12.2)."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


# --------------------------------------------------------------------------- #
# Frame builders (render cadence, pdd.md 12.2)                                 #
# --------------------------------------------------------------------------- #


def build_drone_frame(drone: Drone, asset_pos: Vec2) -> DroneFrame:
    """Project a live :class:`Drone` into its wire :class:`DroneFrame` (pdd.md 12.2).

    ``hp_frac = 1 - energy_absorbed / hardness`` clamped to [0, 1]; ``tti`` is the
    time-to-impact computed by the kinematics module.
    """
    hardness = drone.hardness if drone.hardness > 0.0 else 1.0
    hp_frac = _clamp01(1.0 - drone.energy_absorbed / hardness)
    return DroneFrame(
        id=drone.id,
        x=drone.pos.x,
        y=drone.pos.y,
        v=(drone.vel.x, drone.vel.y),
        value=drone.value,
        hp_frac=hp_frac,
        state=drone.state,
        tti=time_to_impact(drone, asset_pos),
    )


def build_turret_frame(turret: Turret, *, forced_cooldown: bool = False) -> TurretFrame:
    """Project a :class:`Turret` into its wire :class:`TurretFrame` (pdd.md 12.2).

    ``thermal_frac = thermal / h_max`` clamped to [0, 1]. The turret's ``state`` is
    taken as-is from the engine (already reflects forced cooldown); ``forced_cooldown``
    is accepted for callers that want to override the displayed state but is not needed
    when the engine has already set ``state == "cooldown"``.
    """
    h_max = turret.thermal_cfg.h_max if turret.thermal_cfg.h_max > 0.0 else 1.0
    thermal_frac = _clamp01(turret.thermal / h_max)
    state = turret.state
    if forced_cooldown and state != "firing":
        state = "cooldown"
    return TurretFrame(
        id=turret.id,
        x=turret.pos.x,
        y=turret.pos.y,
        aim=turret.aim,
        target=turret.current_target,
        state=state,
        thermal_frac=thermal_frac,
    )


def build_frame_message(
    *,
    t: float,
    drones: list[Drone],
    turrets: list[Turret],
    asset_pos: Vec2,
    kills: int,
    leaks: int,
    forced_cooldown: Optional[dict[str, bool]] = None,
    power_frac: Optional[dict[str, float]] = None,
) -> FrameMessage:
    """Assemble one render-cadence :class:`FrameMessage` (pdd.md 12.2).

    Only live (alive/engaged) drones are emitted as movers; dead/leaked drones are
    dropped from the frame so the renderer naturally clears them. Beams are drawn for
    every turret currently ``firing`` at a live target, with ``power_frac`` = the
    delivered/emitted fraction (Beer-Lambert), giving the renderer intensity for free.

    Iteration is over the fixed entity list order for deterministic byte output.
    """
    fc = forced_cooldown or {}
    pf = power_frac or {}
    drone_frames: list[DroneFrame] = []
    live_by_id: dict[str, Drone] = {}
    for d in drones:
        if d.state in ("dead", "leaked"):
            continue
        drone_frames.append(build_drone_frame(d, asset_pos))
        live_by_id[d.id] = d

    turret_frames: list[TurretFrame] = []
    beams: list[BeamFrame] = []
    for turret in turrets:
        turret_frames.append(
            build_turret_frame(turret, forced_cooldown=fc.get(turret.id, False))
        )
        if turret.state == "firing" and turret.current_target in live_by_id:
            beams.append(
                BeamFrame(
                    from_=turret.id,
                    to=turret.current_target,  # type: ignore[arg-type]
                    power_frac=_clamp01(pf.get(turret.id, 1.0)),
                )
            )

    return FrameMessage(
        t=t,
        drones=drone_frames,
        turrets=turret_frames,
        beams=beams,
        leaks=leaks,
        kills=kills,
    )


# --------------------------------------------------------------------------- #
# Epoch builder (decision cadence, pdd.md 12.2)                                #
# --------------------------------------------------------------------------- #


def build_epoch_message(record: EpochRecord) -> EpochMessage:
    """Project an :class:`EpochRecord` into the wire :class:`EpochMessage` (pdd.md 12.2).

    The wire form drops the embedded :class:`~beam.schemas.Assignment` (clients never
    need it) and the cumulative ledger components, keeping only the compact
    :class:`EpochLedger` triple. Solver rows preserve the race order.
    """
    solvers = [
        EpochSolverEntry(
            name=r.name,
            objective=r.objective,
            solve_ms=r.solve_ms,
            gap=r.gap,
            is_optimal=r.is_optimal,
            bound=r.bound,
            gap_is_bound_based=r.gap_is_bound_based,
        )
        for r in record.solver_results
    ]
    return EpochMessage(
        t=record.t,
        epoch=record.epoch,
        solvers=solvers,
        active_solver=record.active_solver,
        ledger=EpochLedger(
            cumulative_cost=record.ledger.cumulative_cost,
            value_destroyed=record.ledger.value_destroyed,
            net=record.ledger.net,
        ),
    )


# --------------------------------------------------------------------------- #
# Recorder: persistence + reproducibility hashing                             #
# --------------------------------------------------------------------------- #

# Fields excluded from the reproducibility hash because they measure wall-clock time
# (machine-dependent) rather than simulation state. They remain in the on-disk JSONL.
_HASH_EXCLUDED_FIELDS = ("solve_ms",)


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON string for hashing: stable separators, fixed field order."""
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


@dataclass
class TelemetryRecorder:
    """Collects telemetry messages, writes them to disk, and hashes the stream.

    The recorder is the single sink the decision loop streams into. It keeps the wire
    messages (frames + epochs) and the final :class:`RunSummary`, then:

    - :meth:`write` persists ``telemetry.jsonl`` (one JSON object per line: frames and
      epochs interleaved in emission order) and ``summary.json`` under
      ``<base>/runs/<run_id>/`` (pdd.md 12.2);
    - :meth:`telemetry_hash` returns a stable SHA-256 over the simulation-state portion
      of the stream (excluding timing) for the reproducibility gate (mvp.md §4-5).
    """

    run_id: str
    frame_messages: list[FrameMessage] = field(default_factory=list)
    epoch_messages: list[EpochMessage] = field(default_factory=list)
    # Emission order of (kind, index) so the JSONL preserves interleaving.
    _order: list[tuple[str, int]] = field(default_factory=list)
    summary: Optional[RunSummary] = None
    epoch_records: list[EpochRecord] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Ingest                                                              #
    # ------------------------------------------------------------------ #

    def add_frame(self, frame: FrameMessage) -> None:
        self._order.append(("frame", len(self.frame_messages)))
        self.frame_messages.append(frame)

    def add_epoch(self, epoch: EpochMessage, record: Optional[EpochRecord] = None) -> None:
        self._order.append(("epoch", len(self.epoch_messages)))
        self.epoch_messages.append(epoch)
        if record is not None:
            self.epoch_records.append(record)

    def set_summary(self, summary: RunSummary) -> None:
        self.summary = summary

    # ------------------------------------------------------------------ #
    # Serialization                                                       #
    # ------------------------------------------------------------------ #

    def _dump_message(self, kind: str, index: int) -> dict[str, Any]:
        """One wire message as a plain dict (BeamFrame aliased to ``from``)."""
        if kind == "frame":
            return self.frame_messages[index].model_dump(by_alias=True)
        return self.epoch_messages[index].model_dump(by_alias=True)

    def iter_messages(self) -> list[dict[str, Any]]:
        """All messages as plain dicts, in emission (interleaved) order."""
        return [self._dump_message(kind, idx) for kind, idx in self._order]

    def jsonl(self) -> str:
        """The full telemetry stream as newline-delimited JSON (the on-disk form)."""
        return "\n".join(_canonical_json(m) for m in self.iter_messages())

    # ------------------------------------------------------------------ #
    # Reproducibility hash                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_timing(message: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of an epoch message with timing fields removed (recursively).

        Only epoch messages carry ``solve_ms`` (per-solver). Frames carry no timing.
        """
        if message.get("type") != "epoch":
            return message
        clean = dict(message)
        solvers = clean.get("solvers")
        if isinstance(solvers, list):
            clean["solvers"] = [
                {k: v for k, v in s.items() if k not in _HASH_EXCLUDED_FIELDS}
                for s in solvers
            ]
        return clean

    def telemetry_hash(self) -> str:
        """Stable SHA-256 of the simulation-state telemetry stream (mvp.md §4-5).

        Hashes every message in emission order, with machine-dependent timing fields
        stripped, so the hash depends only on the deterministic simulation - same seed
        + same active solver yields the identical hash on any machine.
        """
        h = hashlib.sha256()
        for kind, idx in self._order:
            msg = self._strip_timing(self._dump_message(kind, idx))
            h.update(_canonical_json(msg).encode("ascii"))
            h.update(b"\n")
        return h.hexdigest()

    # ------------------------------------------------------------------ #
    # Persistence                                                         #
    # ------------------------------------------------------------------ #

    def write(self, base_dir: str | Path) -> str:
        """Write ``telemetry.jsonl`` + ``summary.json`` under ``<base>/runs/<run_id>/``.

        Returns the absolute run directory path. Creates parents as needed. The summary
        also carries the telemetry hash so a persisted run is self-describing for repro.
        """
        run_dir = Path(base_dir) / "runs" / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        (run_dir / "telemetry.jsonl").write_text(self.jsonl() + "\n", encoding="ascii")

        summary_obj: dict[str, Any] = {
            "run_id": self.run_id,
            "telemetry_hash": self.telemetry_hash(),
        }
        if self.summary is not None:
            summary_obj["summary"] = self.summary.model_dump()
        (run_dir / "summary.json").write_text(
            json.dumps(summary_obj, indent=2, sort_keys=True) + "\n", encoding="ascii"
        )
        return str(run_dir.resolve())
