"""Cost-basis lot ledger with HIFO sell selection.

Why a local ledger at all? Because the brokerage real-time APIs we target
(IBKR in particular) report only an *average* cost per symbol -- they do not
hand back individual tax lots over the wire. To choose *which* lot to sell we
must track lots ourselves: every buy fill opens a lot, every sell fill consumes
from existing lots.

Selection policy: **HIFO** (Highest In, First Out). To minimise the realised
capital gain on a sale we dispose of the shares with the *highest* cost basis
first, which yields the smallest gain (or the largest loss). Ties on cost are
broken by selling the oldest lot first, purely for determinism.

The selection is factored into :func:`select_hifo` so an alternative policy
(e.g. a holding-period-aware or loss-harvesting strategy) can be dropped in
later without touching the ledger.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..models import Fill, Lot, LotSale, dec


def select_hifo(lots: list[Lot], quantity: Decimal) -> list[LotSale]:
    """Choose lots to satisfy a sale of ``quantity`` shares, highest cost first.

    Returns per-lot sale instructions (a lot may be split). Raises ``ValueError``
    if the lots do not hold enough shares -- the caller must never ask to sell
    more than it holds.
    """
    if quantity <= 0:
        return []
    available = sum((lot.quantity for lot in lots), Decimal(0))
    if quantity > available:
        raise ValueError(
            f"cannot sell {quantity} shares; only {available} held across lots"
        )

    # Highest cost per share first; oldest first to break ties deterministically.
    ordered = sorted(lots, key=lambda l: (-l.cost_per_share, l.acquired, l.lot_id))
    remaining = quantity
    sales: list[LotSale] = []
    for lot in ordered:
        if remaining <= 0:
            break
        take = min(lot.quantity, remaining)
        sales.append(
            LotSale(lot_id=lot.lot_id, quantity=take, cost_per_share=lot.cost_per_share)
        )
        remaining -= take
    return sales


def estimate_realized_gain(sales: list[LotSale], sale_price: Decimal) -> Decimal:
    """Realised gain/loss if the selected lots are sold at ``sale_price``."""
    return sum(
        ((sale_price - s.cost_per_share) * s.quantity for s in sales),
        Decimal(0),
    )


class LotLedger:
    """A persistent collection of open tax lots, grouped by symbol."""

    def __init__(self) -> None:
        self._lots: dict[str, list[Lot]] = {}
        self._seq: int = 0  # monotonic id source; deterministic, test-friendly

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "LotLedger":
        ledger = cls()
        path = Path(path)
        if not path.exists():
            return ledger
        data = json.loads(path.read_text(encoding="utf-8"))
        ledger._seq = int(data.get("seq", 0))
        for raw in data.get("lots", []):
            lot = Lot(
                lot_id=raw["lot_id"],
                symbol=raw["symbol"].upper(),
                quantity=dec(raw["quantity"]),
                cost_per_share=dec(raw["cost_per_share"]),
                acquired=date.fromisoformat(raw["acquired"]),
            )
            ledger._lots.setdefault(lot.symbol, []).append(lot)
        return ledger

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "seq": self._seq,
            "lots": [
                {
                    "lot_id": lot.lot_id,
                    "symbol": lot.symbol,
                    "quantity": str(lot.quantity),
                    "cost_per_share": str(lot.cost_per_share),
                    "acquired": lot.acquired.isoformat(),
                }
                for lots in self._lots.values()
                for lot in lots
            ],
        }
        path.write_text(json.dumps(data, indent=2))

    # -- queries -----------------------------------------------------------
    def lots_for(self, symbol: str) -> list[Lot]:
        return list(self._lots.get(symbol.upper(), []))

    def quantity(self, symbol: str) -> Decimal:
        return sum((lot.quantity for lot in self.lots_for(symbol)), Decimal(0))

    def symbols(self) -> list[str]:
        return sorted(s for s, lots in self._lots.items() if lots)

    def select_for_sale(self, symbol: str, quantity: Decimal) -> list[LotSale]:
        """Preview which lots a sale would consume (HIFO), without mutating."""
        return select_hifo(self.lots_for(symbol), dec(quantity))

    # -- mutations ---------------------------------------------------------
    def record_buy(
        self, symbol: str, quantity: Decimal, price: Decimal, when: date
    ) -> Lot:
        symbol = symbol.upper()
        self._seq += 1
        lot = Lot(
            lot_id=f"{symbol}-{self._seq}",
            symbol=symbol,
            quantity=dec(quantity),
            cost_per_share=dec(price),
            acquired=when,
        )
        self._lots.setdefault(symbol, []).append(lot)
        return lot

    def record_sell(self, symbol: str, quantity: Decimal) -> list[LotSale]:
        """Consume lots for a sale using HIFO, mutating the ledger.

        Returns the lots actually sold (for realised-gain reporting).
        """
        symbol = symbol.upper()
        sales = select_hifo(self.lots_for(symbol), dec(quantity))
        self._consume(symbol, sales)
        return sales

    def apply_fill(self, fill: Fill) -> list[LotSale]:
        """Update the ledger from an executed fill.

        For sells we re-select lots against the *actual* filled quantity rather
        than trusting the quantity chosen at planning time, so partial fills
        never desynchronise the ledger from real share counts. Returns the lots
        consumed (empty for buys).
        """
        if fill.side == "BUY":
            self.record_buy(fill.symbol, fill.quantity, fill.price, fill.when)
            return []
        return self.record_sell(fill.symbol, fill.quantity)

    def _consume(self, symbol: str, sales: list[LotSale]) -> None:
        by_id = {s.lot_id: s for s in sales}
        remaining: list[Lot] = []
        for lot in self._lots.get(symbol, []):
            sale = by_id.get(lot.lot_id)
            if sale is None:
                remaining.append(lot)
                continue
            leftover = lot.quantity - sale.quantity
            if leftover > 0:
                remaining.append(
                    Lot(
                        lot_id=lot.lot_id,
                        symbol=lot.symbol,
                        quantity=leftover,
                        cost_per_share=lot.cost_per_share,
                        acquired=lot.acquired,
                    )
                )
            # leftover == 0 -> lot fully sold, dropped
        self._lots[symbol] = remaining
