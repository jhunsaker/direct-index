"""In-memory paper broker with JSON persistence.

This is a real, fully-working broker implementation -- not a stub. It lets the
entire pipeline (fetch constituents -> blend -> diff -> execute -> update the
tax ledger) run and be tested end-to-end with no external services. It is also
the test double used by the suite.

State is a small JSON file::

    {
      "cash": "100000.00",
      "positions": {"AAPL": "12", "MSFT": "5"},
      "prices":    {"AAPL": "190.50", "MSFT": "410.20"}
    }

Prices live in the state file because a simulated market has to get its prices
from somewhere; ``set_prices`` (and the ``set-prices`` CLI command) update them,
e.g. from a downloaded holdings file or a hand-maintained quotes CSV.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from ..models import Account, Fill, Position, Trade, dec
from .base import BrokerBase


class PaperBroker(BrokerBase):
    def __init__(self, *, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self._cash: Decimal = Decimal(0)
        self._positions: dict[str, Decimal] = {}
        self._prices: dict[str, Decimal] = {}
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._cash = dec(data.get("cash", "0"))
        self._positions = {
            s.upper(): dec(q) for s, q in data.get("positions", {}).items()
        }
        self._prices = {
            s.upper(): dec(p) for s, p in data.get("prices", {}).items()
        }

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": str(self._cash),
            "positions": {s: str(q) for s, q in self._positions.items() if q != 0},
            "prices": {s: str(p) for s, p in self._prices.items()},
        }
        self.state_path.write_text(json.dumps(data, indent=2, sort_keys=True))

    # -- BrokerClient ------------------------------------------------------
    def get_account(self) -> Account:
        positions = {}
        for symbol, qty in self._positions.items():
            if qty == 0:
                continue
            price = self._prices.get(symbol, Decimal(0))
            positions[symbol] = Position(symbol, qty, price)
        return Account(cash=self._cash, positions=positions)

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        return {
            s.upper(): self._prices[s.upper()]
            for s in symbols
            if s.upper() in self._prices
        }

    def execute(self, trade: Trade) -> Fill:
        symbol = trade.symbol.upper()
        # Fill at the current book price, falling back to the trade's estimate
        # if this symbol has no quote yet (e.g. an opening buy).
        price = self._prices.get(symbol, trade.est_price)
        signed = trade.quantity if trade.side == "BUY" else -trade.quantity

        new_qty = self._positions.get(symbol, Decimal(0)) + signed
        if new_qty < 0:
            raise ValueError(
                f"paper broker: cannot sell {trade.quantity} {symbol}; "
                f"only hold {self._positions.get(symbol, Decimal(0))}"
            )

        self._positions[symbol] = new_qty
        self._cash -= signed * price
        self._save()
        return Fill(
            symbol=symbol,
            side=trade.side,
            quantity=trade.quantity,
            price=price,
            when=_today(),
            lots=trade.lots,
        )

    # -- paper-only helpers ------------------------------------------------
    def set_prices(self, prices: dict[str, Decimal]) -> None:
        for symbol, price in prices.items():
            self._prices[symbol.upper()] = dec(price)
        self._save()

    def deposit(self, amount: Decimal) -> None:
        self._cash += dec(amount)
        self._save()


def _today():
    # Imported here so the module has no import-time clock dependency, which
    # keeps it friendly to deterministic tests that monkeypatch the date.
    from datetime import date

    return date.today()
