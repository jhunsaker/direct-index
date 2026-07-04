from datetime import date
from decimal import Decimal
from pathlib import Path

from direct_index.config import (
    BrokerConfig,
    Config,
    IndexConfig,
    RebalanceConfig,
    TaxConfig,
)
from direct_index.models import Account, Constituent, Position
from direct_index.rebalance import blend_targets, plan_rebalance
from direct_index.tax.lots import LotLedger


def _d(x):
    return Decimal(str(x))


def cfg(*, drift="0.005", min_trade="50", fractional=True, indexes=()):
    return Config(
        broker=BrokerConfig(),
        rebalance=RebalanceConfig(
            drift_band=_d(drift),
            min_trade_value=_d(min_trade),
            allow_fractional=fractional,
        ),
        tax=TaxConfig(),
        indexes=tuple(indexes),
        base_dir=Path.cwd(),
    )


def idx(name, allocation):
    return IndexConfig(name=name, allocation=_d(allocation), provider="csv", options={})


# -- blending (the "positions flow between indexes" core) -------------------
def test_blend_sums_overlapping_symbols():
    a = (idx("a", "0.6"), [Constituent("AAPL", _d("0.5")), Constituent("MSFT", _d("0.5"))])
    b = (idx("b", "0.4"), [Constituent("AAPL", _d("1.0"))])
    targets = {t.symbol: t.weight for t in blend_targets([a, b])}
    # AAPL is held by both indexes and its weights add.
    assert targets["AAPL"] == _d("0.7")  # 0.6*0.5 + 0.4*1.0
    assert targets["MSFT"] == _d("0.3")  # 0.6*0.5
    assert sum(targets.values()) == _d("1.0")


def test_blend_partial_allocation_leaves_cash():
    a = (idx("a", "0.6"), [Constituent("AAPL", _d("1.0"))])
    targets = blend_targets([a])
    # 60% allocated -> combined weights sum to 0.6, remaining 40% is cash.
    assert sum(t.weight for t in targets) == _d("0.6")


# -- planning ---------------------------------------------------------------
def test_all_cash_buys_to_target():
    targets = blend_targets([
        (idx("a", "1.0"), [Constituent("AAPL", _d("0.7")), Constituent("MSFT", _d("0.3"))]),
    ])
    account = Account(cash=_d(10000))
    prices = {"AAPL": _d(100), "MSFT": _d(100)}
    plan = plan_rebalance(targets, account, prices, LotLedger(), cfg())

    by_symbol = {t.symbol: t for t in plan.trades}
    assert by_symbol["AAPL"].side == "BUY" and by_symbol["AAPL"].quantity == _d(70)
    assert by_symbol["MSFT"].side == "BUY" and by_symbol["MSFT"].quantity == _d(30)
    assert plan.est_buy_value == _d(10000)


def test_within_drift_band_no_trade():
    targets = blend_targets([(idx("a", "1.0"), [Constituent("AAPL", _d("1.0"))])])
    # Already 100% AAPL, exactly on target.
    account = Account(cash=_d(0), positions={"AAPL": Position("AAPL", _d(100), _d(100))})
    plan = plan_rebalance(targets, account, {"AAPL": _d(100)}, LotLedger(), cfg())
    assert plan.trades == ()


def test_sell_uses_hifo_lots():
    targets = blend_targets([(idx("a", "0.7"), [Constituent("AAPL", _d("1.0"))])])
    account = Account(cash=_d(0), positions={"AAPL": Position("AAPL", _d(100), _d(100))})
    ledger = LotLedger()
    ledger.record_buy("AAPL", _d(50), _d(80), date(2023, 1, 1))
    ledger.record_buy("AAPL", _d(50), _d(120), date(2023, 6, 1))

    plan = plan_rebalance(targets, account, {"AAPL": _d(100)}, ledger, cfg())
    (trade,) = plan.trades
    # investable 10000, target 0.7 -> 7000 -> sell 30 shares.
    assert trade.side == "SELL" and trade.quantity == _d(30)
    # HIFO: all 30 come from the $120 lot; realised gain is a $600 loss at $100.
    assert trade.lots[0].cost_per_share == _d(120)
    assert plan.est_realized_gain == _d(-600)


def test_held_symbol_not_in_any_index_is_liquidated():
    targets = blend_targets([(idx("a", "1.0"), [Constituent("AAPL", _d("1.0"))])])
    account = Account(
        cash=_d(0),
        positions={
            "AAPL": Position("AAPL", _d(70), _d(100)),
            "TSLA": Position("TSLA", _d(30), _d(100)),  # no longer in any index
        },
    )
    plan = plan_rebalance(targets, account, {"AAPL": _d(100), "TSLA": _d(100)}, LotLedger(), cfg())
    tsla = next(t for t in plan.trades if t.symbol == "TSLA")
    assert tsla.side == "SELL" and tsla.quantity == _d(30)  # sold to zero


def test_whole_share_rounding_floors():
    targets = blend_targets([(idx("a", "1.0"), [Constituent("AAPL", _d("1.0"))])])
    account = Account(cash=_d(1000))
    plan = plan_rebalance(
        targets, account, {"AAPL": _d(300)}, LotLedger(), cfg(fractional=False)
    )
    (trade,) = plan.trades
    # 1000/300 = 3.33 shares -> floored to 3 whole shares.
    assert trade.quantity == _d(3)


def test_missing_price_is_skipped_not_crashed():
    targets = blend_targets([(idx("a", "1.0"), [Constituent("XYZ", _d("1.0"))])])
    account = Account(cash=_d(1000))
    plan = plan_rebalance(targets, account, {}, LotLedger(), cfg())
    assert plan.trades == ()
    assert any(s.symbol == "XYZ" and "price" in s.reason for s in plan.skipped)


def test_sells_ordered_before_buys():
    targets = blend_targets([
        (idx("a", "1.0"), [Constituent("AAPL", _d("0.5")), Constituent("MSFT", _d("0.5"))]),
    ])
    # Overweight AAPL (sell), underweight MSFT (buy).
    account = Account(
        cash=_d(0),
        positions={
            "AAPL": Position("AAPL", _d(90), _d(100)),
            "MSFT": Position("MSFT", _d(10), _d(100)),
        },
    )
    plan = plan_rebalance(targets, account, {"AAPL": _d(100), "MSFT": _d(100)}, LotLedger(), cfg())
    sides = [t.side for t in plan.trades]
    assert sides.index("SELL") < sides.index("BUY")
