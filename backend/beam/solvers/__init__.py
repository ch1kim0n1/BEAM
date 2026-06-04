"""Solver suite package.

Exposes the ``Solver`` Protocol and the registry. Concrete solver modules
(greedy, auction, metaheuristic, cp_sat) are added by downstream agents; importing
them registers their classes via the ``@register`` decorator in ``base``.
"""

from __future__ import annotations

from beam.solvers.base import (
    REGISTRY,
    Solver,
    available_solvers,
    get_solver_class,
    register,
)

# Importing concrete solver modules registers their classes in REGISTRY via the
# @register decorator. Keep imports in deterministic build order (pdd.md section 9.2).
from beam.solvers import greedy_nearest  # noqa: E402,F401
from beam.solvers import greedy_threat  # noqa: E402,F401
from beam.solvers import greedy_urgent  # noqa: E402,F401
from beam.solvers import auction  # noqa: E402,F401
from beam.solvers import metaheuristic  # noqa: E402,F401
from beam.solvers import cp_sat  # noqa: E402,F401  (exact reference; registers "cp_sat")

__all__ = [
    "Solver",
    "REGISTRY",
    "register",
    "get_solver_class",
    "available_solvers",
]
