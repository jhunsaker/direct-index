"""The provider interface plus shared weight-normalisation helpers."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from ..models import Constituent

# The list of members returned for one index.
Constituents = list[Constituent]


@runtime_checkable
class ConstituentProvider(Protocol):
    """Anything that can produce the current members of a single index.

    Implementations are constructed from an index's config options (see
    :func:`direct_index.indexes.build_provider`) and should raise on network or
    parse failures rather than returning partial data -- a truncated
    constituent list would silently skew the whole rebalance.
    """

    name: str

    def fetch(self) -> Constituents:
        """Return the index's current constituents with normalised weights."""
        ...


def normalize_weights(constituents: Constituents) -> Constituents:
    """Rescale weights so they sum to exactly 1.

    Holdings files routinely sum to 99.7% or 100.2% (rounding, cash sweep,
    excluded line items). We renormalise so downstream blending math is exact.
    Non-positive weights are dropped. Raises if nothing positive remains.
    """
    positive = [c for c in constituents if c.weight > 0]
    total = sum((c.weight for c in positive), Decimal(0))
    if total <= 0:
        raise ValueError("constituent weights sum to zero; nothing to hold")
    return [
        Constituent(
            symbol=c.symbol,
            weight=c.weight / total,
            name=c.name,
            asset_class=c.asset_class,
        )
        for c in positive
    ]
