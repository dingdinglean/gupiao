from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

import etf_main
import etf_screener
from etf_notifier import format_etf_signals_email
from etf_universe import ETF_NAMES, ETF_UNIVERSE, get_etf_universe, is_etf_symbol
from indicators import add_all_indicators
from screener import DailyDiagnostic, ScanStats, Signal
from state import AlertState
from universe import combine_us_universe, is_us_listed_stock


ET = ZoneInfo("America/New_York")
TARGET = pd.Timestamp("2026-09-18", tz=ET)


def prepared_daily(*, signal: bool = True) -> pd.DataFrame:
    index = pd.date_range(end=TARGET, periods=120, freq="B", tz=ET)
    frame = pd.DataFrame(
        {
            "open": 532.0, "high": 535.0, "low": 530.0, "close": 533.07,
            "volume": 1_000_000.0, "DIF": -1.2, "DEA": -1.0,
            "MACD_bar": -0.4, "N1": 4.0, "MM1": 7.0, "CC1": 1.0,
            "CC2": 0.0, "CC3": 1.0, "DIFL1": -1.0, "DIFL2": -2.0,
            "DIFL3": -3.0, "AAA": True, "BBB": False, "CCC": True,
            "JJJ": False, "DXDX": False, "BLUE_UP": 540.0, "BLUE_DW": 520.0,
            "YELLOW_UP": 510.0, "YELLOW_DW": 500.0,
            "BLUE_ABOVE_YELLOW": True, "BLUE_FULLY_ABOVE_YELLOW": True,
        },
        index=index,
    )
    frame.loc[TARGET, ["JJJ", "DXDX"]] = signal
    return frame


def etf_signal(symbol: str = "SOXX") -> Signal:
    return Signal(
        symbol, "DAILY", True, False, TARGET.to_pydatetime(), None, 533.07,
        True, datetime(2026, 9, 19, 8, 0, tzinfo=ET),
        scan_mode="official_daily_confirmation",
        daily_data_source="yahoo_official_daily", h4_context_source="",
        source_radar="etf_daily", source_timeframe="daily",
    )


def diagnostic(symbol: str = "SOXX") -> DailyDiagnostic:
    return DailyDiagnostic(
        symbol, TARGET.to_pydatetime(), "official_daily_confirmation", True,
        "yahoo_official_daily", 120, 532.0, 535.0, 530.0, 533.07,
        -1.2, -1.0, -0.4, 4.0, 7.0, 1.0, 0.0, 1.0, -1.0, -2.0, -3.0,
        True, False, True, False, True, True, False, "DAILY",
        540.0, 520.0, 510.0, 500.0, True, True,
    )


