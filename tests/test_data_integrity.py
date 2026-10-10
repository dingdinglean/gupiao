"""Integrity tiers. Email is mocked; these tests must never open SMTP."""
from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import data_fetcher
import etf_main
import integrity
import long_main
import main
import notifier
import resonance_main
import screener
import universe
from etf_screener import run_etf_confirmation_screener
from integrity import DataTier, classify_integrity, failure_notification, prepend_html_banner
from long_screener import LongScanStats, LongSignal, run_long_screener
from resonance.config import ResonanceConfig, ThemeConfig, ThemeGroup
from screener import ScanStats, Signal


ET = ZoneInfo("America/New_York")
SMTP = {
    "smtp_host": "smtp.example",
    "smtp_port": 587,
    "smtp_user": "sender@example.test",
    "smtp_password": "app-password",
    "to_addrs": ["to@example.test"],
    "strict_separation": False,
    "max_workers": 1,
    "daily_batch_size": 50,
    "max_single_retries": 20,
}


def _stock_signal() -> Signal:
    moment = datetime(2026, 9, 18, tzinfo=ET)
    return Signal(
        "AMD", "DAILY", True, False, moment, None, 100.0, True, moment,
        scan_mode="official_daily_confirmation", source_radar="daily", source_timeframe="daily",
    )


def _etf_signal() -> Signal:
    moment = datetime(2026, 9, 18, tzinfo=ET)
    return Signal(
        "SOXX", "DAILY", True, False, moment, None, 100.0, True, moment,
        scan_mode="official_daily_confirmation", source_radar="etf_daily", source_timeframe="daily",
    )


def _long_signal() -> LongSignal:
    moment = datetime(2026, 9, 4, tzinfo=ET)
    return LongSignal("META", "weekly", moment, 123.45, datetime(2026, 9, 8, 17, tzinfo=ET))


def _forbid_smtp():
    return (
        patch("notifier.smtplib.SMTP", side_effect=AssertionError("real smtp")),
        patch("notifier.smtplib.SMTP_SSL", side_effect=AssertionError("real smtp")),
    )


def _activate(test, patches):
    mocks = [item.start() for item in patches]
    for item in patches:
        test.addCleanup(item.stop)
    return mocks


class ClassifyIntegrityTests(unittest.TestCase):
    def test_success_partial_and_critical_thresholds(self):
        success = classify_integrity(pool_count=4, fetched_count=4, missing_symbols=[])
        self.assertEqual(success.tier, DataTier.SUCCESS)
        self.assertEqual(success.missing_count, 0)
        self.assertEqual(integrity.warning_banner(success), "")

        partial = classify_integrity(pool_count=4, fetched_count=2, missing_symbols=["msft", "MSFT", "nvda"])
        self.assertEqual(partial.tier, DataTier.PARTIAL)
        self.assertEqual(partial.missing_symbols, ("MSFT", "NVDA"))
        self.assertEqual(partial.missing_count, 2)
        banner = integrity.warning_banner(partial)
        self.assertIn("【数据完整性：部分缺失】", banner)
        self.assertIn("缺失数量：2", banner)
        self.assertIn("MSFT", banner)
        self.assertIn("NVDA", banner)

        empty_pool = classify_integrity(pool_count=0, fetched_count=0)
        self.assertEqual(empty_pool.tier, DataTier.CRITICAL)
        self.assertIn("扫描池为空", empty_pool.critical_reasons)

        none_fetched = classify_integrity(pool_count=3, fetched_count=0, missing_symbols=["A", "B", "C"])
        self.assertEqual(none_fetched.tier, DataTier.CRITICAL)
        self.assertEqual(none_fetched.missing_count, 3)
        self.assertIn("全部标的取数失败", none_fetched.critical_reasons)

        all_missing = classify_integrity(pool_count=2, fetched_count=2, missing_symbols=["A", "B"])
        self.assertEqual(all_missing.tier, DataTier.CRITICAL)

        emergency = classify_integrity(pool_count=4, fetched_count=4, critical_reasons=["sp500 成分股使用紧急子集"])
        self.assertEqual(emergency.tier, DataTier.CRITICAL)
        self.assertIn("紧急子集", integrity.warning_banner(emergency))

    def test_html_banner_escapes_markup(self):
        item = classify_integrity(pool_count=1, fetched_count=0, critical_reasons=["<script>"])
        html = prepend_html_banner("<p>report</p>", item)
        self.assertTrue(html.startswith("<pre>"))
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<p>report</p>", html)

    def test_failure_mail_is_separate_from_the_signal_subject(self):
        item = classify_integrity(pool_count=2, fetched_count=0, missing_symbols=["AMD"])
        subject, body = failure_notification("美股日线抄底雷达", item)
        self.assertEqual(subject, "【数据完整性失败】美股日线抄底雷达")
        self.assertNotIn("【美股日线抄底雷达】日", subject)
        self.assertIn("本次未发送美股日线抄底雷达的常规信号报告", body)
        self.assertIn("AMD", body)


