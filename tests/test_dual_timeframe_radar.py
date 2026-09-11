from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from data_fetcher import resample_to_4h, rth_hourly_to_daily
from indicators import add_all_indicators
import main
import notifier
import screener
import universe
from screener import ScanStats, Signal
from state import AlertState
from universe import combine_us_universe, is_us_listed_stock


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 17, 0, tzinfo=ET)


def daily_frame(*, dxdx: bool = False, bullish: bool = True) -> pd.DataFrame:
    index = pd.date_range(end=pd.Timestamp(NOW.date(), tz=ET), periods=120, freq="B")
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000, "DXDX": False, "BLUE_ABOVE_YELLOW": bullish, "BLUE_FULLY_ABOVE_YELLOW": bullish}, index=index)
    frame.iloc[-1, frame.columns.get_loc("DXDX")] = dxdx
    return frame


def h4_frame(*, first_today: bool = False, second_today: bool = False, old_signal: bool = False) -> pd.DataFrame:
    days = pd.date_range(end=pd.Timestamp(NOW.date(), tz=ET), periods=60, freq="B")
    index = []
    for day in days:
        index.extend([day.normalize() + pd.Timedelta(hours=13, minutes=30), day.normalize() + pd.Timedelta(hours=16)])
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000, "DXDX": False, "BLUE_ABOVE_YELLOW": True, "BLUE_FULLY_ABOVE_YELLOW": True}, index=pd.DatetimeIndex(index))
    if old_signal:
        frame.iloc[-3, frame.columns.get_loc("DXDX")] = True
    frame.iloc[-2, frame.columns.get_loc("DXDX")] = first_today
    frame.iloc[-1, frame.columns.get_loc("DXDX")] = second_today
    return frame


def real_bfb_rth_hourly_with_extended_extremes() -> pd.DataFrame:
    """Recorded Yahoo BF-B RTH 1H values for 2026-09-10 plus non-RTH noise."""
    rows = [
        ("08:30", 1.0, 999.0, 0.01, 1.0),  # pre-market: must be discarded
        ("09:30", 26.209999, 26.379999, 26.084999, 26.330000),
        ("10:30", 26.350000, 26.590000, 26.299999, 26.490000),
        ("11:30", 26.480000, 26.559999, 26.420000, 26.555000),
        ("12:30", 26.559999, 26.639999, 26.540001, 26.570000),
        ("13:30", 26.570000, 26.629999, 26.520000, 26.549999),
        ("14:30", 26.540001, 26.620000, 26.500000, 26.575001),
        ("15:30", 26.575001, 26.650000, 26.559999, 26.610001),
        ("16:30", 1.0, 999.0, 0.01, 1.0),  # after-hours: must be discarded
    ]
    index = pd.DatetimeIndex([pd.Timestamp(f"2026-09-10 {time}", tz=ET) for time, *_ in rows])
    return pd.DataFrame(
        [{"open": op, "high": high, "low": low, "close": close, "volume": 100} for _, op, high, low, close in rows],
        index=index,
    )


def early_close_rth_hourly_with_extended_extremes(day: str = "2026-11-27") -> pd.DataFrame:
    """XNYS Black Friday session: 09:30--13:00, plus forbidden extras."""
    index = pd.DatetimeIndex([
        pd.Timestamp(f"{day} 08:30", tz=ET),
        pd.Timestamp(f"{day} 09:30", tz=ET),
        pd.Timestamp(f"{day} 10:30", tz=ET),
        pd.Timestamp(f"{day} 11:30", tz=ET),
        pd.Timestamp(f"{day} 12:30", tz=ET),
        pd.Timestamp(f"{day} 13:30", tz=ET),
    ])
    return pd.DataFrame(
        {
            "open": [1.0, 10.0, 11.0, 12.0, 13.0, 1.0],
            "high": [999.0, 11.0, 12.0, 13.0, 14.0, 999.0],
            "low": [0.01, 9.0, 10.0, 11.0, 12.0, 0.01],
            "close": [1.0, 11.0, 12.0, 13.0, 14.0, 1.0],
            "volume": [9999.0, 100.0, 100.0, 100.0, 100.0, 9999.0],
        },
        index=index,
    )


