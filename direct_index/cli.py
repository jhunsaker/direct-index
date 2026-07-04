"""Command-line entry point: the scripts you actually run.

Commands (all take ``--config PATH``, default ``direct-index.toml``):

    direct-index targets            show the blended target weight per symbol
    direct-index status             current holdings vs. target, with drift
    direct-index rebalance          plan trades (dry-run); add --execute to send
    direct-index fetch-holdings     refresh/cache each index's constituent data
    direct-index lots [SYMBOL]      show open tax lots
    direct-index set-prices FILE    (paper broker) load a symbol,price CSV

The default is always a dry run: ``rebalance`` prints the orders and the
estimated realised gain but sends nothing until you pass ``--execute``.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from .broker import build_broker
from .config import Config, ConfigError, load_config
from .indexes import build_provider
from .models import BUY, TargetWeight, dec
from .rebalance import blend_targets, drift_report, plan_rebalance
from .tax import apply_reconciliation, diff_positions
from .tax.lots import LotLedger

DEFAULT_CONFIG = "direct-index.toml"


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _load(args) -> Config:
    return load_config(args.config)


def _fetch_targets(config: Config) -> tuple[list[TargetWeight], list[tuple]]:
    """Fetch every index's constituents and blend them into combined targets."""
    per_index = []
    for index in config.indexes:
        provider = build_provider(index)
        constituents = provider.fetch()
        per_index.append((index, constituents))
    return blend_targets(per_index), per_index


def _price_universe(
    targets: list[TargetWeight], account
) -> list[str]:
    return sorted(set(t.symbol for t in targets) | set(account.positions))


def _pct(weight: Decimal) -> str:
    return f"{weight * 100:.2f}%"


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_targets(args) -> int:
    config = _load(args)
    targets, per_index = _fetch_targets(config)

    for index, constituents in per_index:
        print(f"  {index.name}: {len(constituents)} constituents, "
              f"allocation {_pct(index.allocation)}")
    print(f"\nCombined target ({len(targets)} symbols, "
          f"{_pct(config.total_allocation)} invested, "
          f"{_pct(config.cash_target_fraction)} cash):\n")
    print(f"  {'SYMBOL':<8} {'WEIGHT':>8}")
    for t in targets:
        print(f"  {t.symbol:<8} {_pct(t.weight):>8}")
    return 0


def cmd_status(args) -> int:
    config = _load(args)
    targets, _ = _fetch_targets(config)
    with build_broker(config) as broker:
        account = broker.get_account()
        prices = broker.get_prices(_price_universe(targets, account))
    rows = drift_report(targets, account, prices)

    print(f"Investable value: {_money(account.investable_value)}  "
          f"(cash {_money(account.cash)})\n")
    print(f"  {'SYMBOL':<8} {'QTY':>12} {'VALUE':>14} "
          f"{'CURRENT':>9} {'TARGET':>9} {'DRIFT':>9}")
    for r in rows:
        print(f"  {r['symbol']:<8} {r['quantity']:>12} {_money(r['value']):>14} "
              f"{_pct(r['current_weight']):>9} {_pct(r['target_weight']):>9} "
              f"{_pct(r['drift']):>9}")
    return 0


def cmd_rebalance(args) -> int:
    config = _load(args)
    ledger = LotLedger.load(config.resolve(config.tax.ledger_path))
    targets, _ = _fetch_targets(config)

    with build_broker(config) as broker:
        account = broker.get_account()
        prices = broker.get_prices(_price_universe(targets, account))
        plan = plan_rebalance(targets, account, prices, ledger, config)

        _print_plan(plan)
        if not args.execute:
            print("\n(dry run -- pass --execute to submit these orders)")
            return 0
        if not plan.trades:
            return 0

        # Never trade on a stale ledger: mis-synced lots corrupt tax accounting
        # and can leave sells with no lot attribution.
        if not args.skip_reconcile_check and config.reconcile.block_rebalance:
            report = diff_positions(
                ledger,
                {s: p.quantity for s, p in account.positions.items()},
                config.reconcile.tolerance,
            )
            if not report.in_sync:
                print(
                    "\nerror: ledger and broker are out of sync; refusing to "
                    "execute.\n  Run `direct-index reconcile --apply` first, or "
                    "pass --skip-reconcile-check to override.",
                    file=sys.stderr,
                )
                for d in report.mismatches:
                    print(f"    {d.symbol}: ledger {d.ledger_qty} vs broker "
                          f"{d.broker_qty} ({d.kind})", file=sys.stderr)
                return 3

        print("\nExecuting...")
        ledger_path = config.resolve(config.tax.ledger_path)
        for trade in plan.trades:
            fill = broker.execute(trade)
            ledger.apply_fill(fill)
            ledger.save(ledger_path)  # persist after each fill to stay crash-safe
            print(f"  filled {fill.side} {fill.quantity} {fill.symbol} "
                  f"@ {_money(fill.price)}")
    print("Done.")
    return 0


def cmd_fetch_holdings(args) -> int:
    config = _load(args)
    for index in config.indexes:
        provider = build_provider(index)
        constituents = provider.fetch()
        top = constituents[0] if constituents else None
        note = f" (top: {top.symbol} {_pct(top.weight)})" if top else ""
        print(f"  {index.name}: {len(constituents)} constituents{note}")
    return 0


