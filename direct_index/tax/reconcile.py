"""Reconcile the local tax-lot ledger against broker-reported share counts.

The ledger (:mod:`direct_index.tax.lots`) is our authoritative cost-basis
record, but it only stays correct if every share change flows through it. Real
accounts drift for reasons the ledger never sees:

* dividend reinvestment (DRIP) adds shares,
* splits / mergers / spinoffs change share counts,
* trades placed outside this tool,
* transfers in (ACATS) arrive with no lots,
* a fill we failed to record.

When the ledger and broker disagree, HIFO selection and realised-gain math are
wrong, and a rebalance can emit sells with no lot attribution. Reconciliation
detects and (on request) corrects this.

Two phases, deliberately separated:

``diff_positions`` is **read-only** -- it classifies every symbol as matched, a
*surplus* (broker holds more than the ledger knows about), or a *shortfall*
(the ledger holds phantom shares). ``apply_reconciliation`` mutates the ledger
to restore the invariant ``ledger.quantity(sym) == broker_qty(sym)``:

* surplus -> open a new lot for the extra shares, using the broker's average
  cost if available else the current price, dated today (an estimate, flagged);
* shortfall -> retire shares under the configured policy (FIFO by default),
  which realises nothing because this is a correction, not a sale.

Mutating a tax record is consequential, so the CLI keeps ``diff`` the default
and gates ``apply`` behind an explicit flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..models import dec
from .lots import LotLedger

MATCH = "match"
SURPLUS = "surplus"  # broker has more than the ledger
SHORTFALL = "shortfall"  # ledger has more than the broker


@dataclass(frozen=True)
class Discrepancy:
    symbol: str
    ledger_qty: Decimal
    broker_qty: Decimal
    kind: str  # MATCH | SURPLUS | SHORTFALL

    @property
    def delta(self) -> Decimal:
        """Broker minus ledger: positive => surplus, negative => shortfall."""
        return self.broker_qty - self.ledger_qty


@dataclass(frozen=True)
class ReconcileReport:
    discrepancies: tuple[Discrepancy, ...]

    @property
    def mismatches(self) -> tuple[Discrepancy, ...]:
        return tuple(d for d in self.discrepancies if d.kind != MATCH)

    @property
    def in_sync(self) -> bool:
        return not self.mismatches


@dataclass(frozen=True)
class Adjustment:
    """A single change reconciliation made (or would make) to the ledger."""

    symbol: str
    action: str  # "added_lot" | "removed_shares" | "skipped"
    quantity: Decimal
    detail: str


def diff_positions(
    ledger: LotLedger,
    broker_quantities: dict[str, Decimal],
    tolerance: Decimal = Decimal("0.000001"),
) -> ReconcileReport:
    """Compare ledger share counts to broker share counts (read-only)."""
    tolerance = dec(tolerance)
    broker = {s.upper(): dec(q) for s, q in broker_quantities.items()}
    universe = sorted(set(ledger.symbols()) | {s for s, q in broker.items() if q != 0})

    out: list[Discrepancy] = []
    for symbol in universe:
        lq = ledger.quantity(symbol)
        bq = broker.get(symbol, Decimal(0))
        delta = bq - lq
        if abs(delta) <= tolerance:
            kind = MATCH
        elif delta > 0:
            kind = SURPLUS
        else:
            kind = SHORTFALL
        out.append(Discrepancy(symbol, lq, bq, kind))
    return ReconcileReport(tuple(out))


def apply_reconciliation(
    ledger: LotLedger,
    report: ReconcileReport,
    *,
    prices: dict[str, Decimal],
    avg_costs: dict[str, Decimal],
    when: date,
    shortfall_policy: str = "fifo",
) -> list[Adjustment]:
    """Mutate ``ledger`` so its share counts match the broker's.

    After this returns (for every non-skipped symbol), the ledger holds exactly
    the broker-reported quantity. Surplus symbols with no known cost basis *and*
    no price are skipped rather than fabricated -- we will not invent a basis.
    """
    prices = {s.upper(): dec(p) for s, p in prices.items()}
    avg_costs = {s.upper(): dec(c) for s, c in avg_costs.items()}
    adjustments: list[Adjustment] = []

    for d in report.mismatches:
        if d.kind == SURPLUS:
            adjustments.append(
                _apply_surplus(ledger, d, prices, avg_costs, when)
            )
        else:  # SHORTFALL
            removed = ledger.remove_shares(d.symbol, -d.delta, shortfall_policy)
            retired = sum((s.quantity for s in removed), Decimal(0))
            adjustments.append(
                Adjustment(
                    symbol=d.symbol,
                    action="removed_shares",
                    quantity=retired,
                    detail=f"retired {retired} phantom shares via {shortfall_policy}",
                )
            )
    return adjustments


def _apply_surplus(
    ledger: LotLedger,
    d: Discrepancy,
    prices: dict[str, Decimal],
    avg_costs: dict[str, Decimal],
    when: date,
) -> Adjustment:
    qty = d.delta  # positive
    cost = avg_costs.get(d.symbol)
    source = "broker avg cost"
    if cost is None:
        cost = prices.get(d.symbol)
        source = "current price"
    if cost is None:
        return Adjustment(
            symbol=d.symbol,
            action="skipped",
            quantity=qty,
            detail="no avg cost or price available; cannot assign a basis",
        )
    ledger.record_buy(d.symbol, qty, cost, when)
    return Adjustment(
        symbol=d.symbol,
        action="added_lot",
        quantity=qty,
        detail=f"opened lot @ {cost} ({source}, acquired {when.isoformat()} est.)",
    )
