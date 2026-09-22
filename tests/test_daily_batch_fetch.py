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
    def setUp(self):
        self.runtime = patch("data_fetcher.configure_yfinance_runtime", return_value="/tmp/gupiao-yf-test")
        self.warmup = patch("data_fetcher.warm_yfinance_cache", return_value=(True, 0.01))
        self.runtime.start()
        self.warmup.start()

    def tearDown(self):
        self.runtime.stop()
        self.warmup.stop()
        data_fetcher._YF_RUNTIME_READY = False
        data_fetcher._YF_CACHE_WARMED = False
        data_fetcher._YF_CACHE_DIR = None

    def test_503_symbols_make_eleven_sequential_batches(self):
        symbols = [f"X{number}" for number in range(503)]
        calls: list[dict] = []

        def fake_download(**kwargs):
            calls.append(kwargs)
            return multiindex_download(kwargs["tickers"])

        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", side_effect=fake_download):
            result = fetch_daily_batch(symbols, batch_size=50, stats=metrics)
        self.assertEqual(len(calls), math.ceil(503 / 50))
        self.assertEqual(metrics.batch_count, 11)
        self.assertEqual(metrics.batch_success_count, 503)
        self.assertEqual(metrics.fallback_retry_count, 0)
        self.assertEqual(len(result), 503)
        self.assertTrue(all(call["threads"] == 8 and call["timeout"] == 12 for call in calls))

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

    def test_single_fallback_cap_limits_requests_and_counts_untried_symbols(self):
        symbols = ["AMD", "MSFT", "NVDA", "META", "INTC"]
        metrics = BatchDailyFetchStats()
        with self.assertLogs("data_fetcher", level="WARNING") as logs, patch(
            "yfinance.download", return_value=pd.DataFrame()
        ), patch("data_fetcher.fetch_daily", return_value=pd.DataFrame()) as fallback:
            result = fetch_daily_batch(
                symbols,
                stats=metrics,
                max_single_retries=2,
            )
        self.assertEqual(result, {})
        self.assertEqual(fallback.call_count, 2)
        self.assertEqual(metrics.single_retry_count, 2)
        self.assertEqual(metrics.final_failed_count, 5)
        self.assertTrue(any("single retry cap reached" in message for message in logs.output))

    def test_none_single_fallback_cap_preserves_existing_behavior(self):
        symbols = ["AMD", "MSFT", "NVDA"]
        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", return_value=pd.DataFrame()), patch(
            "data_fetcher.fetch_daily", return_value=pd.DataFrame()
        ) as fallback:
            fetch_daily_batch(symbols, stats=metrics, max_single_retries=None)
        self.assertEqual(fallback.call_count, len(symbols))
        self.assertEqual(metrics.final_failed_count, len(symbols))

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

    def test_runtime_configures_isolated_cache_once_before_requests(self):
        self.runtime.stop(); self.warmup.stop()
        data_fetcher._YF_RUNTIME_READY = False
        data_fetcher._YF_CACHE_DIR = None
        with patch("data_fetcher.tempfile.mkdtemp", return_value="/tmp/gupiao-yf-runtime") as make_dir, patch("yfinance.set_tz_cache_location") as set_cache:
            first = data_fetcher.configure_yfinance_runtime()
            second = data_fetcher.configure_yfinance_runtime()
        self.assertEqual(first, "/tmp/gupiao-yf-runtime")
        self.assertEqual(second, first)
        make_dir.assert_called_once_with(prefix="gupiao-yfinance-")
        set_cache.assert_called_once_with("/tmp/gupiao-yf-runtime")

    def test_cache_warmup_is_single_threaded_and_nonfatal(self):
        self.warmup.stop()
        data_fetcher._YF_CACHE_WARMED = False
        with patch("data_fetcher.configure_yfinance_runtime", return_value="/tmp/gupiao-yf-runtime"), patch("yfinance.download", return_value=pd.DataFrame()) as download:
            success, _ = data_fetcher.warm_yfinance_cache()
        self.assertTrue(success)
        self.assertFalse(download.call_args.kwargs["threads"])
        self.assertEqual(download.call_args.kwargs["timeout"], 10)

    def test_standard_midnight_daily_fast_path_skips_xnys_lookup(self):
        index = pd.date_range("2000-01-03", periods=5000, freq="B", tz=ET)
        source = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}, index=index)
        with patch("data_fetcher.nyse_session") as lookup:
            result = data_fetcher._normalize_official_daily(source)
        self.assertEqual(len(result), 5000)
        lookup.assert_not_called()

    def test_mixed_intraday_daily_still_drops_extended_row(self):
        index = pd.DatetimeIndex([
            pd.Timestamp("2026-09-08 00:00", tz=ET),
            pd.Timestamp("2026-09-08 20:00", tz=ET),
        ])
        source = pd.DataFrame({"open": [10.0, 1.0], "high": [11.0, 999.0], "low": [9.0, 0.01], "close": [10.5, 1.0], "volume": [100.0, 999.0]}, index=index)
        result = data_fetcher._normalize_official_daily(source)
        self.assertEqual(len(result), 1)
        self.assertEqual(float(result.iloc[0]["close"]), 10.5)

    def test_mini_batch_retry_recovers_missing_symbol_without_single_fallback(self):
        metrics = BatchDailyFetchStats()
        with patch("yfinance.download", side_effect=[multiindex_download(["AMD"]), multiindex_download(["MSFT"])]) as download, patch("data_fetcher.fetch_daily") as single:
            result = fetch_daily_batch(["AMD", "MSFT"], stats=metrics)
        self.assertEqual(len(download.call_args_list), 2)
        self.assertEqual(metrics.mini_batch_retry_count, 1)
        self.assertEqual(metrics.mini_batch_retry_symbols, 1)
        self.assertEqual(metrics.mini_batch_success_count, 1)
        single.assert_not_called()
        self.assertEqual(set(result), {"AMD", "MSFT"})

    def test_only_mini_batch_residue_uses_single_fallback(self):
        metrics = BatchDailyFetchStats()
        retry = daily_frame(20)
        with patch("yfinance.download", side_effect=[multiindex_download(["AMD"]), multiindex_download(["AMD"])]), patch("data_fetcher.fetch_daily", return_value=retry) as single:
            result = fetch_daily_batch(["AMD", "MSFT"], stats=metrics)
        single.assert_called_once_with("MSFT", period="max")
        self.assertEqual(metrics.single_retry_count, 1)
        self.assertEqual(metrics.single_retry_success_count, 1)
        self.assertEqual(set(result), {"AMD", "MSFT"})

    def test_many_main_batch_misses_use_mini_batches_not_many_single_fetches(self):
        symbols = [f"X{number}" for number in range(20)]
        metrics = BatchDailyFetchStats()
        call_number = 0
        def retry_download(**kwargs):
            nonlocal call_number
            call_number += 1
            return pd.DataFrame() if call_number == 1 else multiindex_download(kwargs["tickers"])
        with patch("yfinance.download", side_effect=retry_download) as download, patch("data_fetcher.fetch_daily") as single:
            result = fetch_daily_batch(symbols, stats=metrics)
        self.assertEqual(len(download.call_args_list), 3)
        self.assertEqual(metrics.mini_batch_retry_count, 2)
        self.assertEqual(metrics.mini_batch_retry_symbols, 20)
        single.assert_not_called()
        self.assertEqual(len(result), 20)

    def test_operational_error_enters_retry_layers_without_crashing(self):
        metrics = BatchDailyFetchStats()
        error = Exception("OperationalError('database is locked')")
        with patch("yfinance.download", side_effect=[error, multiindex_download(["AMD"])]) as download:
            result = fetch_daily_batch(["AMD"], stats=metrics)
        self.assertEqual(len(download.call_args_list), 2)
        self.assertEqual(metrics.batch_success_count, 0)
        self.assertEqual(metrics.mini_batch_success_count, 1)
        self.assertEqual(metrics.final_failed_count, 0)
        self.assertIn("AMD", result)


if __name__ == "__main__":
    unittest.main()
