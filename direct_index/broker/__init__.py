"""Modules #2 and #3: read positions and submit trades.

A single :class:`~direct_index.broker.base.BrokerClient` covers both, since a
broker connection naturally does both. :func:`build_broker` maps config to a
concrete client.
"""

from __future__ import annotations

from ..config import Config
from .base import BrokerClient
from .paper import PaperBroker


def build_broker(config: Config) -> BrokerClient:
    """Instantiate the broker named by the config."""
    if config.broker.type == "paper":
        return PaperBroker(state_path=config.resolve(config.broker.paper_state_path))
    if config.broker.type == "ibkr":
        # Imported lazily so the package works without ib_async installed.
        from .ibkr import IBKRBroker

        return IBKRBroker(config.broker.ibkr)
    raise ValueError(f"unknown broker type: {config.broker.type!r}")


__all__ = ["BrokerClient", "PaperBroker", "build_broker"]
