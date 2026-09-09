from __future__ import annotations

import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

import long_main
import long_screener
import notifier
from data_fetcher import fetch_daily, resample_to_4h, resample_to_monthly, resample_to_weekly
from long_screener import LongScanStats, LongSignal
from state import AlertState


ET = ZoneInfo("America/New_York")
TUESDAY = datetime(2026, 9, 8, 17, 0, tzinfo=ET)


def bars(index: pd.DatetimeIndex, *, final_dxdx: bool = False, previous_dxdx: bool = False) -> pd.DataFrame:
    frame = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
        "volume": 1_000_000, "DXDX": False, "BLUE_ABOVE_YELLOW": False,
    }, index=index)
    if final_dxdx:
        frame.iloc[-1, frame.columns.get_loc("DXDX")] = True
    if previous_dxdx:
        frame.iloc[-2, frame.columns.get_loc("DXDX")] = True
    return frame


def weekly_bars(*, final_dxdx: bool = False, previous_dxdx: bool = False) -> pd.DataFrame:
    return bars(pd.date_range(end="2026-09-11", periods=121, freq="W-FRI", tz=ET), final_dxdx=final_dxdx, previous_dxdx=previous_dxdx)


def monthly_bars(*, final_dxdx: bool = False, previous_dxdx: bool = False) -> pd.DataFrame:
    return bars(pd.date_range(end="2026-09-30", periods=121, freq="ME", tz=ET), final_dxdx=final_dxdx, previous_dxdx=previous_dxdx)


def long_signal(symbol: str = "META", timeframe: str = "weekly", when: datetime | None = None) -> LongSignal:
    when = when or datetime(2026, 9, 4, 0, 0, tzinfo=ET)
    return LongSignal(symbol, timeframe, when, 123.45, TUESDAY)