def cmd_lots(args) -> int:
    config = _load(args)
    ledger = LotLedger.load(config.resolve(config.tax.ledger_path))
    symbols = [args.symbol.upper()] if args.symbol else ledger.symbols()
    if not symbols:
        print("No tax lots recorded yet.")
        return 0
    print(f"  {'SYMBOL':<8} {'LOT':<14} {'QTY':>12} {'COST/SH':>10} {'ACQUIRED':>12}")
    for symbol in symbols:
        for lot in sorted(ledger.lots_for(symbol), key=lambda l: -l.cost_per_share):
            print(f"  {lot.symbol:<8} {lot.lot_id:<14} {lot.quantity:>12} "
                  f"{_money(lot.cost_per_share):>10} {lot.acquired.isoformat():>12}")
    return 0


def cmd_reconcile(args) -> int:
    config = _load(args)
    ledger_path = config.resolve(config.tax.ledger_path)
    ledger = LotLedger.load(ledger_path)

    with build_broker(config) as broker:
        account = broker.get_account()
        symbols = sorted(set(ledger.symbols()) | set(account.positions))
        prices = broker.get_prices(symbols)

    broker_qty = {s: p.quantity for s, p in account.positions.items()}
    avg_costs = {
        s: p.avg_cost for s, p in account.positions.items() if p.avg_cost is not None
    }
    report = diff_positions(ledger, broker_qty, config.reconcile.tolerance)
    _print_reconcile(report)

    if not args.apply:
        if not report.in_sync:
            print("\n(diagnostic only -- pass --apply to correct the ledger)")
        return 0
    if report.in_sync:
        return 0

    adjustments = apply_reconciliation(
        ledger,
        report,
        prices=prices,
        avg_costs=avg_costs,
        when=date.today(),
        shortfall_policy=config.reconcile.shortfall_policy,
    )
    ledger.save(ledger_path)
    print("\nApplied:")
    for a in adjustments:
        print(f"  {a.symbol}: {a.action} {a.quantity} -- {a.detail}")
    return 0


def _print_reconcile(report) -> None:
    matched = len(report.discrepancies) - len(report.mismatches)
    if report.in_sync:
        print(f"Ledger and broker are in sync ({matched} symbols).")
        return
    print(f"  {'SYMBOL':<8} {'LEDGER':>14} {'BROKER':>14} {'DELTA':>14}  STATUS")
    for d in report.mismatches:
        print(f"  {d.symbol:<8} {d.ledger_qty:>14} {d.broker_qty:>14} "
              f"{d.delta:>+14}  {d.kind}")
    print(f"\n  {matched} matched, {len(report.mismatches)} out of sync")


def cmd_set_prices(args) -> int:
    config = _load(args)
    if config.broker.type != "paper":
        print("set-prices only applies to the paper broker", file=sys.stderr)
        return 2
    prices = _read_price_csv(Path(args.file))
    broker = build_broker(config)
    broker.set_prices(prices)  # type: ignore[attr-defined]
    print(f"Set {len(prices)} prices.")
    return 0


def _read_price_csv(path: Path) -> dict[str, Decimal]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        return {
            row["symbol"].strip().upper(): dec(row["price"])
            for row in reader
            if row.get("symbol")
        }


def _print_plan(plan) -> None:
    if not plan.trades:
        print("Portfolio is within tolerance; no trades needed.")
    else:
        print(f"  {'SIDE':<5} {'SYMBOL':<8} {'QTY':>12} {'EST VALUE':>14}")
        for t in plan.trades:
            print(f"  {t.side:<5} {t.symbol:<8} {t.quantity:>12} "
                  f"{_money(t.est_value):>14}")
        print(f"\n  buys {_money(plan.est_buy_value)}  "
              f"sells {_money(plan.est_sell_value)}  "
              f"est. realized gain {_money(plan.est_realized_gain)}")
    if plan.skipped:
        print("\n  skipped:")
        for skip in plan.skipped:
            print(f"    {skip.symbol}: {skip.reason}")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="direct-index", description=__doc__)
    parser.add_argument(
        "-c", "--config", default=DEFAULT_CONFIG,
        help=f"path to config TOML (default: {DEFAULT_CONFIG})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("targets", help="show blended target weights").set_defaults(
        func=cmd_targets
    )
    sub.add_parser("status", help="holdings vs. target with drift").set_defaults(
        func=cmd_status
    )
    reb = sub.add_parser("rebalance", help="plan (and optionally execute) trades")
    reb.add_argument("--execute", action="store_true", help="submit the orders")
    reb.add_argument(
        "--skip-reconcile-check", action="store_true",
        help="execute even if the ledger and broker are out of sync (unsafe)",
    )
    reb.set_defaults(func=cmd_rebalance)

    rec = sub.add_parser("reconcile", help="check ledger vs. broker share counts")
    rec.add_argument(
        "--apply", action="store_true",
        help="correct the ledger to match the broker (adds/retires lots)",
    )
    rec.set_defaults(func=cmd_reconcile)

    sub.add_parser("fetch-holdings", help="refresh/cache constituent data").set_defaults(
        func=cmd_fetch_holdings
    )
    lots = sub.add_parser("lots", help="show open tax lots")
    lots.add_argument("symbol", nargs="?", help="limit to one symbol")
    lots.set_defaults(func=cmd_lots)

    sp = sub.add_parser("set-prices", help="(paper) load a symbol,price CSV")
    sp.add_argument("file", help="CSV with symbol,price columns")
    sp.set_defaults(func=cmd_set_prices)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
