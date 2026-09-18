from __future__ import annotations

import math
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import data_fetcher
import screener
from data_fetcher import BatchDailyFetchStats, fetch_daily_batch
from indicators import add_all_indicators


ET = ZoneInfo("America/New_York")
FIELDS = ["Open", "High", "Low", "Close", "Volume"]


def daily_frame(offset: float = 0.0, periods: int = 160) -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=periods, freq="B", tz=ET)
    close = pd.Series(range(periods), index=index, dtype=float) + 100.0 + offset
    return pd.DataFrame({
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.7,
        "close": close,
        "volume": 1_000_000.0 + offset,
    }, index=index)


def multiindex_download(symbols: list[str], *, ticker_first: bool = False) -> pd.DataFrame:
    index = pd.date_range("2026-09-08", periods=3, freq="B", tz=ET)
    data: dict[tuple[str, str], list[float]] = {}
    for number, symbol in enumerate(symbols):
        base = 10.0 + number
        values = {
            "Open": [base, base + 1, base + 2],
            "High": [base + 1, base + 2, base + 3],
            "Low": [base - 1, base, base + 1],
            "Close": [base + .5, base + 1.5, base + 2.5],
            "Volume": [100.0, 200.0, 300.0],
        }
        for field, row in values.items():
            data[(symbol, field) if ticker_first else (field, symbol)] = row
    return pd.DataFrame(data, index=index)


class DailyBatchFetchTests(unittest.TestCase):
    def test_503_symbols_make_eleven_sequential_batches(self):
        symbols = [f"X{number}" for number in range(503)]
        calls: list[list[str]] = []

        def fake_download(**kwargs):
            calls.append(kwargs["tickers"])
            return multiindex_download(kwargs["tickers"])

        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", side_effect=fake_download):
            result = fetch_daily_batch(symbols, batch_size=50, stats=metrics)
        self.assertEqual(len(calls), math.ceil(503 / 50))
        self.assertEqual(metrics.batch_count, 11)
        self.assertEqual(metrics.batch_success_count, 503)
        self.assertEqual(metrics.fallback_retry_count, 0)
        self.assertEqual(len(result), 503)

    def test_extracts_field_then_ticker_multiindex(self):
        with patch("yfinance.download", return_value=multiindex_download(["AMD", "MSFT"])):
            result = fetch_daily_batch(["AMD", "MSFT"])
        self.assertEqual(list(result["AMD"].columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(float(result["AMD"].iloc[-1]["close"]), 12.5)
        self.assertEqual(float(result["MSFT"].iloc[-1]["close"]), 13.5)

    def test_extracts_ticker_then_field_multiindex(self):
        with patch("yfinance.download", return_value=multiindex_download(["AMD", "MSFT"], ticker_first=True)):
            result = fetch_daily_batch(["AMD", "MSFT"])
        self.assertEqual(float(result["AMD"].iloc[0]["open"]), 10.0)
        self.assertEqual(float(result["MSFT"].iloc[-1]["volume"]), 300.0)

    def test_only_missing_ticker_uses_single_ticker_fallback(self):
        retry = daily_frame(20)
        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", return_value=multiindex_download(["AMD"])), patch("data_fetcher.fetch_daily", return_value=retry) as fallback:
            result = fetch_daily_batch(["AMD", "MSFT"], stats=metrics)
        fallback.assert_called_once_with("MSFT", period="max")
        self.assertIn("AMD", result)
        self.assertIn("MSFT", result)
        self.assertEqual(metrics.fallback_retry_count, 1)
        self.assertEqual(metrics.fallback_success_count, 1)
        self.assertEqual(metrics.final_failed_count, 0)

    def test_failed_fallback_is_counted_as_final_failure(self):
        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", return_value=pd.DataFrame()), patch("data_fetcher.fetch_daily", return_value=pd.DataFrame()) as fallback:
            result = fetch_daily_batch(["AMD"], stats=metrics)
        fallback.assert_called_once_with("AMD", period="max")
        self.assertEqual(result, {})
        self.assertEqual(metrics.final_failed_count, 1)

    def test_batch_ohlcv_and_indicators_match_single_symbol_fixture(self):
        expected = data_fetcher._daily_regular_session_only(daily_frame())
        downloaded = pd.DataFrame({
            (field.title() if field != "volume" else "Volume", "AMD"): expected[field].to_numpy()
            for field in expected.columns
        }, index=expected.index)
        with patch("yfinance.download", return_value=downloaded):
            actual = fetch_daily_batch(["AMD"])["AMD"]
        pd.testing.assert_frame_equal(actual, expected)
        old_indicators = add_all_indicators(expected)
        new_indicators = add_all_indicators(actual)
        for field in ("DIF", "DEA", "MACD_bar", "N1", "MM1", "AAA", "BBB", "CCC", "JJJ", "DXDX"):
            pd.testing.assert_series_equal(old_indicators[field], new_indicators[field])

    def test_confirmation_uses_one_batch_map_and_never_single_fetch_or_hourly(self):
        raw = daily_frame(periods=180)
        sessions = [pd.Timestamp(raw.index[-1])]
        metrics_seen: list[BatchDailyFetchStats] = []

        def fake_batch(symbols, period, batch_size, *, stats):
            metrics_seen.append(stats)
            stats.universe_count = len(symbols); stats.batch_size = batch_size; stats.batch_count = 1
            stats.batch_success_count = len(symbols)
            return {symbol.upper(): raw for symbol in symbols}

        with patch("screener.fetch_daily_batch", side_effect=fake_batch) as batch_fetch, patch("screener.fetch_daily") as single_fetch, patch("screener.fetch_hourly") as hourly_fetch:
            signals, stats, diagnostics = screener.run_confirmation_screener(["AMD", "MSFT"], sessions, batch_size=50)
        batch_fetch.assert_called_once()
        single_fetch.assert_not_called()
        hourly_fetch.assert_not_called()
        self.assertEqual(stats.fetched_count, 2)
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual(signals, [])
        self.assertEqual(len(metrics_seen), 1)

    def test_confirmation_diagnostic_is_official_daily_indicator_row(self):
        raw = daily_frame(periods=180)
        official = add_all_indicators(raw)
        session = pd.Timestamp(raw.index[-1])
        with patch("screener.fetch_daily_batch", return_value={"AMD": raw}):
            _, _, diagnostics = screener.run_confirmation_screener(["AMD"], [session])
        row = official.iloc[-1]
        item = diagnostics[0]
        self.assertEqual(item.official_daily_source, "yahoo_official_daily")
        self.assertAlmostEqual(item.DIFF, float(row["DIF"]))
        self.assertAlmostEqual(item.DEA, float(row["DEA"]))
        self.assertEqual(item.DXDX, bool(row["DXDX"]))


if __name__ == "__main__":
    unittest.main()