class BoundedWaitTests(unittest.TestCase):
    def test_smtp_retries_are_bounded(self):
        self.assertEqual(inspect.signature(notifier.send_email).parameters["retries"].default, 1)
        with patch("notifier.smtplib.SMTP", side_effect=TimeoutError("smtp hung")), patch("notifier.time.sleep") as sleep:
            with self.assertRaises(TimeoutError):
                notifier.send_email("smtp.example", 587, "sender@example.test", "app-password", ["to@example.test"], "subject", "body")
        self.assertEqual(sleep.call_count, 1)
        sleep.assert_called_with(2)

    def test_production_single_symbol_retries_default_to_twenty(self):
        self.assertEqual(inspect.signature(screener.run_confirmation_screener).parameters["max_single_retries"].default, 20)
        self.assertEqual(inspect.signature(run_etf_confirmation_screener).parameters["max_single_retries"].default, 20)
        self.assertEqual(inspect.signature(run_long_screener).parameters["max_single_retries"].default, 20)
        self.assertIsNone(inspect.signature(data_fetcher.fetch_daily_batch).parameters["max_single_retries"].default)

    def test_hourly_and_wikipedia_requests_pass_timeouts(self):
        captured = {}

        class Ticker:
            def history(self, **kwargs):
                captured.update(kwargs)
                import pandas as pd
                return pd.DataFrame()

        fake = type("YF", (), {"Ticker": staticmethod(lambda _symbol: Ticker())})()
        data_fetcher._YF_RUNTIME_READY = True
        with patch.dict("sys.modules", {"yfinance": fake}):
            data_fetcher.fetch_hourly("AMD")
        self.assertEqual(captured["timeout"], data_fetcher.YF_SINGLE_TIMEOUT_SECONDS)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"ok"

        seen = {}

        def fake_urlopen(_request, timeout=None):
            seen["timeout"] = timeout
            return Response()

        with patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(universe._fetch_url("https://example.test/universe"), "ok")
        self.assertEqual(seen["timeout"], 30)


class StockTierTests(unittest.TestCase):
    def setUp(self):
        universe._LAST_LOADS = []

    def _run(self, stats, signals, *, dry_run=False, loads=None):
        universe._LAST_LOADS = list(loads or [])
        args = type("Args", (), {"test_email": False, "replay_daily": None, "confirm_daily": True, "dry_run": dry_run})()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        smtp, smtp_ssl = _forbid_smtp()
        started = _activate(self, [
            patch.object(main, "OUTPUT_DIR", Path(directory.name)),
            patch("main.parse_args", return_value=args),
            patch("main.load_config", return_value=dict(SMTP)),
            patch("main.get_universe", return_value=["AMD", "MSFT"]),
            patch("main.run_confirmation_screener", return_value=(signals, stats, [])),
            patch("main.send_email"),
            patch("main.AlertState"),
            smtp,
            smtp_ssl,
        ])
        email = started[5]
        state = started[6].return_value
        state.filter_new.side_effect = lambda found: found
        code = main.main()
        report = (Path(directory.name) / "dxdx_report.txt").read_text(encoding="utf-8")
        return code, email, state, report

    def test_critical_sends_failure_mail_and_fails_the_job(self):
        stats = ScanStats(pool_count=2, fetched_count=0, failed_count=2, failed_symbols=["AMD", "MSFT"])
        code, email, state, report = self._run(stats, [_stock_signal()])
        email.assert_called_once()
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertEqual(subject, "【数据完整性失败】美股日线抄底雷达")
        self.assertIn("【数据完整性：严重失败】", body)
        self.assertIn("缺失数量：2", body)
        self.assertIn("AMD", body)
        self.assertIn("MSFT", body)
        self.assertNotIn("新的 official daily DXDX", body)
        state.mark_sent.assert_not_called()
        state.save.assert_called_once()
        self.assertIn("邮件是否发送：否", report)
        self.assertIn("【数据完整性：严重失败】", report)
        self.assertEqual(code, 1)

    def test_emergency_universe_is_critical_even_when_every_symbol_fetched(self):
        stats = ScanStats(pool_count=2, fetched_count=2)
        load = universe.ConstituentLoad("sp500", ["AMD"], "emergency-fallback", "fetch failed")
        code, email, state, _report = self._run(stats, [_stock_signal()], loads=[load])
        self.assertEqual(code, 1)
        self.assertTrue(email.call_args.args[5].startswith("【数据完整性失败】"))
        state.mark_sent.assert_not_called()

    def test_partial_without_new_signals_keeps_the_no_email_rule(self):
        stats = ScanStats(pool_count=2, fetched_count=1, failed_count=1, failed_symbols=["MSFT"])
        code, email, state, report = self._run(stats, [])
        email.assert_not_called()
        state.mark_sent.assert_not_called()
        self.assertIn("【数据完整性：部分缺失】", report)
        self.assertIn("缺失数量：1", report)
        self.assertIn("MSFT", report)
        self.assertIn("邮件是否发送：否", report)
        self.assertEqual(code, 0)

    def test_dry_run_critical_sends_nothing_and_still_fails(self):
        stats = ScanStats(pool_count=2, fetched_count=0, failed_symbols=["AMD", "MSFT"])
        code, email, state, report = self._run(stats, [_stock_signal()], dry_run=True)
        email.assert_not_called()
        state.save.assert_not_called()
        state.mark_sent.assert_not_called()
        self.assertIn("【数据完整性：严重失败】", report)
        self.assertEqual(code, 1)

    def test_success_email_has_no_banner(self):
        stats = ScanStats(pool_count=2, fetched_count=2)
        code, email, state, report = self._run(stats, [_stock_signal()])
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertTrue(subject.startswith("【美股日线抄底雷达】"))
        self.assertNotIn("数据完整性", body)
        self.assertNotIn("数据完整性", report)
        state.mark_sent.assert_called_once()
        self.assertEqual(code, 0)


