"""Independent S&P 500 + Nasdaq-100 universe for the DXDX pullback radar."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
CACHE_DIR = Path(__file__).parent / "data" / "constituents"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Embedded fetched_at, not file mtime: git checkout refreshes mtime, which would
# either refetch Wikipedia on every CI run or never refresh a committed cache.
REFRESH_INTERVAL = timedelta(days=7)
UA = "Mozilla/5.0 (compatible; USPullbackRadar/4.2)"
US_TICKER = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")
NON_STOCK_SYMBOLS = {
    "SPY", "QQQ", "DIA", "IWM", "VOO", "VTI",
    "XLK", "SOXX", "SMH", "XLC", "XLF", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE",
    "GLD", "SLV", "USO", "IBIT", "BITO",
}
# Live Wikipedia layouts. The Nasdaq-100 article dropped its component table;
# the dedicated list page is the current source and the article remains as a fallback.
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_URLS = (
    "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
    "https://en.wikipedia.org/wiki/Nasdaq-100",
)
PLAUSIBLE_COUNTS = {
    "sp500": (450, 550),
    "nasdaq100": (90, 115),
}

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


@dataclass(frozen=True)
class ConstituentLoad:
    name: str
    symbols: list[str]
    origin: str
    detail: str = ""


_LAST_LOADS: list[ConstituentLoad] = []


def incomplete_universe_reasons() -> list[str]:
    """Reasons the latest get_universe() call is not a full index universe."""
    reasons = []
    for load in _LAST_LOADS:
        if load.origin == "emergency-fallback":
            reasons.append(f"{load.name} 成分股使用紧急子集（{len(load.symbols)} 只）：{load.detail}")
    return reasons


def is_us_listed_stock(symbol: str) -> bool:
    """Reject foreign suffixes, OTC, ETF/fund/index/crypto-style codes.

    The source-level guarantee is S&P 500/Nasdaq-100 membership. This guard
    prevents a bad cache/fallback from injecting 0700.HK, 600519.SS, BTC-USD,
    or a similarly non-US symbol into the scan.
    """
    normalized = (symbol or "").strip().upper()
    return bool(US_TICKER.fullmatch(normalized)) and normalized not in NON_STOCK_SYMBOLS


def _fetch_url(url: str) -> str:
    from urllib.request import Request, urlopen

    request = Request(url, headers={"User-Agent": UA})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _normalize_tickers(tickers: pd.Series) -> list[str]:
    return [ticker for ticker in tickers.astype(str).str.strip().str.upper().str.replace(".", "-", regex=False) if is_us_listed_stock(ticker)]


def _is_plausible(name: str, symbols: list[str]) -> bool:
    low, high = PLAUSIBLE_COUNTS[name]
    return low <= len(symbols) <= high and all(is_us_listed_stock(symbol) for symbol in symbols)


def _fetch_sp500() -> list[str]:
    tables = pd.read_html(StringIO(_fetch_url(SP500_URL)))
    return _normalize_tickers(tables[0]["Symbol"])


def _tickers_from_tables(tables) -> list[str] | None:
    for table in tables:
        for column in table.columns:
            if str(column).strip().lower() in {"ticker", "symbol"}:
                symbols = _normalize_tickers(table[column])
                if symbols:
                    return symbols
    return None


def _fetch_nasdaq100() -> list[str]:
    errors: list[str] = []
    for url in NASDAQ100_URLS:
        try:
            tables = pd.read_html(StringIO(_fetch_url(url)))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        symbols = _tickers_from_tables(tables)
        if symbols:
            return symbols
        errors.append(f"{url}: Could not find Nasdaq-100 ticker column")
    raise RuntimeError("; ".join(errors) or "Could not find Nasdaq-100 ticker column")


def _parse_fetched_at(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text or text.lower() == "nan":
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_cache(name: str) -> tuple[list[str], datetime | None] | None:
    cache = CACHE_DIR / f"{name}.csv"
    if not cache.exists():
        return None
    cached = pd.read_csv(cache)
    if not {"ticker", "source"}.issubset(cached.columns) or set(cached["source"].astype(str)) != {name}:
        log.warning("Discarding untrusted %s cache without index provenance", name)
        return None
    symbols = _normalize_tickers(cached["ticker"])
    if not _is_plausible(name, symbols):
        log.warning("Discarding implausible %s cache count=%s", name, len(symbols))
        return None
    fetched_at = None
    if "fetched_at" in cached.columns and cached["fetched_at"].notna().any():
        fetched_at = _parse_fetched_at(str(cached["fetched_at"].dropna().astype(str).min()))
    return symbols, fetched_at


def _write_cache(name: str, symbols: list[str], fetched_at: datetime) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({
        "ticker": list(symbols),
        "source": name,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
    })
    target = CACHE_DIR / f"{name}.csv"
    temporary = target.with_suffix(".csv.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(target)


def _remember(loads: list[ConstituentLoad] | None, load: ConstituentLoad) -> None:
    if loads is not None:
        loads.append(load)


def _load_or_fetch(name: str, fetcher, loads: list[ConstituentLoad] | None = None, *, now: datetime | None = None) -> list[str]:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cached = _read_cache(name)
    if cached is not None:
        symbols, fetched_at = cached
        if fetched_at is not None and moment - fetched_at < REFRESH_INTERVAL:
            log.info("%s constituents from fresh cache count=%s fetched_at=%s", name, len(symbols), fetched_at.isoformat())
            _remember(loads, ConstituentLoad(name, symbols, "fresh-cache"))
            return symbols
    fetch_error: Exception | None = None
    fresh: list[str] | None = None
    try:
        fresh = fetcher()
    except Exception as exc:
        fetch_error = exc
        log.warning("%s universe source unavailable: %s", name, exc)
    if fresh is not None and _is_plausible(name, fresh):
        _write_cache(name, fresh, moment)
        log.info("%s constituents refreshed count=%s", name, len(fresh))
        _remember(loads, ConstituentLoad(name, fresh, "refresh"))
        return fresh
    if fresh is not None:
        detail = f"implausible count={len(fresh)}"
        log.warning("%s universe fetch implausible count=%s; cache left unchanged", name, len(fresh))
    else:
        detail = f"fetch failed: {fetch_error}"
    if cached is not None:
        symbols, _fetched_at = cached
        log.warning("%s using cached constituents count=%s after %s", name, len(symbols), detail)
        _remember(loads, ConstituentLoad(name, symbols, "stale-cache", detail))
        return symbols
    fallback = [symbol for symbol in FALLBACK_US_INDEX_STOCKS if is_us_listed_stock(symbol)]
    log.error("%s using emergency fallback count=%s after %s", name, len(fallback), detail)
    _remember(loads, ConstituentLoad(name, fallback, "emergency-fallback", detail))
    return list(fallback)


def get_sp500() -> list[str]:
    return _load_or_fetch("sp500", _fetch_sp500)


def get_nasdaq100() -> list[str]:
    return _load_or_fetch("nasdaq100", _fetch_nasdaq100)


def combine_us_universe(sp500: list[str], nasdaq100: list[str]) -> list[str]:
    """Deduplicate only the two US-index sources; no watchlists are merged."""
    return sorted({symbol.upper() for symbol in [*sp500, *nasdaq100] if is_us_listed_stock(symbol)})


def get_universe() -> list[str]:
    loads: list[ConstituentLoad] = []
    combined = combine_us_universe(
        _load_or_fetch("sp500", _fetch_sp500, loads),
        _load_or_fetch("nasdaq100", _fetch_nasdaq100, loads),
    )
    global _LAST_LOADS
    _LAST_LOADS = loads
    return combined
