#!/usr/bin/env python3
"""Live smoke test for the Interactive Brokers adapter.

Run this against a **paper** IB Gateway / TWS to verify the parts of
``direct_index.broker.ibkr`` that only a live connection can exercise:
connection, account + cash read, market-data prices, and (optionally) a real
order round-trip. It drives the same ``IBKRBroker`` the app uses, so a pass here
means the app can talk to your gateway.

It does NOT touch your config, lot ledger, or index data -- it is a pure
connectivity/adapter check.

Prerequisites
-------------
1. ``pip install -e '.[ibkr]'``
2. IB Gateway or TWS running, logged into a PAPER account, with the API enabled
   (Configure -> API -> Settings -> Enable ActiveX and Socket Clients) and the
   port noted (7497 paper TWS, 4002 paper Gateway).

Examples
--------
Read-only checks (no orders placed)::

    python scripts/ibkr_smoke_test.py --port 7497

Full round-trip (buys then sells 1 share to return to flat)::

    python scripts/ibkr_smoke_test.py --port 7497 --allow-order --symbol F

Placing orders requires the explicit ``--allow-order`` flag; doing so against a
*live* port additionally requires ``--allow-live``.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from direct_index.broker.ibkr import IBKRBroker
from direct_index.config import IBKRConfig
from direct_index.models import BUY, SELL, Trade

LIVE_PORTS = {7496, 4001}


class Checklist:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
        self.results.append((name, ok, detail))

    def step(self, name: str, fn):
        """Run fn(); record PASS with its return detail, or FAIL on exception."""
        try:
            detail = fn() or ""
            self.record(name, True, detail)
            return True
        except Exception as exc:  # noqa: BLE001 - smoke test reports, never crashes
            self.record(name, False, f"{type(exc).__name__}: {exc}")
            return False

    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.results)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.port in LIVE_PORTS and args.allow_order and not args.allow_live:
        print(
            f"refusing to place orders on live port {args.port} without "
            "--allow-live",
            file=sys.stderr,
        )
        return 2

    cfg = IBKRConfig(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        account=args.account,
        market_data_type=args.market_data_type,
        order_timeout=args.order_timeout,
    )
    print(f"Connecting to {cfg.host}:{cfg.port} (clientId {cfg.client_id})...")
    if args.port in LIVE_PORTS:
        print("  WARNING: this looks like a LIVE trading port.")

    check = Checklist()
    broker = IBKRBroker(cfg)

    if not check.step("connect", broker.connect):
        print("\nCould not connect; is the gateway running with the API enabled?")
        return 1

    try:
        account = _run_reads(broker, check, args.symbol)
        if args.allow_order:
            _run_order_roundtrip(broker, check, args)
        else:
            print("  [skip] order round-trip (pass --allow-order to enable)")
    finally:
        check.step("disconnect", broker.disconnect)

    passed = sum(1 for _, ok, _ in check.results if ok)
    print(f"\n{passed}/{len(check.results)} checks passed.")
    return 0 if check.ok() else 1


def _run_reads(broker: IBKRBroker, check: Checklist, symbol: str):
    account = {}

    def read_account():
        acct = broker.get_account()
        account["acct"] = acct
        return (
            f"cash {acct.cash}, {len(acct.positions)} positions"
        )

    check.step("get_account (cash + positions)", read_account)

    def read_prices():
        prices = broker.get_prices([symbol])
        if symbol.upper() not in prices:
            raise RuntimeError(
                f"no price for {symbol} (market-data subscription or data type?)"
            )
        return f"{symbol} = {prices[symbol.upper()]}"

    check.step(f"get_prices([{symbol}])", read_prices)
    return account.get("acct")


def _run_order_roundtrip(broker: IBKRBroker, check: Checklist, args) -> None:
    qty = Decimal(str(args.qty))
    price = broker.get_prices([args.symbol]).get(args.symbol.upper(), Decimal(0))

    def buy():
        fill = broker.execute(Trade(args.symbol.upper(), BUY, qty, price))
        return f"bought {fill.quantity} @ {fill.price}"

    bought = check.step(f"execute BUY {qty} {args.symbol}", buy)

    if bought and not args.no_flatten:
        def sell():
            fill = broker.execute(Trade(args.symbol.upper(), SELL, qty, price))
            return f"sold {fill.quantity} @ {fill.price} (returned to flat)"

        check.step(f"execute SELL {qty} {args.symbol} (flatten)", sell)
    elif bought:
        print(f"  NOTE: --no-flatten set; you now hold {qty} {args.symbol}.")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="7497 paper TWS, 4002 paper GW")
    p.add_argument("--client-id", type=int, default=1)
    p.add_argument("--account", default="", help="required if the login has multiple")
    p.add_argument("--symbol", default="F", help="test symbol (default: F, a low-priced liquid stock)")
    p.add_argument("--qty", default="1", help="order quantity (default: 1 share)")
    p.add_argument("--market-data-type", type=int, default=3, choices=(1, 2, 3, 4))
    p.add_argument("--order-timeout", type=float, default=60.0)
    p.add_argument("--allow-order", action="store_true", help="place a real order round-trip")
    p.add_argument("--allow-live", action="store_true", help="permit orders on a live port")
    p.add_argument("--no-flatten", action="store_true", help="keep the bought shares")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
