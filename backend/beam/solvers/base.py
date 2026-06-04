"""Solver contract + registry (pdd.md section 9.1).

Every assignment policy implements the ``Solver`` Protocol so all policies are
interchangeable and comparable in the solver race. Solvers self-register by name via
the ``@register`` decorator; the engine and API discover them through ``REGISTRY``.

Contract::

    class Solver(Protocol):
        name: str
        def solve(self, state: WorldState, deadline_ms: int) -> Assignment: ...

``solve`` must respect the ``deadline_ms`` wall-clock budget and return its
best-so-far assignment if the deadline is hit. It must read only ``state`` and be
deterministic given identical ``state`` (no hidden global RNG; if a solver needs
randomness it derives it deterministically, e.g. from a seed it is constructed with).
"""

from __future__ import annotations

from typing import Callable, Dict, Protocol, Type, TypeVar, runtime_checkable

from beam.schemas import Assignment, WorldState


@runtime_checkable
class Solver(Protocol):
    """The single interface every assignment policy implements (pdd.md section 9.1)."""

    name: str

    def solve(self, state: WorldState, deadline_ms: int) -> Assignment:
        """Return an assignment (turret -> ordered targets) for this epoch.

        Must respect ``deadline_ms`` wall-clock budget; return best-so-far if hit.
        """
        ...


# Name -> solver class. Insertion order is preserved (dict), giving a deterministic
# iteration order for the solver race.
REGISTRY: Dict[str, Type[Solver]] = {}

_T = TypeVar("_T", bound=Type[Solver])


def register(name: str) -> Callable[[_T], _T]:
    """Class decorator that registers a solver under ``name`` in ``REGISTRY``.

    Usage::

        @register("greedy_nearest")
        class GreedyNearest:
            name = "greedy_nearest"
            def solve(self, state, deadline_ms): ...

    Raises ValueError on a duplicate name to catch accidental clobbering.
    """

    def _decorator(cls: _T) -> _T:
        if name in REGISTRY:
            raise ValueError(f"Solver name already registered: {name!r}")
        REGISTRY[name] = cls
        return cls

    return _decorator


def get_solver_class(name: str) -> Type[Solver]:
    """Look up a registered solver class by name (raises KeyError if unknown)."""
    return REGISTRY[name]


def available_solvers() -> list[str]:
    """Registered solver names in deterministic registration order."""
    return list(REGISTRY.keys())