def ohlcv(index: pd.DatetimeIndex, *, high: float = 101.0, low: float = 99.0, close: float = 100.0, volume: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame({"open": 100.0, "high": high, "low": low, "close": close, "volume": volume}, index=index)


class LongTimeframeRadarTests(unittest.TestCase):
    def test_fetch_daily_requests_prepost_false_and_rejects_intraday_extended_row(self):
        calls: dict = {}
        daily_index = pd.DatetimeIndex([
            pd.Timestamp("2026-09-08", tz=ET),
            pd.Timestamp("2026-09-08 20:00", tz=ET),
        ])

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, **kwargs):
                calls.update(kwargs)
                return ohlcv(daily_index, high=101.0)

        with patch.dict("sys.modules", {"yfinance": types.SimpleNamespace(Ticker=FakeTicker)}):
            result = fetch_daily("META", period="max")
        self.assertFalse(calls["prepost"])
        self.assertEqual(calls["interval"], "1d")
        self.assertEqual(len(result), 1)

    def test_premarket_and_afterhours_do_not_enter_4h(self):
        rth = pd.date_range("2026-09-08 09:30", periods=7, freq="h", tz=ET)
        # 09:30 through 15:30 are valid 1H bar starts; 16:30 is after-hours.
        frame = ohlcv(rth, high=110.0, low=90.0, volume=100.0)
        extras = ohlcv(pd.DatetimeIndex([
            pd.Timestamp("2026-09-08 08:30", tz=ET),
            pd.Timestamp("2026-09-08 20:00", tz=ET),
        ]), high=9_999.0, low=-999.0, volume=9_999.0)
        result = resample_to_4h(pd.concat([frame, extras]).sort_index())
        self.assertEqual(len(result), 2)
        self.assertLess(result["high"].max(), 1_000.0)
        self.assertGreater(result["low"].min(), 0.0)
        self.assertEqual(result["volume"].sum(), 700.0)

    def test_overnight_extremes_do_not_change_weekly_ohlcv(self):
        rth = ohlcv(pd.DatetimeIndex([
            pd.Timestamp("2026-09-01 09:30", tz=ET),
            pd.Timestamp("2026-09-04 09:30", tz=ET),
        ]), high=110.0, low=90.0, close=105.0, volume=100.0)
        overnight = ohlcv(pd.DatetimeIndex([pd.Timestamp("2026-09-02 20:00", tz=ET)]), high=9_999.0, low=-999.0, close=1.0, volume=9_999.0)
        weekly = resample_to_weekly(pd.concat([rth, overnight]).sort_index())
        self.assertEqual(float(weekly.iloc[0]["high"]), 110.0)
        self.assertEqual(float(weekly.iloc[0]["low"]), 90.0)
        self.assertEqual(float(weekly.iloc[0]["volume"]), 200.0)

    def test_overnight_extremes_do_not_change_monthly_ohlcv(self):
        rth = ohlcv(pd.DatetimeIndex([
            pd.Timestamp("2026-08-03 09:30", tz=ET),
            pd.Timestamp("2026-08-28 09:30", tz=ET),
        ]), high=110.0, low=90.0, close=105.0, volume=100.0)
        overnight = ohlcv(pd.DatetimeIndex([pd.Timestamp("2026-08-28 20:00", tz=ET)]), high=9_999.0, low=-999.0, close=1.0, volume=9_999.0)
        monthly = resample_to_monthly(pd.concat([rth, overnight]).sort_index())
        self.assertEqual(float(monthly.iloc[0]["high"]), 110.0)
        self.assertEqual(float(monthly.iloc[0]["low"]), 90.0)
        self.assertEqual(float(monthly.iloc[0]["volume"]), 200.0)

    def test_long_dxdx_input_is_rth_only(self):
        dates = pd.date_range("2024-03-01", "2026-09-08", freq="B", tz=ET) + pd.Timedelta(hours=9, minutes=30)
        daily = ohlcv(dates, high=101.0, low=99.0, close=100.0, volume=100.0)
        overnight = ohlcv(pd.DatetimeIndex([pd.Timestamp("2026-09-08 20:00", tz=ET)]), high=9_999.0, low=-999.0, close=1.0, volume=9_999.0)
        seen: list[pd.DataFrame] = []

        def checked_dxdx(frame: pd.DataFrame) -> pd.DataFrame:
            seen.append(frame.copy())
            self.assertLess(frame["high"].max(), 1_000.0)
            self.assertGreater(frame["low"].min(), 0.0)
            out = frame.copy()
            out["DXDX"] = out.index == pd.Timestamp("2026-09-04", tz=ET)
            return out

        with patch("long_screener.compute_macd_divergence", side_effect=checked_dxdx):
            signals, insufficient = long_screener._signals_from_daily("META", pd.concat([daily, overnight]).sort_index(), TUESDAY)
        self.assertFalse(insufficient)
        self.assertTrue(seen)
        self.assertEqual([signal.timeframe for signal in signals], ["weekly"])
        self.assertEqual(signals[0].push_date, "2026-09-08")
        self.assertEqual(signals[0].push_price, 100.0)

    def test_tuesday_excludes_forming_week_but_allows_prior_week(self):
        frame = weekly_bars(final_dxdx=True, previous_dxdx=True)
        latest = long_screener.latest_complete_long_bar(frame, "weekly", TUESDAY)
        self.assertEqual(latest[0].date().isoformat(), "2026-09-04")
        self.assertTrue(latest[1]["DXDX"])

    def test_friday_after_close_allows_current_week(self):
        frame = weekly_bars(final_dxdx=True)
        now = datetime(2026, 9, 11, 16, 20, tzinfo=ET)
        latest = long_screener.latest_complete_long_bar(frame, "weekly", now)
        self.assertEqual(latest[0].date().isoformat(), "2026-09-11")
        self.assertTrue(latest[1]["DXDX"])

    def test_forming_month_is_excluded_but_previous_month_is_allowed(self):
        frame = monthly_bars(final_dxdx=True, previous_dxdx=True)
        latest = long_screener.latest_complete_long_bar(frame, "monthly", TUESDAY)
        self.assertEqual(latest[0].date().isoformat(), "2026-08-31")
        self.assertTrue(latest[1]["DXDX"])

    def test_weekly_dxdx_creates_weekly_signal_without_blue_yellow_filter(self):
        weekly = weekly_bars(previous_dxdx=True)
        monthly = monthly_bars()
        with patch("long_screener.fetch_daily", return_value=pd.DataFrame({"close": [1]})), patch("long_screener.resample_to_weekly", return_value=weekly), patch("long_screener.resample_to_monthly", return_value=monthly), patch("long_screener.compute_macd_divergence", side_effect=lambda frame: frame):
            signals = long_screener.check_symbol("META", now=TUESDAY)
        self.assertEqual([(item.symbol, item.timeframe) for item in signals], [("META", "weekly")])

    def test_monthly_dxdx_creates_monthly_signal(self):
        weekly = weekly_bars()
        monthly = monthly_bars(previous_dxdx=True)
        with patch("long_screener.fetch_daily", return_value=pd.DataFrame({"close": [1]})), patch("long_screener.resample_to_weekly", return_value=weekly), patch("long_screener.resample_to_monthly", return_value=monthly), patch("long_screener.compute_macd_divergence", side_effect=lambda frame: frame):
            signals = long_screener.check_symbol("INTC", now=TUESDAY)
        self.assertEqual([(item.symbol, item.timeframe) for item in signals], [("INTC", "monthly")])

    def test_insufficient_monthly_history_does_not_block_weekly_scan(self):
        weekly = weekly_bars(previous_dxdx=True)
        short_monthly = bars(pd.date_range(end="2026-09-30", periods=119, freq="ME", tz=ET))
        with patch("long_screener.fetch_daily", return_value=pd.DataFrame({"close": [1]})), patch("long_screener.resample_to_weekly", return_value=weekly), patch("long_screener.resample_to_monthly", return_value=short_monthly), patch("long_screener.compute_macd_divergence", side_effect=lambda frame: frame):
            signals = long_screener.check_symbol("ORCL", now=TUESDAY)
        self.assertEqual([item.timeframe for item in signals], ["weekly"])

    def test_weekly_and_monthly_have_independent_deduplication_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AlertState(Path(directory) / "long.json", ttl_days=400)
            weekly = long_signal(timeframe="weekly")
            monthly = long_signal(timeframe="monthly", when=datetime(2026, 8, 31, 0, 0, tzinfo=ET))
            self.assertNotEqual(state.keys_for(weekly), state.keys_for(monthly))
            state.mark_sent(weekly)
            self.assertFalse(state.is_new(weekly))
            self.assertTrue(state.is_new(monthly))

    def test_same_weekly_and_monthly_bars_do_not_send_twice_but_new_bars_can(self):
        with tempfile.TemporaryDirectory() as directory:
            state = AlertState(Path(directory) / "long.json", ttl_days=400)
            weekly = long_signal(timeframe="weekly")
            monthly = long_signal(timeframe="monthly", when=datetime(2026, 8, 31, 0, 0, tzinfo=ET))
            state.mark_sent(weekly)
            state.mark_sent(monthly)
            self.assertFalse(state.is_new(weekly))
            self.assertFalse(state.is_new(monthly))
            self.assertTrue(state.is_new(long_signal(timeframe="weekly", when=datetime(2026, 9, 11, 0, 0, tzinfo=ET))))
            self.assertTrue(state.is_new(long_signal(timeframe="monthly", when=datetime(2026, 9, 30, 0, 0, tzinfo=ET))))

    def test_single_symbol_failure_does_not_abort_market_scan(self):
        with patch("long_screener.fetch_daily", side_effect=[ValueError("bad data"), pd.DataFrame({"close": [1]})]), patch("long_screener._signals_from_daily", return_value=([], False)):
            signals, stats = long_screener.run_long_screener(["AMD", "NVDA"], max_workers=1, now=TUESDAY)
        self.assertEqual(signals, [])
        self.assertEqual(stats.fetched_count, 1)
        self.assertEqual(stats.failed_count, 1)

    def test_no_signal_skips_smtp_and_writes_artifacts(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(long_main, "OUTPUT_DIR", Path(directory)), patch("long_main.run_long_screener", return_value=([], LongScanStats(pool_count=3, fetched_count=3))), patch("long_main.send_email") as sent:
            long_main.run_once({"max_workers": 1}, ["AMD"], AlertState(Path(directory) / "long.json", ttl_days=400))
            sent.assert_not_called()
            self.assertTrue((Path(directory) / "long_dxdx_signals.csv").exists())
            self.assertTrue((Path(directory) / "long_dxdx_report.txt").exists())

    def test_dry_run_never_creates_or_updates_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(long_main, "OUTPUT_DIR", Path(directory)), patch("long_main.run_long_screener", return_value=([], LongScanStats(pool_count=1, fetched_count=1))):
            state_path = Path(directory) / "long.json"
            long_main.run_once({"max_workers": 1}, ["AMD"], AlertState(state_path, ttl_days=400), dry_run=True)
            self.assertFalse(state_path.exists())

    def test_dry_run_artifact_keeps_all_detected_signals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(long_main, "OUTPUT_DIR", Path(directory)), patch("long_main.run_long_screener", return_value=([long_signal("META", "weekly"), long_signal("INTC", "monthly")], LongScanStats(pool_count=2, fetched_count=2))):
            state = AlertState(Path(directory) / "long.json", ttl_days=400)
            long_main.run_once({"max_workers": 1}, ["META", "INTC"], state, dry_run=True)
            with (Path(directory) / "long_dxdx_signals.csv").open(encoding="utf-8-sig") as handle:
                rows = list(__import__("csv").DictReader(handle))
            self.assertEqual({row["symbol"] for row in rows}, {"META", "INTC"})

    def test_formal_artifact_contains_only_newly_emailed_signals(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(long_main, "OUTPUT_DIR", Path(directory)), patch("long_main.run_long_screener", return_value=([long_signal("META", "weekly"), long_signal("INTC", "monthly")], LongScanStats(pool_count=2, fetched_count=2))), patch("long_main.require_smtp_config"), patch("long_main.send_email") as sent:
            state = AlertState(Path(directory) / "long.json", ttl_days=400)
            state.mark_sent(long_signal("META", "weekly"))
            config = {"max_workers": 1, "smtp_host": "smtp.example.test", "smtp_port": 587, "smtp_user": "sender@example.test", "smtp_password": "secret", "to_addrs": ["to@example.test"]}
            long_main.run_once(config, ["META", "INTC"], state)
            with (Path(directory) / "long_dxdx_signals.csv").open(encoding="utf-8-sig") as handle:
                rows = list(__import__("csv").DictReader(handle))
            self.assertEqual([row["symbol"] for row in rows], ["INTC"])
            sent.assert_called_once()

    def test_long_email_contains_timeframes_ticker_signal_time_and_close(self):
        monthly = long_signal("INTC", "monthly", datetime(2026, 8, 31, 0, 0, tzinfo=ET))
        weekly = long_signal("ORCL", "weekly", datetime(2026, 9, 4, 0, 0, tzinfo=ET))
        subject, body = notifier.format_long_signals_email([monthly, weekly], pool_count=2, scan_time=TUESDAY)
        self.assertIn("月1 周1", subject)
        self.assertIn("月线抄底", body)
        self.assertIn("周线抄底", body)
        self.assertIn("INTC", body)
        self.assertIn("ORCL", body)
        self.assertIn("2026-08-31", body)
        self.assertIn("$123.45", body)

    def test_long_workflow_is_independent_and_original_workflow_is_unchanged(self):
        long_workflow = Path(".github/workflows/long_screen.yml").read_text(encoding="utf-8")
        original = Path(".github/workflows/screen.yml").read_text(encoding="utf-8")
        self.assertIn("US Weekly Monthly DXDX Pullback Radar", long_workflow)
        self.assertIn('cron: "10 23 * * 1-5"', long_workflow)
        self.assertIn("long_main.py", long_workflow)
        self.assertNotIn("long_main.py", original)
        self.assertNotIn("long_alert_state.json", original)


if __name__ == "__main__":
    unittest.main()
