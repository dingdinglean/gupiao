"""Incomplete fetches stay visible without changing email rules."""
from __future__ import annotations

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

    def test_partial_stock_run_keeps_signal_email_and_exits_zero(self):
        universe._LAST_LOADS = []
        stats = ScanStats(pool_count=2, fetched_count=1, failed_count=1, final_failed_count=1, failed_symbols=["MSFT"])
        args = type("Args", (), {"test_email": False, "replay_daily": None, "confirm_daily": True, "dry_run": False})()
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "OUTPUT_DIR", Path(directory)), patch("main.parse_args", return_value=args), patch("main.load_config", return_value={"strict_separation": False, "max_workers": 1, "daily_batch_size": 50, "max_single_retries": 20, "smtp_host": "smtp.example", "smtp_port": 587, "smtp_user": "sender", "smtp_password": "secret", "to_addrs": ["to@example.test"]}), patch("main.get_universe", return_value=["AMD", "MSFT"]), patch("main.run_confirmation_screener", return_value=([_signal()], stats, [])), patch("main.send_email") as email, patch("main.AlertState") as state_type, patch("notifier.smtplib.SMTP", side_effect=AssertionError("real smtp")), patch("notifier.smtplib.SMTP_SSL", side_effect=AssertionError("real smtp")):
            state = state_type.return_value
            state.filter_new.side_effect = lambda signals: signals
            code = main.main()
            report = (Path(directory) / "dxdx_report.txt").read_text(encoding="utf-8")
        subject, body = email.call_args.args[5], email.call_args.args[6]
        email.assert_called_once()
        state.mark_sent.assert_called_once()
        self.assertTrue(subject.startswith("【美股日线抄底雷达】"))
        self.assertIn("【数据完整性：部分缺失】", body)
        self.assertIn("缺失数量：1", body)
        self.assertIn("MSFT", body)
        self.assertIn("日线抄底", body)
        self.assertIn("【数据完整性：部分缺失】", report)
        self.assertIn("邮件是否发送：是", report)
        self.assertEqual(code, 0)

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
        state_paths = []
        for name in schedules:
            text = (root / name).read_text(encoding="utf-8")
            self.assertIn("contents: read", text, name)
            self.assertNotIn("needs:", text, name)
            self.assertNotIn("git commit", text, name)
            self.assertNotIn("git push", text, name)
            timeouts = [int(item) for item in __import__("re").findall(r"timeout-minutes:\s*(\d+)", text)]
            self.assertTrue(timeouts and all(item > 0 for item in timeouts), name)
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("path:"):
                    continue
                value = stripped.split(":", 1)[1].strip()
                if "alert_state" in value or "resonance_state" in value:
                    state_paths.append(value)
        self.assertEqual(set(state_paths), {
            "alert_state.json",
            "data/etf_daily_alert_state.json",
            "long_alert_state.json",
            "data/resonance_state.json",
        })
        self.assertIn('python-version: "3.11"', (root / "ci.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
