from datetime import date
from decimal import Decimal

from direct_index.tax.lots import LotLedger
from direct_index.tax.reconcile import (
    MATCH,
    SHORTFALL,
    SURPLUS,
    apply_reconciliation,
    diff_positions,
)


def _d(x):
    return Decimal(str(x))


def ledger_with(**symbol_to_qty):
    ledger = LotLedger()
    for sym, qty in symbol_to_qty.items():
        ledger.record_buy(sym, _d(qty), _d(100), date(2023, 1, 1))
    return ledger


def apply(ledger, report, *, prices=None, avg_costs=None, policy="fifo"):
    return apply_reconciliation(
        ledger,
        report,
        prices=prices or {},
        avg_costs=avg_costs or {},
        when=date(2026, 7, 4),
        shortfall_policy=policy,
    )


# -- diff -------------------------------------------------------------------
def test_in_sync_when_equal():
    ledger = ledger_with(AAPL=10, MSFT=5)
    report = diff_positions(ledger, {"AAPL": _d(10), "MSFT": _d(5)})
    assert report.in_sync
    assert all(d.kind == MATCH for d in report.discrepancies)


def test_tolerance_absorbs_dust():
    ledger = ledger_with(AAPL=10)
    report = diff_positions(ledger, {"AAPL": _d("10.0000005")}, tolerance=_d("0.000001"))
    assert report.in_sync


def test_classifies_surplus_and_shortfall():
    ledger = ledger_with(AAPL=10, MSFT=5)
    report = diff_positions(ledger, {"AAPL": _d(12), "MSFT": _d(3)})
    kinds = {d.symbol: d.kind for d in report.mismatches}
    assert kinds == {"AAPL": SURPLUS, "MSFT": SHORTFALL}


def test_broker_only_symbol_is_surplus():
    ledger = ledger_with(AAPL=10)
    report = diff_positions(ledger, {"AAPL": _d(10), "TSLA": _d(4)})
    tsla = next(d for d in report.mismatches if d.symbol == "TSLA")
    assert tsla.kind == SURPLUS and tsla.delta == _d(4)


def test_ledger_only_symbol_is_shortfall():
    ledger = ledger_with(AAPL=10, XYZ=7)
    report = diff_positions(ledger, {"AAPL": _d(10)})  # broker has no XYZ
    xyz = next(d for d in report.mismatches if d.symbol == "XYZ")
    assert xyz.kind == SHORTFALL and xyz.delta == _d(-7)


# -- apply: restores the invariant ledger == broker -------------------------
def test_surplus_opens_lot_at_avg_cost():
    ledger = ledger_with(AAPL=10)
    report = diff_positions(ledger, {"AAPL": _d(13)})
    adj = apply(ledger, report, avg_costs={"AAPL": _d(190)}, prices={"AAPL": _d(200)})
    assert ledger.quantity("AAPL") == _d(13)
    # The new 3-share lot uses the broker average cost, not market price.
    (added,) = [a for a in adj if a.action == "added_lot"]
    assert added.quantity == _d(3)
    new_lot = max(ledger.lots_for("AAPL"), key=lambda l: l.lot_id)
    assert new_lot.cost_per_share == _d(190)


def test_surplus_falls_back_to_price_without_avg_cost():
    ledger = ledger_with(AAPL=10)
    report = diff_positions(ledger, {"AAPL": _d(11)})
    apply(ledger, report, prices={"AAPL": _d(200)})
    new_lot = max(ledger.lots_for("AAPL"), key=lambda l: l.lot_id)
    assert new_lot.cost_per_share == _d(200)


def test_surplus_without_basis_is_skipped_not_fabricated():
    ledger = ledger_with(AAPL=10)
    report = diff_positions(ledger, {"AAPL": _d(11)})
    adj = apply(ledger, report)  # no avg cost, no price
    assert adj[0].action == "skipped"
    assert ledger.quantity("AAPL") == _d(10)  # unchanged; no invented basis


def test_shortfall_retires_shares_no_gain():
    ledger = LotLedger()
    ledger.record_buy("AAPL", _d(6), _d(100), date(2022, 1, 1))  # oldest
    ledger.record_buy("AAPL", _d(6), _d(200), date(2024, 1, 1))
    report = diff_positions(ledger, {"AAPL": _d(8)})  # 12 held, broker says 8
    apply(ledger, report, policy="fifo")
    assert ledger.quantity("AAPL") == _d(8)
    # FIFO retired 4 shares from the oldest ($100) lot; the $200 lot is intact.
    by_cost = {l.cost_per_share: l.quantity for l in ledger.lots_for("AAPL")}
    assert by_cost == {_d(100): _d(2), _d(200): _d(6)}


def test_apply_makes_everything_match():
    ledger = ledger_with(AAPL=10, MSFT=5, XYZ=7)
    broker = {"AAPL": _d(12), "MSFT": _d(3), "TSLA": _d(4)}  # +AAPL, -MSFT, -XYZ, +TSLA
    report = diff_positions(ledger, broker)
    apply(
        ledger,
        report,
        prices={"AAPL": _d(200), "TSLA": _d(300)},
    )
    after = diff_positions(ledger, broker)
    assert after.in_sync  # invariant restored across all symbols
