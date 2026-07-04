from decimal import Decimal

import pytest

from direct_index.config import ConfigError, load_config
from direct_index.indexes.csv_provider import CSVConstituentProvider
from direct_index.indexes.ishares import ISharesProvider

CONFIG = """
[broker]
type = "paper"

[rebalance]
drift_band = 0.01
min_trade_value = 25

[[index]]
name = "sp500"
allocation = 0.6
provider = "csv"
[index.csv]
path = "data/sp500.csv"

[[index]]
name = "intl"
allocation = 0.4
provider = "ishares"
[index.ishares]
holdings_url = "https://example.com/holdings.csv"
"""


def write(tmp_path, text, name="direct-index.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_load_valid_config(tmp_path):
    config = load_config(write(tmp_path, CONFIG))
    assert config.broker.type == "paper"
    assert len(config.indexes) == 2
    assert config.total_allocation == Decimal("1.0")
    assert config.cash_target_fraction == Decimal("0")
    assert config.indexes[0].options["path"] == "data/sp500.csv"


def test_allocations_over_one_rejected(tmp_path):
    bad = CONFIG.replace("allocation = 0.4", "allocation = 0.6")  # sums to 1.2
    with pytest.raises(ConfigError, match="exceeds 1.0"):
        load_config(write(tmp_path, bad))


def test_unknown_provider_rejected(tmp_path):
    bad = CONFIG.replace('provider = "csv"', 'provider = "bloomberg"')
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(write(tmp_path, bad))


def test_csv_provider_normalizes_weights(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text("symbol,weight,name\nAAPL,7,Apple\nMSFT,3,Microsoft\n")
    provider = CSVConstituentProvider(name="t", path=str(csv_path))
    constituents = provider.fetch()
    weights = {c.symbol: c.weight for c in constituents}
    # 7 and 3 -> normalised to 0.7 / 0.3.
    assert weights == {"AAPL": Decimal("0.7"), "MSFT": Decimal("0.3")}


def test_csv_provider_handles_percent_signs(tmp_path):
    csv_path = tmp_path / "holdings.csv"
    csv_path.write_text('symbol,weight\nAAPL,"70%"\nMSFT,"30%"\n')
    provider = CSVConstituentProvider(name="t", path=str(csv_path))
    weights = {c.symbol: c.weight for c in provider.fetch()}
    assert weights == {"AAPL": Decimal("0.7"), "MSFT": Decimal("0.3")}


ISHARES_CSV = '''\
iShares Core S&P 500 ETF
Fund Holdings as of,"Jul 03, 2026"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Shares,Price,Location,Exchange,Currency
AAPL,APPLE INC,Information Technology,Equity,"1,000,000.00",7.10,"5,000","190.50",United States,NASDAQ,USD
MSFT,MICROSOFT CORP,Information Technology,Equity,"950,000.00",6.80,"2,300","410.20",United States,NASDAQ,USD
USD,US DOLLAR,Cash and/or Derivatives,Cash,"5,000.00",0.05,"5,000","1.00",United States,-,USD


The content contained herein is owned by BlackRock and is provided for informational purposes only.
'''


def test_ishares_parser_extracts_equities_only(tmp_path):
    path = tmp_path / "ivv.csv"
    path.write_text(ISHARES_CSV)
    provider = ISharesProvider(name="sp500", path=str(path))
    constituents = provider.fetch()
    symbols = {c.symbol for c in constituents}
    # Cash (USD) row is dropped; only the two equities remain.
    assert symbols == {"AAPL", "MSFT"}
    # Weights renormalised over the two equities (7.10 + 6.80).
    total = sum(c.weight for c in constituents)
    assert total == Decimal("1")
