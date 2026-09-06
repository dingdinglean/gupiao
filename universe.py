"""Independent S&P 500 + Nasdaq-100 universe for the DXDX pullback radar."""
from __future__ import annotations

import logging
import re
import time
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

log = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600
UA = "Mozilla/5.0 (compatible; USPullbackRadar/4.2)"
US_TICKER = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")
NON_STOCK_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "XLK", "XLE", "XLV", "XLF", "GLD", "SLV", "USO", "IBIT", "BITO"}

# Audited emergency fallback: US-listed common stocks only, drawn from the
# intended index universe. It is not a watchlist and is never expanded.
FALLBACK_US_INDEX_STOCKS = (
    "AAPL", "ABBV", "ABT", "ADBE", "AMD", "AMGN", "AMZN", "AVGO", "BAC", "BA",
    "CAT", "CMCSA", "COST", "CRM", "CSCO", "CVX", "DE", "DIS", "EOG", "GE",
    "GOOG", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "ISRG", "JNJ", "JPM",
    "KO", "LLY", "LMT", "MA", "MCD", "META", "MRK", "MSFT", "NFLX", "NVDA",
    "ORCL", "PANW", "PEP", "PFE", "PG", "QCOM", "RTX", "SBUX", "TMO", "TSLA",
    "TXN", "UNH", "V", "WFC", "WMT", "XOM",
)


def is_us_listed_stock(symbol: str) -> bool:
    """Reject foreign suffixes, OTC, ETF/fund/index/crypto-style codes.

    The source-level guarantee is S&P 500/Nasdaq-100 membership. This guard
    prevents a bad cache/fallback from injecting 0700.HK, 600519.SS, BTC-USD,
    or a similarly non-US symbol into the scan.
    """
    normalized = (symbol or "").strip().upper()
    return bool(US_TICKER.fullmatch(normalized)) and normalized not in NON_STOCK_SYMBOLS


def _fetch_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _normalize_tickers(tickers: pd.Series) -> list[str]:
    return [ticker for ticker in tickers.astype(str).str.strip().str.upper().str.replace(".", "-", regex=False) if is_us_listed_stock(ticker)]


def _fetch_sp500() -> list[str]:
    tables = pd.read_html(StringIO(_fetch_url("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")))
    return _normalize_tickers(tables[0]["Symbol"])


def _fetch_nasdaq100() -> list[str]:
    tables = pd.read_html(StringIO(_fetch_url("https://en.wikipedia.org/wiki/Nasdaq-100")))
    for table in tables:
        for column in table.columns:
            if str(column).strip().lower() in {"ticker", "symbol"}:
                return _normalize_tickers(table[column])
    raise RuntimeError("Could not find Nasdaq-100 ticker column")


def _load_or_fetch(name: str, fetcher) -> list[str]:
    cache = CACHE_DIR / f"{name}.csv"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_SECONDS:
        cached = pd.read_csv(cache)
        # Legacy/unknown caches have no provenance and must not be trusted to
        # inject an ETF, OTC ticker, or a foreign-market code.
        if {"ticker", "source"}.issubset(cached.columns) and set(cached["source"].astype(str)) == {name}:
            return _normalize_tickers(cached["ticker"])
        log.warning("Discarding untrusted %s cache without index provenance", name)
    try:
        symbols = fetcher()
        pd.DataFrame({"ticker": symbols, "source": name}).to_csv(cache, index=False)
        return symbols
    except Exception as exc:
        log.warning("%s universe source unavailable: %s", name, exc)
        if cache.exists():
            cached = pd.read_csv(cache)
            if {"ticker", "source"}.issubset(cached.columns) and set(cached["source"].astype(str)) == {name}:
                return _normalize_tickers(cached["ticker"])
        return list(FALLBACK_US_INDEX_STOCKS)


def get_sp500() -> list[str]:
    return _load_or_fetch("sp500", _fetch_sp500)


def get_nasdaq100() -> list[str]:
    return _load_or_fetch("nasdaq100", _fetch_nasdaq100)


def combine_us_universe(sp500: list[str], nasdaq100: list[str]) -> list[str]:
    """Deduplicate only the two US-index sources; no watchlists are merged."""
    return sorted({symbol.upper() for symbol in [*sp500, *nasdaq100] if is_us_listed_stock(symbol)})


def get_universe() -> list[str]:
    return combine_us_universe(get_sp500(), get_nasdaq100())
