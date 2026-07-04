"""iShares (BlackRock) ETF-holdings provider.

iShares publishes a daily holdings CSV for each ETF at a stable ``.ajax`` URL,
which we use as a free, realistic proxy for the underlying index. Point an
index at the fund's CSV URL (copy it from the fund page's "Detailed Holdings
and Analytics -> Download CSV" link)::

    [[index]]
    name = "sp500"
    allocation = 0.6
    provider = "ishares"
    [index.ishares]
    holdings_url = "https://www.ishares.com/us/products/239726/.../fund.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
    cache_path = "data/ivv_holdings.csv"   # optional: save each download

You can also skip the network entirely and parse a file you downloaded by hand::

    [index.ishares]
    path = "data/ivv_holdings.csv"

Only the HTTP fetch relies on the network (stdlib ``urllib``, no third-party
dependency). The CSV parser is what carries the iShares-specific knowledge:
their files carry a multi-line preamble before a header row containing
"Ticker", and a trailing disclaimer section after a blank line. The exact
column set drifts over time, so :meth:`ISharesProvider.parse` is written
defensively and is the piece most worth re-checking against a live download.
"""

from __future__ import annotations

import csv
import io
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..models import Constituent, dec
from .base import Constituents, normalize_weights

# iShares rejects requests without a browser-ish User-Agent.
_HEADERS = {"User-Agent": "Mozilla/5.0 (direct-index constituent fetch)"}
_TIMEOUT = 30

# Column-name candidates, matched case-insensitively against the header row.
_TICKER_KEYS = ("ticker",)
_WEIGHT_KEYS = ("weight (%)", "weight(%)", "weight")
_NAME_KEYS = ("name",)
_ASSET_KEYS = ("asset class",)

# Asset classes we actually hold as equities; everything else (cash, futures,
# FX forwards, money-market sweeps) is dropped from the target.
_HELD_ASSET_CLASSES = {"equity"}


class ISharesProvider:
    def __init__(
        self,
        *,
        name: str,
        holdings_url: str = "",
        path: str = "",
        cache_path: str = "",
    ) -> None:
        if not holdings_url and not path:
            raise ValueError(
                f"index {name!r}: iShares provider needs holdings_url or path"
            )
        self.name = name
        self.holdings_url = holdings_url
        self.path = Path(path) if path else None
        self.cache_path = Path(cache_path) if cache_path else None

    def fetch(self) -> Constituents:
        text = self._load_text()
        constituents = self.parse(text)
        if not constituents:
            raise ValueError(
                f"index {self.name!r}: parsed zero equity holdings from iShares CSV"
            )
        return normalize_weights(constituents)

    def _load_text(self) -> str:
        if self.path is not None:
            if not self.path.exists():
                raise FileNotFoundError(
                    f"index {self.name!r}: holdings file not found at {self.path}"
                )
            return self.path.read_text(encoding="utf-8-sig")
        req = urllib.request.Request(self.holdings_url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310
            text = resp.read().decode("utf-8-sig", errors="replace")
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(text, encoding="utf-8")
        return text

    def parse(self, text: str) -> Constituents:
        """Parse an iShares holdings CSV into constituents.

        Finds the header row by looking for a "Ticker" column, then reads
        equity rows until the data runs out (blank/short rows mark the start of
        the disclaimer block).
        """
        lines = text.splitlines()
        header_idx = _find_header(lines)
        if header_idx is None:
            raise ValueError(
                f"index {self.name!r}: no 'Ticker' header row in iShares CSV"
            )

        reader = csv.reader(io.StringIO("\n".join(lines[header_idx:])))
        header = next(reader)
        cols = _column_index(header)

        out: Constituents = []
        for row in reader:
            if not _looks_like_data(row, cols):
                break  # reached the trailing disclaimer section
            constituent = _row_to_constituent(row, cols)
            if constituent is not None:
                out.append(constituent)
        return out


def _find_header(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        cells = [c.strip().strip('"').lower() for c in line.split(",")]
        if any(cell in _TICKER_KEYS for cell in cells):
            return i
    return None


def _column_index(header: list[str]) -> dict[str, int]:
    lower = [h.strip().lower() for h in header]

    def find(keys: tuple[str, ...]) -> int | None:
        for key in keys:
            if key in lower:
                return lower.index(key)
        return None

    cols = {
        "ticker": find(_TICKER_KEYS),
        "weight": find(_WEIGHT_KEYS),
        "name": find(_NAME_KEYS),
        "asset": find(_ASSET_KEYS),
    }
    if cols["ticker"] is None or cols["weight"] is None:
        raise ValueError("iShares CSV header lacks a Ticker and/or Weight column")
    return cols


def _looks_like_data(row: list[str], cols: dict[str, int]) -> bool:
    idx = cols["ticker"]
    return len(row) > idx and bool(row[idx].strip())


def _row_to_constituent(row: list[str], cols: dict[str, int]) -> Constituent | None:
    ticker = row[cols["ticker"]].strip().upper()
    # Drop cash lines and anything without a normal alphabetic ticker.
    if not ticker or ticker in {"-", "CASH", "USD"} or not ticker.isalnum():
        return None

    asset_idx = cols["asset"]
    if asset_idx is not None and asset_idx < len(row):
        asset = row[asset_idx].strip().lower()
        if asset and asset not in _HELD_ASSET_CLASSES:
            return None

    try:
        weight = _parse_weight(row[cols["weight"]])
    except (InvalidOperation, ValueError):
        return None
    if weight <= 0:
        return None

    name = ""
    name_idx = cols["name"]
    if name_idx is not None and name_idx < len(row):
        name = row[name_idx].strip()

    return Constituent(symbol=ticker, weight=weight, name=name)


def _parse_weight(raw: str) -> Decimal:
    return dec(raw.replace("%", "").replace(",", "").strip())
