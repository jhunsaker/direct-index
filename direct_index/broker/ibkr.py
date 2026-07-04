"""Interactive Brokers adapter via ``ib_async``.

This is the real-world broker implementation. It talks to a running **Trader
Workstation** or **IB Gateway** over the local socket API -- there is no
cloud REST endpoint, so a gateway process must be running and logged in for any
of this to work. Because that requires a live session, this adapter is written
out in full but is the one part of the system that cannot be exercised by the
offline test suite; treat the market-data and fractional-order paths as
needing a paper-account smoke test before you trust them with real money.

Install the integration with::

    pip install -e '.[ibkr]'

Ports (set in TWS/Gateway API settings): 7497 paper TWS, 7496 live TWS,
4002 paper Gateway, 4001 live Gateway.

Tax lots
--------
The IBKR real-time API exposes only an *average* cost per symbol, not
individual lots, so it cannot drive HIFO on its own. We keep the authoritative
lot ledger locally (:mod:`direct_index.tax.lots`) and, for the broker's own
tax-lot matching, you should set the IBKR account's default lot-matching method
to "Highest Cost" so the broker's realised-gain reporting agrees with ours.
Per-order specific-lot assignment is not reliably available through this API.
"""

from __future__ import annotations

from decimal import Decimal

from ..config import IBKRConfig
from ..models import Account, Fill, Position, Trade, dec
from .base import BrokerBase

# Fields on the account summary that represent spendable cash.
_CASH_TAG = "TotalCashValue"


class IBKRBroker(BrokerBase):
    def __init__(self, cfg: IBKRConfig) -> None:
        self.cfg = cfg
        self._ib = None  # set on connect()

    # -- connection --------------------------------------------------------
    def connect(self) -> None:
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "the IBKR broker requires ib_async; install with "
                "`pip install -e '.[ibkr]'`"
            ) from exc
        self._ib = IB()
        self._ib.connect(
            self.cfg.host,
            self.cfg.port,
            clientId=self.cfg.client_id,
            readonly=False,
        )

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._ib = None

    @property
    def ib(self):
        if self._ib is None or not self._ib.isConnected():
            raise RuntimeError("IBKR broker is not connected; use as a context manager")
        return self._ib

    # -- module #2: positions + cash --------------------------------------
    def get_account(self) -> Account:
        cash = self._cash_balance()
        positions: dict[str, Position] = {}
        for item in self.ib.portfolio(self.cfg.account or ""):
            symbol = item.contract.symbol.upper()
            positions[symbol] = Position(
                symbol=symbol,
                quantity=dec(item.position),
                market_price=dec(item.marketPrice),
                # IBKR reports an average cost per share; reconciliation uses it
                # as the basis for any surplus shares it has to book.
                avg_cost=dec(item.averageCost) if item.averageCost else None,
            )
        return Account(cash=cash, positions=positions)

    def _cash_balance(self) -> Decimal:
        for row in self.ib.accountSummary(self.cfg.account or ""):
            if row.tag == _CASH_TAG and row.currency in ("USD", "BASE", ""):
                return dec(row.value)
        return Decimal(0)

    # -- prices ------------------------------------------------------------
    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        from ib_async import Stock

        contracts = [Stock(s.upper(), "SMART", "USD") for s in symbols]
        self.ib.qualifyContracts(*contracts)
        tickers = self.ib.reqTickers(*contracts)
        prices: dict[str, Decimal] = {}
        for ticker in tickers:
            price = _usable_price(ticker)
            if price is not None:
                prices[ticker.contract.symbol.upper()] = price
        return prices

    # -- module #3: submit trades -----------------------------------------
    def execute(self, trade: Trade) -> Fill:
        from ib_async import MarketOrder, Stock

        contract = Stock(trade.symbol.upper(), "SMART", "USD")
        self.ib.qualifyContracts(contract)
        order = MarketOrder(trade.side, float(trade.quantity))
        if self.cfg.account:
            order.account = self.cfg.account

        ib_trade = self.ib.placeOrder(contract, order)
        # Block until the order reaches a terminal state.
        while not ib_trade.isDone():
            self.ib.waitOnUpdate(timeout=5)

        filled = sum((dec(f.execution.shares) for f in ib_trade.fills), Decimal(0))
        avg_price = _avg_fill_price(ib_trade)
        if filled <= 0:
            raise RuntimeError(
                f"IBKR order for {trade.symbol} did not fill: "
                f"{ib_trade.orderStatus.status}"
            )
        return Fill(
            symbol=trade.symbol.upper(),
            side=trade.side,
            quantity=filled,
            price=avg_price,
            when=_today(),
            lots=trade.lots,
        )


def _usable_price(ticker) -> Decimal | None:
    """Prefer last trade, then close, then mid; ignore NaN sentinels."""
    for value in (ticker.last, ticker.close, ticker.marketPrice()):
        if value is not None and value == value and value > 0:  # value==value: not NaN
            return dec(value)
    bid, ask = ticker.bid, ticker.ask
    if bid and ask and bid == bid and ask == ask and bid > 0 and ask > 0:
        return dec((bid + ask) / 2)
    return None


def _avg_fill_price(ib_trade) -> Decimal:
    shares = Decimal(0)
    notional = Decimal(0)
    for f in ib_trade.fills:
        qty = dec(f.execution.shares)
        shares += qty
        notional += qty * dec(f.execution.price)
    return notional / shares if shares else Decimal(0)


def _today():
    from datetime import date

    return date.today()
