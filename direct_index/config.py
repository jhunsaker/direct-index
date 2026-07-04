"""Local configuration: the single source of truth for what to hold.

The config is TOML (read with the stdlib ``tomllib``, so no dependency) and
describes three things:

1. which brokerage to talk to,
2. the set of indexes to track and how the portfolio is split across them,
3. rebalancing and tax-accounting policy.

Allocation semantics
--------------------
Each index's ``allocation`` is its fraction of the *investable* portfolio
(cash + positions). Allocations must sum to <= 1.0; whatever is left
unallocated (``1 - sum``) is deliberately held as cash. So ``0.6 + 0.4`` is
fully invested, while ``0.6 + 0.3`` targets a 10% cash position.

Note there is no per-index position tracking anywhere in the system: the
allocations exist only to blend the indexes into one combined target weight per
symbol (see :mod:`direct_index.rebalance.engine`). Once blended, shares "flow"
freely between indexes.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .models import dec

# Sum-of-allocations tolerance to absorb decimal representation noise.
_ALLOC_EPSILON = Decimal("0.0001")


class ConfigError(ValueError):
    """Raised for any malformed or inconsistent configuration."""


@dataclass(frozen=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 7497  # 7497 = paper TWS, 7496 = live TWS, 4002/4001 = Gateway
    client_id: int = 1
    account: str = ""  # required only when the login has multiple accounts


@dataclass(frozen=True)
class BrokerConfig:
    type: str = "paper"  # "paper" | "ibkr"
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    # Path to the paper broker's persisted state (positions + cash), relative
    # to the config file. Only used when type == "paper".
    paper_state_path: str = "state/paper_account.json"


@dataclass(frozen=True)
class RebalanceConfig:
    # Only trade a symbol whose weight has drifted from target by at least this
    # much (absolute, in weight terms). Prevents churning on tiny moves.
    drift_band: Decimal = Decimal("0.005")
    # Never place an order smaller than this dollar value.
    min_trade_value: Decimal = Decimal("50")
    # Whether the broker/target math may use fractional shares.
    allow_fractional: bool = True


@dataclass(frozen=True)
class TaxConfig:
    strategy: str = "hifo"  # currently only HIFO is implemented
    ledger_path: str = "state/lots.json"


@dataclass(frozen=True)
class IndexConfig:
    name: str
    allocation: Decimal
    provider: str  # "csv" | "ishares"
    # Provider-specific options passed straight through to the provider
    # factory, keeping this module decoupled from provider internals.
    options: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    broker: BrokerConfig
    rebalance: RebalanceConfig
    tax: TaxConfig
    indexes: tuple[IndexConfig, ...]
    # Directory of the config file; used to resolve every relative path.
    base_dir: Path = field(default_factory=Path.cwd)

    @property
    def total_allocation(self) -> Decimal:
        return sum((i.allocation for i in self.indexes), Decimal(0))

    @property
    def cash_target_fraction(self) -> Decimal:
        return Decimal(1) - self.total_allocation

    def resolve(self, relative: str) -> Path:
        """Resolve a config-relative path to an absolute one."""
        p = Path(relative)
        return p if p.is_absolute() else (self.base_dir / p)


def load_config(path: str | Path) -> Config:
    """Load and validate a config file, raising :class:`ConfigError` on issues."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return _parse(raw, base_dir=path.parent.resolve())


def _parse(raw: dict, *, base_dir: Path) -> Config:
    broker = _parse_broker(raw.get("broker", {}))
    rebalance = _parse_rebalance(raw.get("rebalance", {}))
    tax = _parse_tax(raw.get("tax", {}))
    indexes = _parse_indexes(raw.get("index", []))

    if not indexes:
        raise ConfigError("at least one [[index]] must be configured")

    names = [i.name for i in indexes]
    if len(names) != len(set(names)):
        raise ConfigError(f"duplicate index names: {names}")

    total = sum((i.allocation for i in indexes), Decimal(0))
    if total > Decimal(1) + _ALLOC_EPSILON:
        raise ConfigError(
            f"index allocations sum to {total}, which exceeds 1.0; "
            "the unallocated remainder is held as cash, so they must sum to <= 1"
        )

    return Config(
        broker=broker,
        rebalance=rebalance,
        tax=tax,
        indexes=indexes,
        base_dir=base_dir,
    )


def _parse_broker(raw: dict) -> BrokerConfig:
    btype = raw.get("type", "paper")
    if btype not in ("paper", "ibkr"):
        raise ConfigError(f"unknown broker type: {btype!r} (expected paper|ibkr)")
    ib_raw = raw.get("ibkr", {})
    ibkr = IBKRConfig(
        host=ib_raw.get("host", "127.0.0.1"),
        port=int(ib_raw.get("port", 7497)),
        client_id=int(ib_raw.get("client_id", 1)),
        account=ib_raw.get("account", ""),
    )
    return BrokerConfig(
        type=btype,
        ibkr=ibkr,
        paper_state_path=raw.get("paper_state_path", "state/paper_account.json"),
    )


def _parse_rebalance(raw: dict) -> RebalanceConfig:
    drift = dec(raw.get("drift_band", "0.005"))
    if not (Decimal(0) <= drift < Decimal(1)):
        raise ConfigError(f"rebalance.drift_band must be in [0, 1); got {drift}")
    min_trade = dec(raw.get("min_trade_value", "50"))
    if min_trade < 0:
        raise ConfigError("rebalance.min_trade_value must be >= 0")
    return RebalanceConfig(
        drift_band=drift,
        min_trade_value=min_trade,
        allow_fractional=bool(raw.get("allow_fractional", True)),
    )


def _parse_tax(raw: dict) -> TaxConfig:
    strategy = raw.get("strategy", "hifo").lower()
    if strategy != "hifo":
        raise ConfigError(
            f"tax.strategy {strategy!r} not implemented; only 'hifo' is available"
        )
    return TaxConfig(
        strategy=strategy,
        ledger_path=raw.get("ledger_path", "state/lots.json"),
    )


def _parse_indexes(raw_list: list) -> tuple[IndexConfig, ...]:
    indexes = []
    for i, raw in enumerate(raw_list):
        name = raw.get("name")
        if not name:
            raise ConfigError(f"[[index]] #{i} is missing a name")
        provider = raw.get("provider")
        if provider not in ("csv", "ishares"):
            raise ConfigError(
                f"index {name!r}: unknown provider {provider!r} (expected csv|ishares)"
            )
        alloc = dec(raw.get("allocation", "0"))
        if not (Decimal(0) < alloc <= Decimal(1)):
            raise ConfigError(
                f"index {name!r}: allocation must be in (0, 1]; got {alloc}"
            )
        # Everything under the provider's own table is passed through verbatim.
        options = dict(raw.get(provider, {}))
        indexes.append(
            IndexConfig(name=name, allocation=alloc, provider=provider, options=options)
        )
    return tuple(indexes)
