"""Shared utilities: seeded RNG, Vec2 math, angular helpers.

Determinism is a hard requirement (pdd.md sections 3.1, 4, 21). Every stochastic
draw in BEAM must come from a single seeded RNG created via ``make_rng``; never call
the module-level ``numpy.random`` or ``random`` functions directly.
"""

from __future__ import annotations

import math

import numpy as np

from beam.schemas import Vec2

TWO_PI = 2.0 * math.pi


# --------------------------------------------------------------------------- #
# Seeded RNG factory                                                          #
# --------------------------------------------------------------------------- #


def make_rng(seed: int) -> np.random.Generator:
    """Create the single seeded RNG for a run (numpy PCG64).

    Using ``numpy.random.Generator`` with an explicit ``PCG64(seed)`` gives a
    reproducible, platform-stable stream. Pass this object everywhere; do not reseed
    mid-run.
    """
    return np.random.Generator(np.random.PCG64(seed))


# --------------------------------------------------------------------------- #
# Vec2 math helpers                                                           #
# --------------------------------------------------------------------------- #


def vadd(a: Vec2, b: Vec2) -> Vec2:
    return Vec2(x=a.x + b.x, y=a.y + b.y)


def vsub(a: Vec2, b: Vec2) -> Vec2:
    return Vec2(x=a.x - b.x, y=a.y - b.y)


def vscale(a: Vec2, k: float) -> Vec2:
    return Vec2(x=a.x * k, y=a.y * k)


def vdot(a: Vec2, b: Vec2) -> float:
    return a.x * b.x + a.y * b.y


def vnorm(a: Vec2) -> float:
    """Euclidean magnitude of a Vec2."""
    return math.hypot(a.x, a.y)


def vdist(a: Vec2, b: Vec2) -> float:
    """Euclidean distance between two points."""
    return math.hypot(a.x - b.x, a.y - b.y)


def vunit(a: Vec2) -> Vec2:
    """Unit vector in the direction of ``a`` (returns zero vector if ``a`` is zero)."""
    n = vnorm(a)
    if n == 0.0:
        return Vec2(x=0.0, y=0.0)
    return Vec2(x=a.x / n, y=a.y / n)


def heading_to(src: Vec2, dst: Vec2) -> float:
    """Heading (radians) from ``src`` toward ``dst`` via atan2, in (-pi, pi]."""
    return math.atan2(dst.y - src.y, dst.x - src.x)


# --------------------------------------------------------------------------- #
# Angular helpers                                                             #
# --------------------------------------------------------------------------- #


def wrap_angle(theta: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    t = (theta + math.pi) % TWO_PI - math.pi
    # ((-pi) % 2pi) maps to -pi -> bring to +pi to keep the half-open convention.
    if t == -math.pi:
        return math.pi
    return t


def angular_distance(a: float, b: float) -> float:
    """Smallest absolute angular separation between two headings, in [0, pi].

    Used for slew-time setup cost (pdd.md section 8.4):
        s_i(a, b) = angular_distance(aim_a, aim_b) / slew_rate_i + settle_time_i
    """
    return abs(wrap_angle(a - b))