class ETFDailyRadarTests(unittest.TestCase):
    def test_etf_universe_is_exact_fixed_17(self):
        self.assertEqual(
            ETF_UNIVERSE,
            ("SPY", "QQQ", "DIA", "IWM", "XLK", "SOXX", "SMH", "XLC", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE"),
        )
        self.assertEqual(len(ETF_NAMES), 17)
        self.assertEqual(get_etf_universe(), list(ETF_UNIVERSE))

    def test_soxx_is_accepted_only_by_etf_whitelist(self):
        self.assertTrue(is_etf_symbol("SOXX"))
        self.assertFalse(is_us_listed_stock("SOXX"))

    def test_spy_qqq_and_all_etfs_remain_out_of_stock_scanner(self):
        self.assertTrue(all(not is_us_listed_stock(symbol) for symbol in ETF_UNIVERSE))
        self.assertEqual(combine_us_universe(list(ETF_UNIVERSE), []), [])

    def test_etf_whitelist_rejects_unapproved_funds_and_stocks(self):
        for symbol in ("TQQQ", "SQQQ", "IBIT", "GLD", "TLT", "AAPL"):
            self.assertFalse(is_etf_symbol(symbol))

    def test_etf_scanner_uses_one_max_history_batch_of_17(self):
        seen = {}

        def fake_batch(symbols, period, batch_size, *, stats):
            seen.update(symbols=list(symbols), period=period, batch_size=batch_size)
            stats.universe_count = len(symbols)
            stats.batch_size = batch_size
            stats.batch_count = 1
            stats.batch_success_count = len(symbols)
            return {symbol: prepared_daily(signal=False) for symbol in symbols}

        with patch("screener.fetch_daily_batch", side_effect=fake_batch), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            _, stats, rows = etf_screener.run_etf_confirmation_screener([TARGET])
        self.assertEqual(seen, {"symbols": list(ETF_UNIVERSE), "period": "max", "batch_size": 50})
        self.assertEqual(stats.batch_count, 1)
        self.assertEqual(len(rows), 17)

    def test_etf_scanner_never_fetches_hourly_or_single_daily(self):
        frames = {symbol: prepared_daily(signal=False) for symbol in ETF_UNIVERSE}
        with patch("screener.fetch_daily_batch", return_value=frames), patch("screener.fetch_daily") as single, patch("screener.fetch_hourly") as hourly, patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            etf_screener.run_etf_confirmation_screener([TARGET])
        single.assert_not_called()
        hourly.assert_not_called()

    def test_shared_fetcher_requests_adjusted_official_daily_only(self):
        import data_fetcher
        fake_yf = MagicMock()
        data_fetcher._download_daily_batch(fake_yf, ["SOXX"], period="max", threads=8)
        fake_yf.download.assert_called_once_with(
            tickers=["SOXX"], period="max", interval="1d", auto_adjust=True,
            prepost=False, progress=False, threads=8, timeout=12,
        )

    def test_etf_scanner_calls_the_same_add_all_indicators(self):
        frames = {symbol: prepared_daily(signal=False) for symbol in ETF_UNIVERSE}
        with patch("screener.fetch_daily_batch", return_value=frames), patch("screener.add_all_indicators", side_effect=lambda frame: frame) as indicators:
            etf_screener.run_etf_confirmation_screener([TARGET])
        self.assertEqual(indicators.call_count, 17)

    def test_etf_dxdx_diagnostic_matches_production_indicators(self):
        index = pd.date_range("2025-01-02", periods=420, freq="B", tz=ET)
        close = pd.Series([100 + value * 0.03 + (value % 17 - 8) ** 2 * 0.02 for value in range(len(index))], index=index)
        raw = pd.DataFrame({"open": close - .2, "high": close + .7, "low": close - .8, "close": close, "volume": 1_000_000.0}, index=index)
        expected = add_all_indicators(raw).iloc[-1]
        session = pd.Timestamp(raw.index[-1])
        with patch("screener.fetch_daily_batch", return_value={symbol: raw for symbol in ETF_UNIVERSE}):
            _, _, rows = etf_screener.run_etf_confirmation_screener([session])
        soxx = next(row for row in rows if row.symbol == "SOXX")
        self.assertEqual(soxx.DXDX, bool(expected["DXDX"]))
        self.assertAlmostEqual(soxx.DIFF, float(expected["DIF"]))

    def test_etf_blue_above_yellow_matches_production_indicators(self):
        raw = prepared_daily(signal=False)[["open", "high", "low", "close", "volume"]]
        expected = add_all_indicators(raw).iloc[-1]
        with patch("screener.fetch_daily_batch", return_value={symbol: raw for symbol in ETF_UNIVERSE}):
            _, _, rows = etf_screener.run_etf_confirmation_screener([TARGET])
        soxx = next(row for row in rows if row.symbol == "SOXX")
        self.assertEqual(soxx.BLUE_ABOVE_YELLOW, bool(expected["BLUE_ABOVE_YELLOW"]))

    def test_five_session_catch_up_produces_85_diagnostics(self):
        sessions = list(pd.date_range(end=TARGET, periods=5, freq="B", tz=ET)[::-1])
        frames = {symbol: prepared_daily(signal=False) for symbol in ETF_UNIVERSE}
        with patch("screener.fetch_daily_batch", return_value=frames), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            _, _, rows = etf_screener.run_etf_confirmation_screener(sessions)
        self.assertEqual(len(rows), 85)

    def test_etf_state_path_and_key_are_independent(self):
        self.assertEqual(etf_main.ETF_STATE_PATH.as_posix(), "data/etf_daily_alert_state.json")
        with tempfile.TemporaryDirectory() as directory:
            etf_state = AlertState(Path(directory) / "etf.json")
            stock_state = AlertState(Path(directory) / "stock.json")
            item = etf_signal()
            etf_state.mark_sent(item)
            self.assertFalse(etf_state.is_new(item))
            self.assertTrue(stock_state.is_new(item))
            self.assertEqual(etf_state.keys_for(item), ["SOXX|DAILY|2026-09-18T00:00:00-04:00"])

    def test_dry_run_never_emails_filters_saves_or_marks_state(self):
        state = MagicMock()
        stats = ScanStats(pool_count=17, fetched_count=17, batch_size=50, batch_count=1)
        cfg = {"strict_separation": False, "daily_batch_size": 50}
        with tempfile.TemporaryDirectory() as directory, patch.object(etf_main, "OUTPUT_DIR", Path(directory)), patch("etf_main.run_etf_confirmation_screener", return_value=([etf_signal()], stats, [diagnostic()])), patch("etf_main.send_email") as email:
            signals, _, sent = etf_main.run_etf_confirmation(cfg, state, dry_run=True, now=datetime(2026, 9, 20, 8, 0, tzinfo=ET))
        self.assertEqual(len(signals), 1)
        self.assertFalse(sent)
        email.assert_not_called()
        state.filter_new.assert_not_called()
        state.save.assert_not_called()
        state.mark_sent.assert_not_called()

    def test_cli_dry_run_does_not_construct_formal_state(self):
        args = MagicMock(dry_run=True, replay=None)
        with patch("etf_main.parse_args", return_value=args), patch("etf_main.load_config", return_value={}), patch("etf_main.AlertState") as state_type, patch("etf_main.run_etf_confirmation") as run:
            etf_main.main()
        state_type.assert_not_called()
        run.assert_called_once_with({}, None, dry_run=True)

    def test_etf_signal_metadata_is_explicit(self):
        frames = {symbol: prepared_daily(signal=(symbol == "SOXX")) for symbol in ETF_UNIVERSE}
        with patch("screener.fetch_daily_batch", return_value=frames), patch("screener.add_all_indicators", side_effect=lambda frame: frame):
            signals, _, _ = etf_screener.run_etf_confirmation_screener([TARGET])
        item = signals[0]
        self.assertEqual(item.symbol, "SOXX")
        self.assertEqual(item.signal_level, "DAILY")
        self.assertTrue(item.daily_dxdx)
        self.assertFalse(item.h4_dxdx)
        self.assertEqual(item.source_radar, "etf_daily")
        self.assertEqual(item.source_timeframe, "daily")
        self.assertEqual(item.daily_data_source, "yahoo_official_daily")
        self.assertEqual(item.h4_context_source, "")

    def test_etf_artifacts_have_names_sources_and_core_diagnostics(self):
        stats = ScanStats(pool_count=17, fetched_count=17, batch_size=50, batch_count=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(etf_main, "OUTPUT_DIR", Path(directory)):
            etf_main.write_etf_reports([etf_signal()], stats, email_sent=False, detected_at=datetime.now(tz=ET), diagnostics=[diagnostic()], dry_run=True, state_restored=False, state_saved=False, marked_sent=0)
            with (Path(directory) / "etf_daily_signals.csv").open(encoding="utf-8-sig", newline="") as handle:
                signal_row = next(csv.DictReader(handle))
            with (Path(directory) / "etf_daily_diagnostics.csv").open(encoding="utf-8-sig", newline="") as handle:
                diagnostic_row = next(csv.DictReader(handle))
        self.assertEqual(signal_row["name"], "半导体ETF-iShares")
        self.assertEqual(signal_row["source_radar"], "etf_daily")
        self.assertEqual(signal_row["signal_level"], "DAILY")
        self.assertEqual(signal_row["h4_dxdx"], "False")
        for field in etf_main.DIAGNOSTIC_FIELDS:
            self.assertIn(field, diagnostic_row)

    def test_etf_email_is_separate_and_contains_required_fields(self):
        subject, body = format_etf_signals_email([etf_signal()])
        self.assertEqual(subject, "【美股ETF日线抄底】SOXX 等 1 只")
        for text in ("SOXX｜半导体ETF-iShares", "信号日：2026-09-18", "收盘：533.07", "DXDX：是", "蓝梯>黄梯：是"):
            self.assertIn(text, body)

    def test_soxx_replay_regression_uses_official_shared_path(self):
        output = io.StringIO()
        with patch("data_fetcher.fetch_daily_batch", return_value={"SOXX": prepared_daily()}), patch("screener.add_all_indicators", side_effect=lambda frame: frame), redirect_stdout(output):
            result = etf_main.replay_etf_daily("SOXX", "2026-09-18")
        self.assertEqual(result, 0)
        self.assertIn("symbol=SOXX", output.getvalue())
        self.assertIn("DXDX=True", output.getvalue())
        self.assertIn("BLUE_ABOVE_YELLOW=True", output.getvalue())

    def test_etf_workflow_is_independent_daily_schedule_and_artifacts(self):
        workflow = Path(".github/workflows/etf_daily_screen.yml").read_text(encoding="utf-8")
        stock_workflow = Path(".github/workflows/screen.yml").read_text(encoding="utf-8")
        self.assertIn("name: US ETF Daily DXDX Radar", workflow)
        self.assertIn('cron: "0 8 * * 2-6"', workflow)
        self.assertIn("python etf_main.py", workflow)
        self.assertIn("data/etf_daily_alert_state.json", workflow)
        self.assertIn("etf_daily_signals.csv", workflow)
        self.assertNotIn("etf_main.py", stock_workflow)

    def test_stock_confirmation_defaults_are_unchanged(self):
        import screener
        frame = prepared_daily()
        with patch("screener.fetch_daily_batch", return_value={"SOXX": frame}), patch("screener.add_all_indicators", side_effect=lambda item: item):
            signals, stats, rows = screener.run_confirmation_screener(["SOXX"], [TARGET])
        self.assertEqual(signals, [])
        self.assertEqual(rows, [])
        self.assertEqual(stats.pool_count, 0)


if __name__ == "__main__":
    unittest.main()
