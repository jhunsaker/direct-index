"""Offline verification of the IBKR adapter's translation logic.

We can't reach a live gateway from CI, but ib_async *is* installed, so we drive
IBKRBroker with a fake ``ib`` client that mimics the ib_async API surface the
adapter actually uses. This catches the failure modes a paper smoke test would
otherwise be the first to find: wrong attribute names, NaN market data, sign
conventions, average-fill math, and the execute() timeout/cancel path.

The genuinely un-mockable parts (does BlackRock's endpoint respond, does IBKR
accept a fractional order, is the account funded) still require the live smoke
test in scripts/ibkr_smoke_test.py.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from direct_index.broker.ibkr import IBKRBroker, _avg_fill_price, _usable_price
from direct_index.config import IBKRConfig
from direct_index.models import BUY, SELL, Trade

NAN = float("nan")


# -- fakes mimicking the ib_async surface the adapter touches ---------------
def portfolio_item(symbol, position, price, avg_cost):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        position=position,
        marketPrice=price,
        averageCost=avg_cost,
    )


def account_value(tag, value, currency):
    return SimpleNamespace(tag=tag, value=value, currency=currency)


class FakeTicker:
    def __init__(self, contract, data):
        self.contract = contract
        self.last = data.get("last", NAN)
        self.close = data.get("close", NAN)
        self.bid = data.get("bid", NAN)
        self.ask = data.get("ask", NAN)
        self._market = data.get("market", NAN)

    def marketPrice(self):
        return self._market


class FakeTrade:
    def __init__(self, fills, status, done):
        self.fills = [
            SimpleNamespace(execution=SimpleNamespace(shares=s, price=p))
            for s, p in fills
        ]
        self.orderStatus = SimpleNamespace(status=status)
        self._done = done

    def isDone(self):
        return self._done


class FakeIB:
    def __init__(
        self,
        *,
        portfolio=(),
        account_values=(),
        tickers=None,
        unknown=(),
        fills=(),
        status="Filled",
        done=True,
    ):
        self._portfolio = list(portfolio)
        self._account_values = list(account_values)
        self._tickers = tickers or {}
        self._unknown = set(unknown)
        self._fills = fills
        self._status = status
        self._done = done
        self._connected = True
        self.md_type = None
        self.cancelled = []
        self.placed = []
        self.connect_args = None

    def connect(self, host, port, clientId, readonly):
        self.connect_args = (host, port, clientId, readonly)
        self._connected = True

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def reqMarketDataType(self, n):
        self.md_type = n

    def portfolio(self, account=""):
        return self._portfolio

    def accountValues(self, account=""):
        return self._account_values

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 0 if c.symbol in self._unknown else 100
        return list(contracts)

    def reqTickers(self, *contracts):
        return [FakeTicker(c, self._tickers.get(c.symbol, {})) for c in contracts]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return FakeTrade(self._fills, self._status, self._done)

    def waitOnUpdate(self, timeout=0):
        pass

    def cancelOrder(self, order):
        self.cancelled.append(order)


def broker_with(fake, **cfg_kwargs):
    broker = IBKRBroker(IBKRConfig(**cfg_kwargs))
    broker._ib = fake
    return broker


# -- get_account ------------------------------------------------------------
def test_get_account_maps_positions_and_cash():
    fake = FakeIB(
        portfolio=[
            portfolio_item("AAPL", 10, 190.5, 175.0),
            portfolio_item("MSFT", 5, 410.0, 0.0),  # avg cost 0 -> None
        ],
        account_values=[
            account_value("NetLiquidation", "50000", "USD"),  # noise
            account_value("TotalCashValue", "12345.67", "USD"),
        ],
    )
    account = broker_with(fake).get_account()

    assert account.cash == Decimal("12345.67")
    assert account.positions["AAPL"].quantity == Decimal("10")
    assert account.positions["AAPL"].avg_cost == Decimal("175.0")
    assert account.positions["AAPL"].market_value == Decimal("1905.0")
    # averageCost of 0 must not become a bogus 0-cost basis.
    assert account.positions["MSFT"].avg_cost is None


def test_cash_defaults_to_zero_when_tag_absent():
    fake = FakeIB(account_values=[account_value("NetLiquidation", "50000", "USD")])
    assert broker_with(fake).get_account().cash == Decimal("0")


# -- get_prices -------------------------------------------------------------
def test_get_prices_prefers_last_skips_nan_and_falls_back_to_mid():
    fake = FakeIB(
        tickers={
            "AAPL": {"last": 190.5, "close": 188.0},        # uses last
            "MSFT": {"last": NAN, "close": 410.0},          # NaN last -> close
            "GOOG": {"bid": 170.0, "ask": 172.0},           # only bid/ask -> mid
            "DEAD": {"last": NAN, "close": NAN},            # nothing usable
        },
    )
    prices = broker_with(fake).get_prices(["AAPL", "MSFT", "GOOG", "DEAD"])
    assert prices == {
        "AAPL": Decimal("190.5"),
        "MSFT": Decimal("410.0"),
        "GOOG": Decimal("171.0"),
    }
    assert "DEAD" not in prices  # no price is omitted, never zero


def test_get_prices_drops_unqualified_symbols():
    fake = FakeIB(tickers={"AAPL": {"last": 190.5}}, unknown={"BOGUS"})
    prices = broker_with(fake).get_prices(["AAPL", "BOGUS"])
    assert set(prices) == {"AAPL"}


# -- execute ----------------------------------------------------------------
def test_execute_returns_average_fill_price():
    fake = FakeIB(fills=[(3, 100.0), (2, 110.0)], status="Filled", done=True)
    trade = Trade("AAPL", BUY, Decimal("5"), Decimal("105"))
    fill = broker_with(fake).execute(trade)
    assert fill.side == BUY and fill.symbol == "AAPL"
    assert fill.quantity == Decimal("5")
    assert fill.price == Decimal("104")  # (3*100 + 2*110) / 5


def test_execute_times_out_and_cancels():
    fake = FakeIB(fills=[], done=False)  # never reaches terminal state
    broker = broker_with(fake, order_timeout=0.05)
    with pytest.raises(TimeoutError):
        broker.execute(Trade("AAPL", BUY, Decimal("1"), Decimal("100")))
    assert len(fake.cancelled) == 1  # stray order was cancelled


def test_execute_raises_when_nothing_fills():
    fake = FakeIB(fills=[], status="Cancelled", done=True)
    with pytest.raises(RuntimeError, match="did not fill"):
        broker_with(fake).execute(Trade("AAPL", SELL, Decimal("1"), Decimal("100")))


# -- connect primes delayed market data -------------------------------------
def test_connect_sets_market_data_type(monkeypatch):
    fake = FakeIB()
    import ib_async

    monkeypatch.setattr(ib_async, "IB", lambda: fake)
    broker = IBKRBroker(IBKRConfig(market_data_type=3))
    broker.connect()
    assert fake.connect_args[:3] == ("127.0.0.1", 7497, 1)
    assert fake.md_type == 3  # delayed data requested so paper accounts work


# -- pure helpers -----------------------------------------------------------
def test_usable_price_rejects_nan_and_nonpositive():
    assert _usable_price(FakeTicker(None, {"last": NAN, "close": -5})) is None
    assert _usable_price(FakeTicker(None, {"last": 0, "close": 0})) is None
    assert _usable_price(FakeTicker(None, {"market": 55.0})) == Decimal("55.0")


def test_avg_fill_price_zero_when_no_fills():
    assert _avg_fill_price(FakeTrade([], "Cancelled", True)) == Decimal("0")
