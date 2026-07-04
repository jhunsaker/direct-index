"""The rebalancing engine: blend, diff, and produce trades.

Two pure functions carry the design.

``blend_targets`` implements the "positions flow between indexes" requirement.
Rather than tracking which shares belong to which index, we collapse every
index into a single combined target weight per symbol:

    combined_weight(sym) = sum over indexes of  allocation_i * within_weight_i(sym)

A symbol that appears in two indexes simply sums. Because each index's weights
are normalised to 1 and each ``allocation_i`` is a fraction of the portfolio,
the combined weights sum to ``total_allocation`` (<= 1); the remainder is the
intended cash position. There is exactly one target per symbol, so shares are
fungible across indexes by construction.

``plan_rebalance`` turns that target into orders: value each symbol against the
investable portfolio, skip anything inside the drift band or below the minimum
trade size, and for sells ask the tax ledger which (highest-cost) lots to
dispose of. Sells are ordered before buys so proceeds fund the buys.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..config import Config, IndexConfig
from ..indexes.base import Constituents
from ..models import BUY, SELL, Account, TargetWeight, Trade
from ..tax.lots import LotLedger, estimate_realized_gain

# Fractional-share precision we are willing to send to a broker.
_SHARE_QUANTUM = Decimal("0.000001")


def blend_targets(
    indexes: list[tuple[IndexConfig, Constituents]],
) -> list[TargetWeight]:
    """Blend per-index constituents into one combined target weight per symbol."""
    combined: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for index, constituents in indexes:
        for c in constituents:
            combined[c.symbol.upper()] += index.allocation * c.weight
    return [
        TargetWeight(symbol=sym, weight=weight)
        for sym, weight in sorted(combined.items(), key=lambda kv: -kv[1])
    ]


@dataclass(frozen=True)
class Skip:
    """A symbol that was considered but not traded, with the reason."""

    symbol: str
    reason: str


@dataclass(frozen=True)
class RebalancePlan:
    trades: tuple[Trade, ...]
    skipped: tuple[Skip, ...]
    investable_value: Decimal
    est_realized_gain: Decimal

    @property
    def est_buy_value(self) -> Decimal:
        return sum((t.est_value for t in self.trades if t.side == BUY), Decimal(0))

    @property
    def est_sell_value(self) -> Decimal:
        return sum((t.est_value for t in self.trades if t.side == SELL), Decimal(0))


def plan_rebalance(
    targets: list[TargetWeight],
    account: Account,
    prices: dict[str, Decimal],
    ledger: LotLedger,
    config: Config,
) -> RebalancePlan:
    """Produce the set of trades that moves ``account`` toward ``targets``."""
    rc = config.rebalance
    investable = account.investable_value
    if investable <= 0:
        return RebalancePlan((), (), investable, Decimal(0))

    target_by_symbol = {t.symbol: t.weight for t in targets}
    # Consider every target symbol plus anything currently held (held symbols
    # with no target get a target of 0 -> liquidated, which is how shares exit
    # an index that dropped them).
    universe = sorted(set(target_by_symbol) | set(account.positions))

    trades: list[Trade] = []
    skipped: list[Skip] = []
    total_realized = Decimal(0)

    for symbol in universe:
        target_weight = target_by_symbol.get(symbol, Decimal(0))
        current_qty = account.quantity_of(symbol)
        price = prices.get(symbol)

        if price is None or price <= 0:
            # Can't value or trade without a price. Only a problem if we're
            # meant to act on this symbol.
            if target_weight > 0 or current_qty != 0:
                skipped.append(Skip(symbol, "no price available"))
            continue

        current_value = current_qty * price
        current_weight = current_value / investable
        drift = abs(target_weight - current_weight)
        if drift < rc.drift_band:
            continue  # inside the no-trade band

        target_value = target_weight * investable
        delta_value = target_value - current_value
        if abs(delta_value) < rc.min_trade_value:
            skipped.append(Skip(symbol, "below min_trade_value"))
            continue

        raw_shares = abs(delta_value) / price
        qty = _round_shares(raw_shares, rc.allow_fractional)
        if qty <= 0:
            skipped.append(Skip(symbol, "rounds to zero shares"))
            continue

        if delta_value > 0:
            trades.append(Trade(symbol=symbol, side=BUY, quantity=qty, est_price=price))
        else:
            trade = _build_sell(symbol, qty, current_qty, price, ledger)
            trades.append(trade)
            total_realized += estimate_realized_gain(list(trade.lots), price)

    trades.sort(key=_execution_order)
    return RebalancePlan(
        trades=tuple(trades),
        skipped=tuple(skipped),
        investable_value=investable,
        est_realized_gain=total_realized,
    )


def _build_sell(
    symbol: str,
    qty: Decimal,
    current_qty: Decimal,
    price: Decimal,
    ledger: LotLedger,
) -> Trade:
    # Never sell more than we actually hold, even if rounding nudged upward.
    qty = min(qty, current_qty)
    # Lot selection is bounded by what the ledger knows about; if the ledger
    # and broker disagree (they should be reconciled), sell what we can attribute.
    sellable = min(qty, ledger.quantity(symbol))
    lots = tuple(ledger.select_for_sale(symbol, sellable)) if sellable > 0 else ()
    return Trade(symbol=symbol, side=SELL, quantity=qty, est_price=price, lots=lots)


def _round_shares(shares: Decimal, allow_fractional: bool) -> Decimal:
    """Round a share count toward zero so we never over-buy or over-sell."""
    quantum = _SHARE_QUANTUM if allow_fractional else Decimal(1)
    return shares.quantize(quantum, rounding=ROUND_DOWN)


def _execution_order(trade: Trade) -> tuple[int, str]:
    # Sells (0) before buys (1) so proceeds are available to fund purchases.
    return (0 if trade.side == SELL else 1, trade.symbol)


def drift_report(
    targets: list[TargetWeight],
    account: Account,
    prices: dict[str, Decimal],
) -> list[dict]:
    """Per-symbol current vs. target weight, for the ``status`` command."""
    investable = account.investable_value
    target_by_symbol = {t.symbol: t.weight for t in targets}
    universe = sorted(set(target_by_symbol) | set(account.positions))

    rows = []
    for symbol in universe:
        target_weight = target_by_symbol.get(symbol, Decimal(0))
        qty = account.quantity_of(symbol)
        price = prices.get(symbol, Decimal(0))
        value = qty * price
        current_weight = value / investable if investable > 0 else Decimal(0)
        rows.append(
            {
                "symbol": symbol,
                "quantity": qty,
                "price": price,
                "value": value,
                "current_weight": current_weight,
                "target_weight": target_weight,
                "drift": current_weight - target_weight,
            }
        )
    rows.sort(key=lambda r: -r["target_weight"])
    return rows
