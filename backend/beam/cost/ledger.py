"""Running cost ledger (pdd.md section 10).

Implements the per-epoch cost/value bookkeeping that powers the breakeven
headline metric. The model (pdd.md 10) is::

    shot_energy_cost = energy_delivered_kWh * price_per_kWh
    maintenance_cost = maintenance_rate * operating_time
    capex_amortized  = system_capex / expected_lifetime_engagements  (per engagement)
    cumulative_cost  = Sum(shot_energy_cost + maintenance_cost) + capex_amortized * engagements
    value_destroyed  = Sum(v_j) over killed targets
    net_position     = value_destroyed - cumulative_cost

All cost constants come from :class:`beam.config.CostConfig`; nothing is
hard-coded here (the only literal is ``0.0`` initial state). The ledger is a
pure accumulator: identical inputs in identical order produce identical
output, satisfying the determinism / reproducibility requirement (no RNG is
needed or used).

Units convention (illustrative, internally consistent per pdd.md 8/18):
``operating_time`` is supplied in the same time unit that ``maintenance_rate``
is denominated in (defaults are "per operating-hour"); callers pass elapsed
operating hours for the epoch. ``energy_delivered_kWh`` is the laser energy
actually delivered to targets during the epoch, in kWh.
"""

from __future__ import annotations

from beam.config import CostConfig
from beam.schemas import LedgerSnapshot


class Ledger:
    """Stateful running ledger; one instance per run.

    Construct from a :class:`CostConfig` (config-sourced constants), then call
    :meth:`update` once per decision epoch with that epoch's deltas. The ledger
    accumulates the cumulative components internally and returns a fresh
    :class:`LedgerSnapshot` each epoch.
    """

    def __init__(self, cost: CostConfig) -> None:
        self._price_per_kwh = cost.price_per_kwh
        self._maintenance_rate = cost.maintenance_rate
        self._system_capex = cost.system_capex
        self._expected_lifetime_engagements = cost.expected_lifetime_engagements

        # Cumulative running components (all start at zero state).
        self._shot_energy_cost = 0.0
        self._maintenance_cost = 0.0
        self._value_destroyed = 0.0
        self._engagements = 0

    # ------------------------------------------------------------------ #
    # Derived per-engagement / per-unit constants (from config)          #
    # ------------------------------------------------------------------ #
    @property
    def capex_per_engagement(self) -> float:
        """Amortized capex charged per engagement (config-derived).

        ``system_capex / expected_lifetime_engagements``. Guards against a
        zero/negative lifetime in config by yielding 0.0 (no amortization)
        rather than dividing by zero.
        """
        denom = self._expected_lifetime_engagements
        if denom <= 0:
            return 0.0
        return self._system_capex / denom

    # ------------------------------------------------------------------ #
    # Per-shot / per-epoch cost helpers                                  #
    # ------------------------------------------------------------------ #
    def shot_cost(self, energy_delivered_kwh: float) -> float:
        """Energy cost for delivering ``energy_delivered_kwh`` kWh of beam.

        ``energy_delivered_kWh * price_per_kWh`` (pdd.md 10). Positive for any
        positive energy delivered.
        """
        return energy_delivered_kwh * self._price_per_kwh

    def maintenance_for(self, operating_time: float) -> float:
        """Maintenance cost accrued over ``operating_time`` (pdd.md 10).

        ``maintenance_rate * operating_time``.
        """
        return self._maintenance_rate * operating_time

    # ------------------------------------------------------------------ #
    # Epoch update                                                        #
    # ------------------------------------------------------------------ #
    def update(
        self,
        *,
        energy_delivered_kwh: float = 0.0,
        operating_time: float = 0.0,
        value_destroyed: float = 0.0,
        engagements: int = 0,
    ) -> LedgerSnapshot:
        """Fold one epoch's deltas into the running ledger and snapshot it.

        Parameters
        ----------
        energy_delivered_kwh:
            Beam energy delivered this epoch (kWh). Drives ``shot_energy_cost``.
        operating_time:
            Operating time elapsed this epoch, in the unit ``maintenance_rate``
            is denominated in. Drives ``maintenance_cost``.
        value_destroyed:
            Sum of target values (v_j) killed this epoch. Added to the running
            ``value_destroyed``.
        engagements:
            Number of new engagements opened this epoch. Each engagement is
            charged ``capex_per_engagement`` of amortized capex.

        Returns
        -------
        LedgerSnapshot
            The cumulative ledger state after applying this epoch's deltas.
        """
        self._shot_energy_cost += self.shot_cost(energy_delivered_kwh)
        self._maintenance_cost += self.maintenance_for(operating_time)
        self._value_destroyed += value_destroyed
        self._engagements += engagements
        return self.snapshot()

    # ------------------------------------------------------------------ #
    # Snapshot                                                            #
    # ------------------------------------------------------------------ #
    def snapshot(self) -> LedgerSnapshot:
        """Materialize the current cumulative state as a :class:`LedgerSnapshot`."""
        capex_amortized = self.capex_per_engagement * self._engagements
        cumulative_cost = (
            self._shot_energy_cost + self._maintenance_cost + capex_amortized
        )
        return LedgerSnapshot(
            cumulative_cost=cumulative_cost,
            value_destroyed=self._value_destroyed,
            net=self._value_destroyed - cumulative_cost,
            shot_energy_cost=self._shot_energy_cost,
            maintenance_cost=self._maintenance_cost,
            capex_amortized=capex_amortized,
            engagements=self._engagements,
        )