def signal(symbol: str = "AMD", *, daily: bool = False, h4: bool = True, moment: datetime | None = None) -> Signal:
    moment = moment or datetime(2026, 9, 8, 16, 0, tzinfo=ET)
    level = "S" if daily and h4 else ("A" if h4 else "B")
    return Signal(symbol, level, daily, h4, moment if daily else None, moment if h4 else None, 100.0, True, NOW)


class DualTimeframeRadarTests(unittest.TestCase):
    def _check(self, daily: pd.DataFrame, h4: pd.DataFrame, now: datetime = NOW, strict: bool = False):
        with patch("screener.fetch_daily", return_value=daily), patch("screener.fetch_hourly", return_value=pd.DataFrame()), patch("screener.resample_to_4h", return_value=h4), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            return screener.check_symbol("AMD", now=now, require_strict_separation=strict)

    def test_sp500_and_nasdaq100_are_deduplicated(self):
        universe = combine_us_universe(["AAPL", "MSFT"], ["MSFT", "NVDA"])
        self.assertEqual(universe, ["AAPL", "MSFT", "NVDA"])

    def test_non_us_and_etf_symbols_are_rejected(self):
        self.assertFalse(is_us_listed_stock("0700.HK"))
        self.assertFalse(is_us_listed_stock("600519.SS"))
        self.assertFalse(is_us_listed_stock("SPY"))
        self.assertFalse(is_us_listed_stock("BTC-USD"))
        self.assertTrue(is_us_listed_stock("TSM"))  # US-listed ADR

    def test_untrusted_cache_cannot_inject_an_etf(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(universe, "CACHE_DIR", Path(directory)):
            pd.DataFrame({"ticker": ["VYM"]}).to_csv(Path(directory) / "sp500.csv", index=False)
            self.assertEqual(universe._load_or_fetch("sp500", lambda: ["AAPL"]), ["AAPL"])

    def test_bullish_trend_is_required(self):
        self.assertIsNone(self._check(daily_frame(dxdx=True, bullish=False), h4_frame()))

    def test_strict_trend_mode_remains_available(self):
        daily = daily_frame(dxdx=True, bullish=True)
        daily["BLUE_FULLY_ABOVE_YELLOW"] = False
        self.assertIsNone(self._check(daily, h4_frame(), strict=True))

    def test_h4_dxdx_creates_a_level(self):
        result = self._check(daily_frame(), h4_frame(second_today=True))
        self.assertEqual(result.signal_level, "A")
        self.assertTrue(result.h4_dxdx)

    def test_stale_daily_context_suppresses_current_day_h4_signal(self):
        # Simulates Yahoo finalising the RTH 1H response before its 1D response.
        yesterday = pd.Timestamp(NOW.date(), tz=ET) - pd.offsets.BDay(1)
        daily = daily_frame()
        daily.index = pd.date_range(end=yesterday, periods=120, freq="B")
        with self.assertLogs("screener", level="WARNING") as logs:
            result = self._check(daily, h4_frame(second_today=True))
        self.assertIsNone(result)
        self.assertIn("latest completed daily bar", "\n".join(logs.output))

    def test_same_day_completed_daily_context_allows_h4_signal(self):
        result = self._check(daily_frame(), h4_frame(second_today=True))
        self.assertEqual(result.signal_level, "A")

    def test_real_bfb_rth_4h_bucket_boundaries_and_extended_extremes(self):
        bars = resample_to_4h(real_bfb_rth_hourly_with_extended_extremes())
        self.assertEqual(list(bars.index.strftime("%H:%M")), ["13:30", "16:00"])
        first, second = bars.iloc[0], bars.iloc[1]
        self.assertAlmostEqual(first["open"], 26.209999, places=5)
        self.assertAlmostEqual(first["high"], 26.639999, places=5)
        self.assertAlmostEqual(first["low"], 26.084999, places=5)
        self.assertAlmostEqual(first["close"], 26.570000, places=5)
        self.assertAlmostEqual(second["open"], 26.570000, places=5)
        self.assertAlmostEqual(second["high"], 26.650000, places=5)
        self.assertAlmostEqual(second["low"], 26.500000, places=5)
        self.assertAlmostEqual(second["close"], 26.610001, places=5)

    def test_h4_candidate_diagnostic_contains_formula_and_daily_context(self):
        daily = daily_frame()
        h4 = h4_frame(second_today=True)
        h4[["DIF", "DEA", "MACD_bar"]] = [-0.2, -0.1, -0.2]
        h4["CCC"] = True
        h4["JJJ"] = True
        with patch("screener.fetch_daily", return_value=daily), patch("screener.fetch_hourly", return_value=pd.DataFrame()), patch("screener.resample_to_4h", return_value=h4), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            _, diagnostic, stale = screener._check_symbol_with_diagnostic("AMD", now=NOW)
        self.assertFalse(stale)
        self.assertTrue(diagnostic.dxdx)
        self.assertTrue(diagnostic.ccc)
        self.assertTrue(diagnostic.jjj)
        self.assertEqual(diagnostic.daily_bar_date.date(), NOW.date())
        self.assertTrue(diagnostic.daily_fresh_for_h4)
        self.assertEqual(diagnostic.daily_context_source, "yahoo_daily")

    def test_completed_rth_hourly_fallback_builds_correct_daily_ohlcv(self):
        hourly = real_bfb_rth_hourly_with_extended_extremes()
        daily = rth_hourly_to_daily(
            hourly,
            pd.Timestamp("2026-09-10", tz=ET),
            now=pd.Timestamp("2026-09-10 16:20", tz=ET),
        )
        self.assertEqual(len(daily), 1)
        bar = daily.iloc[0]
        self.assertAlmostEqual(bar["open"], 26.209999, places=5)
        self.assertAlmostEqual(bar["high"], 26.650000, places=5)
        self.assertAlmostEqual(bar["low"], 26.084999, places=5)
        self.assertAlmostEqual(bar["close"], 26.610001, places=5)
        self.assertEqual(bar["volume"], 700.0)  # seven RTH bars; extended rows excluded

    def test_rth_hourly_fallback_requires_all_session_bars_after_close_grace(self):
        hourly = real_bfb_rth_hourly_with_extended_extremes()
        before_close = rth_hourly_to_daily(
            hourly,
            pd.Timestamp("2026-09-10", tz=ET),
            now=pd.Timestamp("2026-09-10 16:19", tz=ET),
        )
        incomplete = rth_hourly_to_daily(
            hourly.drop(pd.Timestamp("2026-09-10 15:30", tz=ET)),
            pd.Timestamp("2026-09-10", tz=ET),
            now=pd.Timestamp("2026-09-10 16:20", tz=ET),
        )
        self.assertTrue(before_close.empty)
        self.assertTrue(incomplete.empty)

    def test_early_close_rth_fallback_uses_actual_1300_close_and_excludes_afterhours(self):
        hourly = early_close_rth_hourly_with_extended_extremes()
        before_grace = rth_hourly_to_daily(
            hourly,
            pd.Timestamp("2026-11-27", tz=ET),
            now=pd.Timestamp("2026-11-27 13:19", tz=ET),
        )
        completed = rth_hourly_to_daily(
            hourly,
            pd.Timestamp("2026-11-27", tz=ET),
            now=pd.Timestamp("2026-11-27 13:20", tz=ET),
        )
        missing_required = rth_hourly_to_daily(
            hourly.drop(pd.Timestamp("2026-11-27 11:30", tz=ET)),
            pd.Timestamp("2026-11-27", tz=ET),
            now=pd.Timestamp("2026-11-27 13:20", tz=ET),
        )
        self.assertTrue(before_grace.empty)
        self.assertTrue(missing_required.empty)
        self.assertEqual(len(completed), 1)
        self.assertEqual(float(completed.iloc[0]["high"]), 14.0)
        self.assertEqual(float(completed.iloc[0]["low"]), 9.0)
        self.assertEqual(float(completed.iloc[0]["close"]), 14.0)
        self.assertEqual(float(completed.iloc[0]["volume"]), 400.0)

    def test_early_close_4h_bar_ends_at_actual_session_close(self):
        bars = resample_to_4h(early_close_rth_hourly_with_extended_extremes())
        self.assertEqual(list(bars.index.strftime("%H:%M")), ["13:00"])
        self.assertEqual(float(bars.iloc[0]["volume"]), 400.0)

    def test_stale_daily_uses_same_day_rth_hourly_fallback_and_real_close(self):
        yesterday = pd.Timestamp(NOW.date(), tz=ET) - pd.offsets.BDay(1)
        index = pd.date_range(end=yesterday, periods=120, freq="B")
        daily = pd.DataFrame(
            {"open": 26.0, "high": 26.5, "low": 25.5, "close": 26.0, "volume": 1_000_000},
            index=index,
        )
        h4 = h4_frame(second_today=True)
        hourly = real_bfb_rth_hourly_with_extended_extremes().copy()
        hourly.index = hourly.index + pd.Timedelta(days=(pd.Timestamp(NOW.date()) - pd.Timestamp("2026-09-10")).days)

        def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
            # The pre-built 4H fixture already carries its candidate DXDX;
            # daily input must use the real indicator implementation.
            return frame if "DXDX" in frame.columns else add_all_indicators(frame)

        with patch("screener.fetch_daily", return_value=daily), patch("screener.fetch_hourly", return_value=hourly), patch("screener.resample_to_4h", return_value=h4), patch("screener.add_all_indicators", side_effect=add_indicators):
            result, diagnostic, stale = screener._check_symbol_with_diagnostic("AMD", now=NOW)
        self.assertFalse(stale)
        self.assertEqual(result.signal_level, "A")
        self.assertAlmostEqual(result.close, 26.610001, places=5)
        self.assertEqual(diagnostic.daily_context_source, "rth_hourly_fallback")
        self.assertEqual(diagnostic.daily_bar_date.date(), NOW.date())
        self.assertTrue(diagnostic.blue_above_yellow)

    def test_incomplete_hourly_keeps_stale_daily_candidate_rejected(self):
        yesterday = pd.Timestamp(NOW.date(), tz=ET) - pd.offsets.BDay(1)
        daily = daily_frame()
        daily.index = pd.date_range(end=yesterday, periods=120, freq="B")
        hourly = real_bfb_rth_hourly_with_extended_extremes().copy()
        hourly.index = hourly.index + pd.Timedelta(days=(pd.Timestamp(NOW.date()) - pd.Timestamp("2026-09-10")).days)
        hourly = hourly.drop(hourly.index[hourly.index.strftime("%H:%M") == "15:30"])
        with patch("screener.fetch_daily", return_value=daily), patch("screener.fetch_hourly", return_value=hourly), patch("screener.resample_to_4h", return_value=h4_frame(second_today=True)), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            result, diagnostic, stale = screener._check_symbol_with_diagnostic("AMD", now=NOW)
        self.assertIsNone(result)
        self.assertTrue(stale)
        self.assertEqual(diagnostic.daily_context_source, "stale_rejected")

    def test_daily_dxdx_creates_b_level(self):
        result = self._check(daily_frame(dxdx=True), h4_frame())
        self.assertEqual(result.signal_level, "B")
        self.assertTrue(result.daily_dxdx)

    def test_daily_and_h4_create_s_level(self):
        result = self._check(daily_frame(dxdx=True), h4_frame(second_today=True))
        self.assertEqual(result.signal_level, "S")

    def test_both_current_day_h4_bars_are_checked(self):
        result = self._check(daily_frame(), h4_frame(first_today=True))
        self.assertEqual(result.signal_level, "A")
        self.assertEqual(result.h4_signal_time.hour, 13)

    def test_unclosed_h4_bar_cannot_trigger(self):
        before_close = datetime(2026, 9, 8, 13, 40, tzinfo=ET)
        self.assertIsNone(self._check(daily_frame(), h4_frame(first_today=True), before_close))

    def test_old_h4_signal_cannot_trigger(self):
        self.assertIsNone(self._check(daily_frame(), h4_frame(old_signal=True)))

    def test_latest_complete_daily_bar_is_required(self):
        before_daily_close = datetime(2026, 9, 8, 15, 0, tzinfo=ET)
        self.assertIsNone(self._check(daily_frame(dxdx=True), h4_frame(), before_daily_close))

    def test_same_candle_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AlertState(Path(directory) / "state.json")
            item = signal()
            self.assertTrue(state.is_new(item))
            state.mark_sent(item)
            self.assertFalse(state.is_new(item))

    def test_new_candle_can_be_sent_again(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AlertState(Path(directory) / "state.json")
            state.mark_sent(signal(moment=datetime(2026, 9, 8, 13, 30, tzinfo=ET)))
            self.assertTrue(state.is_new(signal(moment=datetime(2026, 9, 8, 16, 0, tzinfo=ET))))

    def test_no_signal_skips_email_and_writes_reports(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "OUTPUT_DIR", Path(directory)), patch("main.run_screener", return_value=([], ScanStats(pool_count=3, fetched_count=3))), patch("main.send_email") as sent:
            main.run_once({"strict_separation": False, "max_workers": 1}, ["AMD"], AlertState(Path(directory) / "state.json"))
            sent.assert_not_called()
            self.assertTrue((Path(directory) / "dxdx_report.txt").exists())

    def test_no_signal_does_not_require_smtp_settings(self):
        with patch.dict("os.environ", {}, clear=True):
            config = main.load_config()
        self.assertIsNone(config["smtp_host"])

    def test_gmail_starttls_mock(self):
        calls = []
        class FakeSMTP:
            def __init__(self, *args, **kwargs): calls.append("connect")
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def ehlo(self): calls.append("ehlo")
            def starttls(self, **kwargs): calls.append("starttls")
            def login(self, *args): calls.append("login")
            def send_message(self, *args): calls.append("send")
        with patch("notifier.smtplib.SMTP", FakeSMTP):
            notifier.send_email("smtp.gmail.com", 587, "sender@example.test", "app-password", ["to@example.test"], "test", "body")
        self.assertIn("starttls", calls)
        self.assertIn("send", calls)

    def test_chinese_email_contains_signal_levels(self):
        _, body = notifier.format_signals_email([signal(daily=True, h4=True), signal("NVDA", h4=True)], pool_count=2, scan_time=NOW)
        self.assertIn("S级｜双周期共振", body)
        self.assertIn("A级｜4H抄底", body)
        self.assertIn("趋势：蓝梯 > 黄梯", body)

    def test_one_symbol_failure_does_not_abort_scan(self):
        with patch("screener.check_symbol", side_effect=[ValueError("bad data"), None]):
            signals, stats = screener.run_screener(["AMD", "NVDA"], max_workers=1)
        self.assertEqual(signals, [])
        self.assertEqual(stats.failed_count, 1)
        self.assertEqual(stats.fetched_count, 1)

    def test_artifacts_have_required_columns(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "OUTPUT_DIR", Path(directory)):
            main.write_reports([signal()], ScanStats(pool_count=1, fetched_count=1), email_sent=False, detected_at=NOW)
            csv_text = (Path(directory) / "dxdx_signals.csv").read_text(encoding="utf-8-sig")
            self.assertIn("signal_level", csv_text)
            self.assertIn("h4_signal_time", csv_text)
            diagnostic_text = (Path(directory) / "h4_dxdx_diagnostics.csv").read_text(encoding="utf-8-sig")
            self.assertIn("daily_fresh_for_h4", diagnostic_text)
            self.assertIn("daily_context_source", diagnostic_text)
            self.assertIn("macd_bar", diagnostic_text)
            self.assertTrue((Path(directory) / "dxdx_report.txt").exists())

    def test_workflow_is_daily_not_half_hourly(self):
        workflow = Path(".github/workflows/screen.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "30 22 * * 1-5"', workflow)
        self.assertNotIn("*/30", workflow)
        self.assertIn("dxdx_signals.csv", workflow)
        self.assertIn("h4_dxdx_diagnostics.csv", workflow)

    def test_readme_matches_levels_and_independent_design(self):
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("S级：双周期共振", readme)
        self.assertIn("A级：4H 抄底", readme)
        self.assertIn("B级：日线抄底", readme)
        self.assertIn("完全独立", readme)
        self.assertNotIn("每 30 分钟", readme)


if __name__ == "__main__":
    unittest.main()
