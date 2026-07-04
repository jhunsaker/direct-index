"""Module #1: index constituent data.

A :class:`~direct_index.indexes.base.ConstituentProvider` returns the members of
one index and their within-index weights. :func:`build_provider` maps a
:class:`~direct_index.config.IndexConfig` to a concrete provider instance.
"""

from __future__ import annotations

from ..config import IndexConfig
from .base import Constituents, ConstituentProvider
from .csv_provider import CSVConstituentProvider
from .ishares import ISharesProvider


def build_provider(index: IndexConfig) -> ConstituentProvider:
    """Instantiate the provider named by an index's config."""
    if index.provider == "csv":
        return CSVConstituentProvider(name=index.name, **index.options)
    if index.provider == "ishares":
        return ISharesProvider(name=index.name, **index.options)
    raise ValueError(f"unknown provider: {index.provider!r}")


__all__ = [
    "Constituents",
    "ConstituentProvider",
    "CSVConstituentProvider",
    "ISharesProvider",
    "build_provider",
]
