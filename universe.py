"""Stock universe loader: S&P 500 + NASDAQ 100, scraped from Wikipedia with 24h cache."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Fallback list of popular US tickers, used if Wikipedia is unreachable
FALLBACK = [
    "AAPL","MSFT","GOOGL","GOOG","AMZN","NVDA","META","TSLA","BRK-B","JPM",
    "V","JNJ","WMT","PG","MA","UNH","HD","DIS","BAC","ADBE","CRM","NFLX",
    "XOM","KO","PEP","PFE","TMO","ABT","CVX","ABBV","MRK","COST","AVGO",
    "MCD","ACN","WFC","DHR","VZ","NKE","LLY","TXN","QCOM","PM","INTC","AMD",
    "CAT","INTU","AMGN","GS","BA","ORCL","T","IBM","GE","AXP","C","MS","BLK",
    "SPGI","BKNG","PYPL","SBUX","GILD","MDLZ","ADP","TJX","ISRG","VRTX","REGN",
    "AMAT","MMM","F","GM","CSCO","HON","RTX","LMT","NOW","PLTR","SHOP","UBER",
    "ABNB","SNOW","COIN","ROKU","DDOG","MDB","OKTA","NET","CRWD","FTNT","PANW",
    "DELL","HPQ","AAL","DAL","UAL","LUV","MAR","HLT","MGM","WYNN","LVS","CCL",
    "RCL","NCLH","BX","KKR","APO","SCHW","ICE","CME","COF","USB","PNC","TFC",
    "MU","LRCX","KLAC","ADI","MRVL","ON","ASML","TSM","SMCI","ANET","CDNS","SNPS"
]


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
        log.warning(f"Fetch '{name}' failed ({e}); using fallback list")
        if cache.exists():
            return pd.read_csv(cache)["ticker"].astype(str).tolist()
        return FALLBACK


def _fetch_sp500() -> list[str]:
    html = _fetch_url("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
    tables = pd.read_html(html)
    return tables[0]["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()


def _fetch_ndx() -> list[str]:
    html = _fetch_url("https://en.wikipedia.org/wiki/Nasdaq-100")
    tables = pd.read_html(html)
    for t in tables:
        for col in t.columns:
            if str(col).lower() in ("ticker", "symbol"):
                return t[col].astype(str).str.replace(".", "-", regex=False).tolist()
    raise RuntimeError("Could not find ticker column on NASDAQ-100 page")


def get_sp500() -> list[str]:
    return _load_or_fetch("sp500", _fetch_sp500)


def get_nasdaq100() -> list[str]:
    return _load_or_fetch("ndx", _fetch_ndx)


def get_universe() -> list[str]:
    return sorted(set(get_sp500()) | set(get_nasdaq100()))