class EtfTierTests(unittest.TestCase):
    def _run(self, stats, signals, *, dry_run=False):
        args = type("Args", (), {"dry_run": dry_run, "replay": None})()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        smtp, smtp_ssl = _forbid_smtp()
        started = _activate(self, [
            patch.object(etf_main, "OUTPUT_DIR", Path(directory.name)),
            patch.object(etf_main, "ETF_STATE_PATH", Path(directory.name) / "etf_state.json"),
            patch("etf_main.parse_args", return_value=args),
            patch("etf_main.load_config", return_value=dict(SMTP)),
            patch("etf_main.run_etf_confirmation_screener", return_value=(signals, stats, [])),
            patch("etf_main.send_email"),
            patch("etf_main.AlertState"),
            smtp,
            smtp_ssl,
        ])
        email = started[5]
        state = started[6].return_value
        state.filter_new.side_effect = lambda found: found
        code = etf_main.main()
        report = (Path(directory.name) / "etf_daily_report.txt").read_text(encoding="utf-8")
        return code, email, state, report

    def test_critical_replaces_the_etf_report_email(self):
        stats = ScanStats(pool_count=2, fetched_count=0, failed_symbols=["SOXX", "SMH"])
        code, email, state, report = self._run(stats, [_etf_signal()])
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertEqual(subject, "【数据完整性失败】美股ETF日线抄底雷达")
        self.assertIn("缺失标的：SMH, SOXX", body)
        self.assertNotIn("【美股ETF日线抄底】", subject)
        state.mark_sent.assert_not_called()
        self.assertIn("email：0", report)
        self.assertEqual(code, 1)

    def test_partial_etf_signal_keeps_original_subject_and_adds_banner(self):
        stats = ScanStats(pool_count=2, fetched_count=1, failed_symbols=["SMH"])
        code, email, state, report = self._run(stats, [_etf_signal()])
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertTrue(subject.startswith("【美股ETF日线抄底】"))
        self.assertIn("【数据完整性：部分缺失】", body)
        self.assertIn("缺失数量：1", body)
        self.assertIn("SMH", body)
        self.assertIn("SOXX", body)
        state.mark_sent.assert_called_once()
        self.assertIn("email：1", report)
        self.assertEqual(code, 0)


class LongTierTests(unittest.TestCase):
    def setUp(self):
        universe._LAST_LOADS = []

    def _run(self, stats, signals, *, dry_run=False):
        args = type("Args", (), {"dry_run": dry_run})()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        smtp, smtp_ssl = _forbid_smtp()
        started = _activate(self, [
            patch.object(long_main, "OUTPUT_DIR", Path(directory.name)),
            patch("long_main.parse_args", return_value=args),
            patch("long_main.load_config", return_value=dict(SMTP)),
            patch("long_main.get_universe", return_value=["META", "INTC"]),
            patch("long_main.run_long_screener", return_value=(signals, stats)),
            patch("long_main.send_email"),
            patch("long_main.AlertState"),
            smtp,
            smtp_ssl,
        ])
        email = started[5]
        state = started[6].return_value
        state.filter_new.side_effect = lambda found: found
        code = long_main.main()
        report = (Path(directory.name) / "long_dxdx_report.txt").read_text(encoding="utf-8")
        return code, email, state, report

    def test_critical_long_run_does_not_mark_sent(self):
        stats = LongScanStats(pool_count=2, fetched_count=0, failed_symbols=["META", "INTC"])
        code, email, state, report = self._run(stats, [_long_signal()])
        self.assertEqual(email.call_args.args[5], "【数据完整性失败】美股周/月抄底雷达")
        self.assertIn("缺失数量：2", email.call_args.args[6])
        state.mark_sent.assert_not_called()
        self.assertIn("邮件是否发送：否", report)
        self.assertEqual(code, 1)

    def test_partial_long_signal_uses_the_original_email(self):
        stats = LongScanStats(pool_count=2, fetched_count=1, failed_symbols=["INTC"])
        code, email, state, _report = self._run(stats, [_long_signal()])
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertTrue(subject.startswith("【美股周/月抄底雷达】"))
        self.assertIn("【数据完整性：部分缺失】", body)
        self.assertIn("META", body)
        state.mark_sent.assert_called_once()
        self.assertEqual(code, 0)

    def test_dry_run_does_not_send_the_failure_notice(self):
        stats = LongScanStats(pool_count=2, fetched_count=0, failed_symbols=["META"])
        code, email, state, _report = self._run(stats, [_long_signal()], dry_run=True)
        email.assert_not_called()
        state.save.assert_not_called()
        self.assertEqual(code, 1)


