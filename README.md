# direct-index

Manage investments in indexes by **directly holding the constituents** rather
than buying an ETF. A set of command-line scripts that connect to a brokerage,
check how the target weights have drifted as prices move, and submit trades to
rebalance — selling the highest-cost tax lots first to minimise realised gains.

## Why direct indexing

Holding the underlying names (instead of a fund) lets you harvest tax losses at
the individual-stock level, tilt or exclude specific names, and blend several
indexes into one portfolio. The cost is operational complexity — many small
positions, fractional shares, drift tracking, and lot-level tax accounting —
which is exactly what this tool automates.

## Architecture

Five modules mirror the system's responsibilities:

| Module | Package | Responsibility |
|---|---|---|
| #1 Constituent data | [`indexes/`](direct_index/indexes/) | Fetch each index's members + weights (iShares holdings, or local CSV) |
| #2 Positions | [`broker/`](direct_index/broker/) | Read current holdings + cash |
| #3 Trades | [`broker/`](direct_index/broker/) | Submit orders |
| #4 Multi-index management | [`rebalance/`](direct_index/rebalance/engine.py) | Blend indexes → one target → diff → trades |
| #5 Tax accounting | [`tax/`](direct_index/tax/lots.py) | Cost-basis lot ledger, HIFO sell selection |

### The key design decision: positions flow between indexes

We **never** track which shares belong to which index. Instead every index is
collapsed into a single combined target weight per symbol, weighted by that
index's capital allocation:

```
combined_weight(sym) = Σ  allocation_i × within_index_weight_i(sym)
```

A symbol in two indexes (e.g. NVDA in both a US and an international index)
simply sums. Rebalancing is then one comparison — combined target vs. actual
holdings — and shares are fungible across indexes by construction. This is
[`blend_targets`](direct_index/rebalance/engine.py).

### Tax lots (HIFO)

Brokerage real-time APIs (IBKR included) report only an *average* cost per
symbol, not individual lots — so we keep the authoritative lot ledger locally.
Every buy fill opens a lot; every sell consumes the **highest-cost lots first**
([`select_hifo`](direct_index/tax/lots.py)), which minimises the realised gain.
Set your IBKR account's lot-matching method to "Highest Cost" so the broker's
own tax reporting agrees with the ledger.

## Quick start (fully offline, no brokerage needed)

The default config uses a built-in **paper broker** and local CSV constituents,
so the entire pipeline runs with zero external services or credentials.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                                    # 22 tests, all offline

# Blend the two example indexes into one target
direct-index targets

# Fund the paper account and load prices, then rebalance
python -c "from direct_index.config import load_config; from direct_index.broker import build_broker; from decimal import Decimal; build_broker(load_config('direct-index.toml')).deposit(Decimal('100000'))"
direct-index set-prices examples/prices.csv
direct-index rebalance                    # dry run — prints the orders
direct-index rebalance --execute          # submit them (to the paper broker)

direct-index status                        # holdings vs. target, with drift
direct-index lots                          # open tax lots
```

## Commands

| Command | Purpose |
|---|---|
| `direct-index targets` | Show the blended target weight per symbol |
| `direct-index status` | Current holdings vs. target, with drift |
| `direct-index rebalance` | Plan trades (dry run); `--execute` to submit |
| `direct-index fetch-holdings` | Refresh/cache each index's constituent data |
| `direct-index lots [SYMBOL]` | Show open tax lots |
| `direct-index set-prices FILE` | (paper broker) load a `symbol,price` CSV |

All commands take `-c/--config PATH` (default `direct-index.toml`). `rebalance`
is a **dry run by default** — it never sends orders without `--execute`.

## Configuration

See [`direct-index.toml`](direct-index.toml). Each index gets an `allocation`
(its fraction of the investable portfolio); allocations must sum to ≤ 1, and any
remainder is deliberately held as cash. The `drift_band` and `min_trade_value`
suppress churn on small moves.

## Going live with Interactive Brokers

1. Install the integration: `pip install -e '.[ibkr]'`
2. Run **Trader Workstation** or **IB Gateway**, log in, and enable the API
   (Configure → API → Settings). Note the port (7497 paper TWS, 4002 paper
   Gateway).
3. Point an index at a real iShares holdings CSV URL and switch the broker:

   ```toml
   [broker]
   type = "ibkr"
   [broker.ibkr]
   port = 7497        # 7497 paper TWS / 4002 paper Gateway
   account = "DU1234567"

   [[index]]
   name = "us_large_cap"
   allocation = 0.6
   provider = "ishares"
   [index.ishares]
   holdings_url = "https://www.ishares.com/us/products/239726/.../IVV_holdings.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
   ```

Always test against a **paper account** first.

## Status: what's verified vs. what needs live testing

Built as a design + scaffold. The core is production-shaped and covered by
tests; the two external integrations are written against real APIs but require
live services to exercise.

- ✅ **Tested & runnable offline:** the rebalance engine (blending, drift,
  trade generation), the HIFO tax ledger, config parsing, the CSV provider, the
  iShares CSV *parser*, and the paper broker — exercised by 22 unit tests and
  the end-to-end CLI flow above.
- ⚠️ **Written, needs live smoke test:** the Interactive Brokers adapter
  ([`broker/ibkr.py`](direct_index/broker/ibkr.py)) needs a running Gateway; its
  market-data and fractional-order paths in particular should be validated on a
  paper account. The iShares *network fetch* depends on BlackRock's endpoint and
  CSV layout, which drift over time — re-check the parser against a live
  download.

## Safety notes

This is early-stage software that can place real trades. It has no order-rate
limiting, no market-hours guard, no partial-fill retry loop, and no
reconciliation between the local lot ledger and broker-reported share counts —
all of which you want before trading meaningful capital. Start on paper.
