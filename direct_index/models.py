"""Core value types shared across every module.

These are deliberately plain, mostly-frozen dataclasses with no I/O and no
dependencies so they can flow between the index providers, the broker adapters,
the rebalancer and the tax ledger without coupling any of them together.

Monetary and share quantities are represented with :class:`decimal.Decimal`.
Floating-point dollars accumulate rounding error that is unacceptable for money
and for reconciling share counts against a brokerage, so every quantity that
participates in arithmetic here is a ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

Side = str  # "BUY" | "SELL" -- kept as a str alias for simple serialisation.
BUY: Side = "BUY"
SELL: Side = "SELL"


def dec(value: object) -> Decimal:
    """Coerce ints/floats/strings to ``Decimal`` without float artefacts.

    ``Decimal(0.1)`` yields ``0.1000000000000000055...``; going through ``str``
    first (``Decimal("0.1")``) gives the value the user actually wrote. All
    external inputs (config, CSVs, broker responses) come through here.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class Constituent:
    """A single member of one index, with its weight *within that index*.

    ``weight`` is a fraction in ``[0, 1]``; the weights of all constituents in
    an index are expected to sum to ~1 (providers normalise them).
    """

    symbol: str
    weight: Decimal
    name: str = ""
    asset_class: str = "equity"


@dataclass(frozen=True)
class TargetWeight:
    """A blended target weight for one symbol across the whole portfolio."""

    symbol: str
    weight: Decimal


@dataclass(frozen=True)
class Position:
    """A holding as reported by the broker."""

    symbol: str
    quantity: Decimal
    market_price: Decimal

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.market_price


@dataclass(frozen=True)
class Lot:
    """One tax lot: shares acquired together at a single cost basis.

    The lot ledger (see :mod:`direct_index.tax.lots`) is the authoritative
    record of cost basis, because most brokerage real-time APIs (IBKR included)
    do not expose per-lot data -- only an average cost per symbol.
    """

    lot_id: str
    symbol: str
    quantity: Decimal
    cost_per_share: Decimal
    acquired: date

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.cost_per_share


@dataclass(frozen=True)
class LotSale:
    """Instruction to sell part or all of a specific lot (for tax reporting)."""

    lot_id: str
    quantity: Decimal
    cost_per_share: Decimal

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.cost_per_share


@dataclass(frozen=True)
class Trade:
    """A desired order produced by the rebalancer.

    For SELLs, ``lots`` carries the tax-lot selection so execution and
    accounting agree on exactly which basis is being realised.
    """

    symbol: str
    side: Side
    quantity: Decimal
    est_price: Decimal
    lots: tuple[LotSale, ...] = ()

    @property
    def est_value(self) -> Decimal:
        return self.quantity * self.est_price


@dataclass(frozen=True)
class Fill:
    """The result of an executed order, fed back into the lot ledger."""

    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    when: date
    lots: tuple[LotSale, ...] = ()


@dataclass
class Account:
    """A snapshot of investable cash plus current positions."""

    cash: Decimal = field(default_factory=lambda: Decimal(0))
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def positions_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), Decimal(0))

    @property
    def investable_value(self) -> Decimal:
        """Cash plus the market value of all positions.

        This is the base against which target weights are turned into target
        dollar amounts.
        """
        return self.cash + self.positions_value

    def quantity_of(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal(0)
