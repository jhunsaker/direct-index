"""The broker interface used by the rebalancer and CLI.

Every broker is a context manager so connections (IBKR holds a live socket) are
opened and torn down deterministically::

    with build_broker(config) as broker:
        account = broker.get_account()
        prices = broker.get_prices(symbols)
        fill = broker.execute(trade)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..models import Account, Fill, Trade


@runtime_checkable
class BrokerClient(Protocol):
    def connect(self) -> None:
        """Establish the connection (no-op for stateless brokers)."""
        ...

    def disconnect(self) -> None:
        """Tear the connection down."""
        ...

    def get_account(self) -> Account:
        """Return cash plus current positions (module #2)."""
        ...

    def get_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        """Return the latest price for each requested symbol.

        A symbol with no available price is omitted from the result; callers
        must treat a missing price as "cannot trade this symbol", not as zero.
        """
        ...

    def execute(self, trade: Trade) -> Fill:
        """Submit one order and return the resulting fill (module #3)."""
        ...

    # Context-manager sugar; concrete classes inherit these by also subclassing
    # BrokerBase below, or implement their own.
    def __enter__(self) -> "BrokerClient": ...
    def __exit__(self, *exc: object) -> None: ...


class BrokerBase:
    """Mixin providing context-manager behaviour on top of connect/disconnect."""

    def connect(self) -> None:  # pragma: no cover - trivial default
        pass

    def disconnect(self) -> None:  # pragma: no cover - trivial default
        pass

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()