class ResonanceTierTests(unittest.TestCase):
    def _config(self):
        group = ThemeGroup("g", "组", "stock", ("AAA", "BBB"))
        theme = ThemeConfig("t", "主题", (group,))
        return ResonanceConfig(1, 0, 5, 2, 3, 5, 2, (1, 3, 5), {"WATCH": "观察", "NEW": "新"}, {"t": theme})

    def _run(self, daily_map, changes, *, dry_run=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        captured = {}

        def write_outputs(_events, _changes, _observations, report, html_preview, _config):
            captured["report"] = "\n".join(report)
            captured["html"] = html_preview

        args = type("Args", (), {"dry_run": dry_run, "themes": None, "as_of": None, "reconstruct": None})()
        store = MagicMock()
        store.apply.return_value = changes
        smtp, smtp_ssl = _forbid_smtp()
        started = _activate(self, [
            patch.object(resonance_main, "OUTPUT_DIR", Path(directory.name)),
            patch.object(resonance_main, "STATE_PATH", Path(directory.name) / "resonance_state.json"),
            patch("resonance_main.parse_args", return_value=args),
            patch("resonance_main.load_resonance_config", return_value=self._config()),
            patch("resonance_main.fetch_daily_batch", return_value=daily_map),
            patch("resonance_main.TimeframeSignalProvider"),
            patch("resonance_main.ResonanceEngine"),
            patch("resonance_main.ResonanceStateStore", return_value=store),
            patch("resonance_main.format_resonance_email", return_value=("【集体行为】", "原文", "<p>html</p>")),
            patch("resonance_main.write_outputs", side_effect=write_outputs),
            patch("resonance_main.load_config", return_value=dict(SMTP)),
            patch("resonance_main.send_email"),
            smtp,
            smtp_ssl,
        ])
        started[5].return_value.build.return_value = []
        started[6].return_value.evaluate.return_value = []
        code = resonance_main.main()
        return code, started[11], store, captured

    def test_critical_notifies_failure_instead_of_marking_changes(self):
        code, email, store, captured = self._run({}, [MagicMock()])
        self.assertEqual(email.call_args.args[5], "【数据完整性失败】全市场板块 / 主题集体行为引擎")
        self.assertIn("缺失数量：2", email.call_args.args[6])
        store.mark_notified.assert_not_called()
        self.assertIn("邮件发送：否", captured["report"])
        self.assertIn("【数据完整性：严重失败】", captured["report"])
        self.assertEqual(code, 1)

    def test_partial_change_keeps_original_mail_and_escapes_the_banner(self):
        code, email, store, captured = self._run({"AAA": object()}, [MagicMock()])
        subject, body = email.call_args.args[5], email.call_args.args[6]
        self.assertEqual(subject, "【集体行为】")
        self.assertIn("【数据完整性：部分缺失】", body)
        self.assertIn("BBB", body)
        self.assertIn("原文", body)
        self.assertEqual(email.call_args.kwargs["html_body"][:5], "<pre>")
        self.assertIn("BBB", email.call_args.kwargs["html_body"])
        store.mark_notified.assert_called_once()
        self.assertIn("<pre>", captured["html"])
        self.assertEqual(code, 0)

    def test_partial_without_changes_does_not_email(self):
        code, email, store, captured = self._run({"AAA": object()}, [])
        email.assert_not_called()
        store.mark_notified.assert_not_called()
        self.assertIn("缺失数量：1", captured["report"])
        self.assertEqual(code, 0)

    def test_dry_run_critical_does_not_email(self):
        code, email, store, _captured = self._run({}, [MagicMock()], dry_run=True)
        email.assert_not_called()
        store.save.assert_not_called()
        store.mark_notified.assert_not_called()
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
