"""Incomplete fetches stay visible without changing email rules."""
from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import data_fetcher
import main
import universe
from screener import ScanStats, Signal


ET = ZoneInfo("America/New_York")


def _signal() -> Signal:
    moment = datetime(2026, 9, 18, tzinfo=ET)
    return Signal("AMD", "DAILY", True, False, moment, None, 100.0, True, moment, scan_mode="official_daily_confirmation", source_radar="daily", source_timeframe="daily")


class RunCompletenessTests(unittest.TestCase):
    def test_retry_configuration_failure_is_logged(self):
        data_fetcher._YF_RUNTIME_READY = False
        data_fetcher._YF_CACHE_DIR = None

        class Network:
            def __setattr__(self, name, value):
                raise RuntimeError("cannot set retries")

        class Config:
            network = Network()

        fake = type("YF", (), {"config": Config(), "set_tz_cache_location": staticmethod(lambda _path: None)})()
        with patch.dict("sys.modules", {"yfinance": fake}), patch("data_fetcher.tempfile.mkdtemp", return_value="/tmp/gupiao-yf-runtime"):
            with self.assertLogs("data_fetcher", level="WARNING") as logs:
                data_fetcher.configure_yfinance_runtime()
        self.assertTrue(any("retry configuration failed" in line for line in logs.output))
        data_fetcher._YF_RUNTIME_READY = False
        data_fetcher._YF_CACHE_DIR = None

    def test_missing_confirmation_history_is_logged_and_does_not_abort(self):
        import screener

        with patch("screener.fetch_daily_batch", return_value={}):
            with self.assertLogs("screener", level="WARNING") as logs:
                signals, stats, _rows = screener.run_confirmation_screener(["AMD", "MSFT"], [])
        self.assertEqual(signals, [])
        self.assertEqual(stats.failed_count, 2)
        self.assertIn("MSFT skipped: no daily history", "\n".join(logs.output))
        self.assertIn("AMD skipped: no daily history", "\n".join(logs.output))

    def test_incomplete_stock_run_still_emails_then_returns_nonzero(self):
        universe._LAST_LOADS = []
        stats = ScanStats(pool_count=2, fetched_count=1, failed_count=1, final_failed_count=1)
        args = type("Args", (), {"test_email": False, "replay_daily": None, "confirm_daily": True, "dry_run": False})()
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "OUTPUT_DIR", Path(directory)), patch("main.parse_args", return_value=args), patch("main.load_config", return_value={"strict_separation": False, "max_workers": 1, "daily_batch_size": 50, "smtp_host": "smtp.example", "smtp_port": 587, "smtp_user": "sender", "smtp_password": "secret", "to_addrs": ["to@example.test"]}), patch("main.get_universe", return_value=["AMD", "MSFT"]), patch("main.run_confirmation_screener", return_value=([_signal()], stats, [])), patch("main.send_email") as email, patch("main.AlertState") as state_type:
            state = state_type.return_value
            state.filter_new.side_effect = lambda signals: signals
            code = main.main()
            report = (Path(directory) / "dxdx_report.txt").read_text(encoding="utf-8")
        email.assert_called_once()
        state.mark_sent.assert_called_once()
        self.assertIn("结果完整性警告：数据失败数量=1；最终失败数量=1", report)
        self.assertIn("邮件是否发送：是", report)
        self.assertEqual(code, 1)

    def test_complete_etf_run_returns_zero(self):
        stats = ScanStats(pool_count=17, fetched_count=17, failed_count=0, final_failed_count=0)
        self.assertEqual(data_fetcher.log_incomplete_results(logging.getLogger("etf-test"), data_fetcher.incomplete_data_reasons(stats)), 0)
        self.assertIsNone(data_fetcher.completeness_warning(stats))

    def test_workflows_share_yahoo_slot_without_dropping_pending_runs(self):
        root = Path(".github/workflows")
        schedules = {
            "screen.yml": 'cron: "0 8 * * 2-6"',
            "etf_daily_screen.yml": 'cron: "10 8 * * 2-6"',
            "resonance_screen.yml": 'cron: "20 8 * * 2-6"',
            "long_screen.yml": 'cron: "10 23 * * 1-5"',
        }
        for name, cron in schedules.items():
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn(cron, text, name)
            self.assertIn("group: yahoo-market-data", text, name)
            self.assertIn("cancel-in-progress: false", text, name)
            self.assertIn("queue: max", text, name)
        etf = (root / "etf_daily_screen.yml").read_text(encoding="utf-8")
        stock = (root / "screen.yml").read_text(encoding="utf-8")
        self.assertIn("python etf_main.py", etf)
        self.assertIn("python main.py", stock)
        self.assertNotIn("python etf_main.py", stock)


if __name__ == "__main__":
    unittest.main()
