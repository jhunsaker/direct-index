from datetime import date
from decimal import Decimal

from direct_index.models import Fill
from direct_index.tax.lots import LotLedger, estimate_realized_gain, select_hifo


def _d(x):
    return Decimal(str(x))


def make_ledger():
    ledger = LotLedger()
    # Three lots of AAPL at different cost bases.
    ledger.record_buy("AAPL", _d(10), _d(100), date(2023, 1, 1))  # low cost
    ledger.record_buy("AAPL", _d(10), _d(200), date(2023, 6, 1))  # high cost
    ledger.record_buy("AAPL", _d(10), _d(150), date(2024, 1, 1))  # mid cost
    return ledger


def test_hifo_sells_highest_cost_first():
    ledger = make_ledger()
    # Sell 15 shares: should take all 10 @200 then 5 @150.
    sales = ledger.select_for_sale("AAPL", _d(15))
    assert [(s.cost_per_share, s.quantity) for s in sales] == [
        (_d(200), _d(10)),
        (_d(150), _d(5)),
    ]


def test_hifo_minimizes_gain_vs_fifo():
    ledger = make_ledger()
    sales = ledger.select_for_sale("AAPL", _d(10))
    # HIFO picks the $200 lot; selling at $210 realises only $10/sh = $100.
    gain = estimate_realized_gain(sales, _d(210))
    assert gain == _d(100)


def test_record_sell_mutates_ledger():
    ledger = make_ledger()
    ledger.record_sell("AAPL", _d(15))
    # 30 held - 15 sold = 15 remain: the $100 lot (10) + $150 lot partial (5).
    assert ledger.quantity("AAPL") == _d(15)
    remaining = {(l.cost_per_share, l.quantity) for l in ledger.lots_for("AAPL")}
    assert remaining == {(_d(100), _d(10)), (_d(150), _d(5))}


def test_oversell_raises():
    ledger = make_ledger()
    try:
        ledger.select_for_sale("AAPL", _d(31))
    except ValueError:
        pass
    else:
        raise AssertionError("selling more than held should raise")


def test_apply_fill_buy_then_sell():
    ledger = LotLedger()
    ledger.apply_fill(Fill("MSFT", "BUY", _d(5), _d(300), date(2024, 1, 1)))
    ledger.apply_fill(Fill("MSFT", "BUY", _d(5), _d(400), date(2024, 2, 1)))
    consumed = ledger.apply_fill(Fill("MSFT", "SELL", _d(3), _d(500), date(2024, 3, 1)))
    # HIFO: sells from the $400 lot first.
    assert consumed[0].cost_per_share == _d(400)
    assert ledger.quantity("MSFT") == _d(7)


def test_persistence_round_trip(tmp_path):
    ledger = make_ledger()
    path = tmp_path / "lots.json"
    ledger.save(path)
    reloaded = LotLedger.load(path)
    assert reloaded.quantity("AAPL") == _d(30)
    # Selection order survives a round trip.
    assert reloaded.select_for_sale("AAPL", _d(1))[0].cost_per_share == _d(200)


def test_select_hifo_tie_breaks_on_age():
    lots_older = LotLedger()
    lots_older.record_buy("X", _d(5), _d(100), date(2022, 1, 1))
    lots_older.record_buy("X", _d(5), _d(100), date(2023, 1, 1))
    sales = select_hifo(lots_older.lots_for("X"), _d(5))
    # Equal cost -> oldest lot sold first.
    assert sales[0].lot_id.endswith("-1")
