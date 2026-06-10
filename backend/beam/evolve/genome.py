"""Evolutionary genome: swarm approach parameters varied by the GA."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = ["Genome", "BOUNDS", "GENE_KEYS", "genome_to_overlay", "random_genome", "crossover", "mutate"]

# Search space: (lo, hi) per gene.
BOUNDS: dict[str, tuple[float, float]] = {
    "spawn_arc_center_deg": (0.0, 360.0),
    "spawn_arc_width_deg": (10.0, 180.0),
    "spawn_radius": (500.0, 8000.0),
    "speed": (5.0, 60.0),
    "separation_weight": (0.1, 5.0),
    "cohesion_weight": (0.01, 1.0),
    "goal_weight": (0.5, 8.0),
}

GENE_KEYS = list(BOUNDS.keys())


@dataclass
class Genome:
    spawn_arc_center_deg: float
    spawn_arc_width_deg: float
    spawn_radius: float
    speed: float
    separation_weight: float
    cohesion_weight: float
    goal_weight: float

    def to_vector(self) -> list[float]:
        return [getattr(self, k) for k in GENE_KEYS]

    @staticmethod
    def from_vector(v: list[float]) -> "Genome":
        return Genome(**dict(zip(GENE_KEYS, v)))


def random_genome(rng: np.random.Generator) -> Genome:
    vec = [float(rng.uniform(lo, hi)) for lo, hi in BOUNDS.values()]
    return Genome.from_vector(vec)


def crossover(a: Genome, b: Genome, rng: np.random.Generator) -> tuple[Genome, Genome]:
    va, vb = a.to_vector(), b.to_vector()
    mask = rng.integers(0, 2, size=len(va)).astype(bool)
    c1 = [va[i] if mask[i] else vb[i] for i in range(len(va))]
    c2 = [vb[i] if mask[i] else va[i] for i in range(len(va))]
    return Genome.from_vector(c1), Genome.from_vector(c2)


def mutate(g: Genome, rng: np.random.Generator, *, rate: float = 0.15) -> Genome:
    vec = g.to_vector()
    for i, (lo, hi) in enumerate(BOUNDS.values()):
        if rng.random() < rate:
            sigma = (hi - lo) * 0.1
            vec[i] = float(np.clip(vec[i] + rng.normal(0, sigma), lo, hi))
    return Genome.from_vector(vec)


def genome_to_overlay(
    g: Genome,
    *,
    base_swarm_count: int,
    base_behavior: str,
) -> dict[str, Any]:
    arc_half = g.spawn_arc_width_deg / 2.0
    arc_start = g.spawn_arc_center_deg - arc_half
    arc_end = g.spawn_arc_center_deg + arc_half
    overlay: dict[str, Any] = {
        "swarm_spec": {
            "count": base_swarm_count,
            "behavior": base_behavior,
            "spawn_radius": g.spawn_radius,
            "spawn_arc_deg": [arc_start, arc_end],
            "speed": g.speed,
        }
    }
    if base_behavior == "flocking":
        overlay["kinematics"] = {
            "flocking": {
                "separation_weight": g.separation_weight,
                "cohesion_weight": g.cohesion_weight,
                "goal_weight": g.goal_weight,
            }
        }
    return overlay
