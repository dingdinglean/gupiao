"""Stock universe loader, scraped from Wikipedia with 24h cache."""
from __future__ import annotations

import logging
import time
from io import StringIO
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

EXTRA_SEMIS = [
    "AMKR", "RMBS", "ONTO", "LSCC", "SMTC", "MTSI",
    "TER", "SWKS", "QRVO", "MPWR", "NXPI", "MRVL",
    "ASML", "TSM", "ARM", "ALAB",
]

# Fallback list used when GitHub Actions cannot reach Wikipedia.
FALLBACK = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "TSLA", "AVGO", "COST",
    "NFLX", "AMD", "ADBE", "CRM", "ORCL", "INTC", "CSCO", "QCOM", "TXN", "AMAT",
    "MU", "LRCX", "KLAC", "PANW", "CRWD", "FTNT", "NOW", "SHOP", "UBER", "ABNB",
    "PLTR", "COIN", "SMCI", "ANET", "DDOG", "MDB", "NET", "SNOW", "OKTA", "ROKU",
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "COF", "SCHW",
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "ISRG", "REGN",
    "WMT", "HD", "MCD", "NKE", "SBUX", "TJX", "COST", "PG", "KO", "PEP",
    "XOM", "CVX", "COP", "SLB", "EOG", "CAT", "DE", "HON", "GE", "BA",
    "RTX", "LMT", "DAL", "UAL", "AAL", "MAR", "HLT", "MGM", "CCL", "RCL",
    "DIS", "CMCSA", "VZ", "T", "TMUS", "IBM", "ACN", "INTU", "ADP", "PYPL",
] + EXTRA_SEMIS


def _fetch_url(url: str) -> str:
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def _load_or_fetch(name: str, fetcher) -> list[str]:
    cache = CACHE_DIR / f"{name}.csv"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL_SECONDS:
        return pd.read_csv(cache)["ticker"].astype(str).tolist()
    try:
        tickers = fetcher()
        pd.DataFrame({"ticker": tickers}).to_csv(cache, index=False)
        log.info(f"Cached {len(tickers)} tickers as '{name}'")
        return tickers
    except Exception as e:
        log.warning(f"Fetch '{name}' failed ({e}); using cache or fallback list")
        if cache.exists():
            return pd.read_csv(cache)["ticker"].astype(str).tolist()
        return FALLBACK


def _normalize_tickers(tickers: pd.Series) -> list[str]:
    return tickers.astype(str).str.strip().str.replace(".", "-", regex=False).tolist()


def _fetch_sp500() -> list[str]:
    html = _fetch_url("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    tables = pd.read_html(StringIO(html))
    # Yahoo uses '-' instead of '.' in tickers (e.g. BRK.B -> BRK-B)
    return _normalize_tickers(tables[0]["Symbol"])


def _fetch_ndx() -> list[str]:
    html = _fetch_url("https://en.wikipedia.org/wiki/Nasdaq-100")
    tables = pd.read_html(StringIO(html))
    for t in tables:
        for col in t.columns:
            if str(col).lower() in ("ticker", "symbol"):
                return _normalize_tickers(t[col])
    raise RuntimeError("Could not find ticker column on NASDAQ-100 Wikipedia page")


def _fetch_sp400() -> list[str]:
    html = _fetch_url("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies")
    tables = pd.read_html(StringIO(html))
    for t in tables:
        for col in t.columns:
            col_name = str(col).strip().lower()
            if col_name in ("symbol", "ticker", "ticker symbol"):
                return _normalize_tickers(t[col])
    raise RuntimeError("Could not find ticker column on S&P MidCap 400 Wikipedia page")


def get_sp500() -> list[str]:
    return _load_or_fetch("sp500", _fetch_sp500)


def get_nasdaq100() -> list[str]:
    return _load_or_fetch("ndx", _fetch_ndx)


def get_sp400() -> list[str]:
    return _load_or_fetch("sp400", _fetch_sp400)


def get_universe() -> list[str]:
    """S&P 500 union S&P MidCap 400 union NASDAQ 100 union extra semiconductor watchlist."""
    return sorted(
        set(get_sp500())
        | set(get_sp400())
        | set(get_nasdaq100())
        | set(EXTRA_SEMIS)
    )
