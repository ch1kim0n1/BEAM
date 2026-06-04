"""Cost ledger package (pdd.md section 10).

Stub created by the scaffold. The cost agent implements the per-epoch ledger here,
producing ``LedgerSnapshot`` records. All cost constants come from
``beam.config.CostConfig``; none are hard-coded.
"""

from beam.cost.ledger import Ledger

__all__ = ["Ledger"]
