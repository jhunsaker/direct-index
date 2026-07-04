"""Local-CSV constituent provider -- the simplest, fully-offline source.

You maintain a CSV per index with at least a symbol column and a weight column.
Weights may be fractions (0.07) or percentages (7 or "7%"); they are normalised
to sum to 1 regardless. This provider is also what the test suite and the
sample config use, so the whole system is exercisable without a network.

Example CSV::

    symbol,weight,name
    AAPL,7.1,Apple Inc.
    MSFT,6.8,Microsoft Corp.
    NVDA,6.2,NVIDIA Corp.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..models import Constituent, dec
from .base import Constituents, normalize_weights


class CSVConstituentProvider:
    def __init__(
        self,
        *,
        name: str,
        path: str,
        symbol_column: str = "symbol",
        weight_column: str = "weight",
        name_column: str = "name",
    ) -> None:
        self.name = name
        self.path = Path(path)
        self.symbol_column = symbol_column
        self.weight_column = weight_column
        self.name_column = name_column

    def fetch(self) -> Constituents:
        if not self.path.exists():
            raise FileNotFoundError(
                f"index {self.name!r}: holdings CSV not found at {self.path}"
            )
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            self._require_columns(reader.fieldnames)
            constituents = [self._parse_row(row) for row in reader]
        constituents = [c for c in constituents if c is not None]
        if not constituents:
            raise ValueError(f"index {self.name!r}: no rows found in {self.path}")
        return normalize_weights(constituents)

    def _require_columns(self, fieldnames: list[str] | None) -> None:
        have = set(fieldnames or [])
        missing = {self.symbol_column, self.weight_column} - have
        if missing:
            raise ValueError(
                f"index {self.name!r}: {self.path} is missing column(s) "
                f"{sorted(missing)}; found {sorted(have)}"
            )

    def _parse_row(self, row: dict) -> Constituent | None:
        symbol = (row.get(self.symbol_column) or "").strip().upper()
        if not symbol:
            return None  # skip blank/spacer rows
        try:
            weight = _parse_weight(row.get(self.weight_column))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"index {self.name!r}: bad weight for {symbol!r}: {exc}"
            ) from exc
        return Constituent(
            symbol=symbol,
            weight=weight,
            name=(row.get(self.name_column) or "").strip(),
        )


def _parse_weight(raw: object) -> Decimal:
    if raw is None or str(raw).strip() == "":
        return Decimal(0)
    return dec(str(raw).replace("%", "").replace(",", "").strip())
