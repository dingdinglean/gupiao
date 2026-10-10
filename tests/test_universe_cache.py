"""Committed constituent cache: refresh, plausibility, and fallback."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import universe


def plausible_symbols(name: str, extra: int = 0) -> list[str]:
    count = universe.PLAUSIBLE_COUNTS[name][0] + extra
    symbols: list[str] = []
    number = 0
    while len(symbols) < count:
        chars = []
        value = number
        for _ in range(4):
            chars.append(chr(ord("A") + (value % 26)))
            value //= 26
        symbol = "".join(reversed(chars))
        if universe.is_us_listed_stock(symbol):
            symbols.append(symbol)
        number += 1
    return symbols


def write_cache(directory: Path, name: str, symbols: list[str], fetched_at: datetime) -> None:
    pd.DataFrame({
        "ticker": symbols,
        "source": name,
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat(),
    }).to_csv(directory / f"{name}.csv", index=False)


class CommittedConstituentCacheTests(unittest.TestCase):
    def test_committed_lists_are_plausible_us_stocks(self):
        for name in ("sp500", "nasdaq100"):
            path = universe.CACHE_DIR / f"{name}.csv"
            self.assertTrue(path.is_file(), path)
            frame = pd.read_csv(path)
            self.assertTrue({"ticker", "source", "fetched_at"}.issubset(frame.columns))
            self.assertEqual(set(frame["source"].astype(str)), {name})
            symbols = universe._normalize_tickers(frame["ticker"])
            self.assertTrue(universe._is_plausible(name, symbols))
            self.assertIsNotNone(universe._parse_fetched_at(str(frame["fetched_at"].iloc[0])))


class UniverseCacheTests(unittest.TestCase):
    def setUp(self):
        universe._LAST_LOADS = []

    def test_fresh_cache_skips_wikipedia(self):
        symbols = plausible_symbols("sp500")
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            write_cache(Path(directory), "sp500", symbols, datetime.now(timezone.utc) - timedelta(days=1))

            def fail_if_called():
                raise AssertionError("fresh cache must not fetch")

            loaded = universe._load_or_fetch("sp500", fail_if_called)
        self.assertEqual(loaded, symbols)

    def test_stale_cache_refreshes_and_records_new_timestamp(self):
        cached = plausible_symbols("nasdaq100")
        refreshed = plausible_symbols("nasdaq100", extra=2)
        moment = datetime(2026, 10, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            write_cache(Path(directory), "nasdaq100", cached, moment - timedelta(days=8))
            loaded = universe._load_or_fetch("nasdaq100", lambda: refreshed, now=moment)
            stored = pd.read_csv(Path(directory) / "nasdaq100.csv")
        self.assertEqual(loaded, refreshed)
        self.assertEqual(stored["fetched_at"].nunique(), 1)
        self.assertEqual(universe._parse_fetched_at(str(stored["fetched_at"].iloc[0])), moment)

    def test_fetch_failure_falls_back_to_stale_cache_without_rewriting_it(self):
        symbols = plausible_symbols("sp500")
        moment = datetime(2026, 10, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            path = Path(directory)
            write_cache(path, "sp500", symbols, moment - timedelta(days=8))
            before = (path / "sp500.csv").read_text(encoding="utf-8")

            def fail():
                raise TimeoutError("wikipedia down")

            loaded = universe._load_or_fetch("sp500", fail, now=moment)
            after = (path / "sp500.csv").read_text(encoding="utf-8")
        self.assertEqual(loaded, symbols)
        self.assertEqual(before, after)

    def test_implausible_fetch_keeps_cache(self):
        symbols = plausible_symbols("sp500")
        moment = datetime(2026, 10, 10, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            path = Path(directory)
            write_cache(path, "sp500", symbols, moment - timedelta(days=8))
            before = (path / "sp500.csv").read_text(encoding="utf-8")
            loaded = universe._load_or_fetch("sp500", lambda: ["AAPL", "MSFT"], now=moment)
            after = (path / "sp500.csv").read_text(encoding="utf-8")
        self.assertEqual(loaded, symbols)
        self.assertEqual(before, after)

    def test_missing_cache_and_bad_fetch_uses_emergency_subset(self):
        loads: list[universe.ConstituentLoad] = []
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            loaded = universe._load_or_fetch("sp500", lambda: ["AAPL"], loads)
        self.assertEqual(loaded, list(universe.FALLBACK_US_INDEX_STOCKS))
        self.assertEqual(loads[0].origin, "emergency-fallback")
        self.assertLess(len(loaded), universe.PLAUSIBLE_COUNTS["sp500"][0])

    def test_get_universe_records_emergency_fallback_for_exit_signal(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)), patch.object(universe, "_fetch_sp500", return_value=["AAPL"]), patch.object(universe, "_fetch_nasdaq100", return_value=["MSFT"]):
            combined = universe.get_universe()
        reasons = universe.incomplete_universe_reasons()
        self.assertEqual(combined, sorted(set(universe.FALLBACK_US_INDEX_STOCKS)))
        self.assertEqual(len(reasons), 2)
        self.assertTrue(all("紧急子集" in reason for reason in reasons))

    def test_nasdaq_fetcher_reads_component_list_before_the_index_article(self):
        symbols = plausible_symbols("nasdaq100")
        rows = "".join(f"<tr><td>{symbol}</td><td>Company</td></tr>" for symbol in symbols)
        html = f"<table><thead><tr><th>Ticker</th><th>Company</th></tr></thead><tbody>{rows}</tbody></table>"
        calls: list[str] = []

        def fake_fetch(url: str) -> str:
            calls.append(url)
            if url == universe.NASDAQ100_URLS[0]:
                return html
            raise AssertionError(url)

        with patch.object(universe, "_fetch_url", side_effect=fake_fetch):
            loaded = universe._fetch_nasdaq100()
        self.assertEqual(loaded, symbols)
        self.assertEqual(calls, [universe.NASDAQ100_URLS[0]])


if __name__ == "__main__":
    unittest.main()
