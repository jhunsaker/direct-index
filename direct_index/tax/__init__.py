"""Module #5: tax accounting.

A local, authoritative cost-basis ledger of tax lots, with sell-lot selection
that minimises realised gains by always disposing of the **highest-cost** lot
first (HIFO). See :mod:`direct_index.tax.lots`.
"""

from __future__ import annotations

from .lots import LotLedger, estimate_realized_gain, select_hifo, select_lots
from .reconcile import (
    Adjustment,
    Discrepancy,
    ReconcileReport,
    apply_reconciliation,
    diff_positions,
)

__all__ = [
    "Adjustment",
    "Discrepancy",
    "LotLedger",
    "ReconcileReport",
    "apply_reconciliation",
    "diff_positions",
    "estimate_realized_gain",
    "select_hifo",
    "select_lots",
]
