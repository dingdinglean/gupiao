from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resonance.cluster_builder import SignalClusterBuilder
from resonance.config import load_resonance_config
from resonance.engine import ResonanceEngine
from resonance.mapper import ThemeMapper
from resonance.models import ResonanceChange, SignalObservation
from resonance.performance import calculate_event_performance
from resonance.reconstruction import HistoricalReconstructor
from resonance.renderer import format_resonance_email
from resonance.signal_provider import TimeframeSignalProvider
from resonance.state_store import ResonanceStateStore


def observation(
    ticker: str,
    day: str,
    timeframe: str = "daily",
    available: str | None = None,
    trend_filter_pass: bool | None = None,
    bar_date: str | None = None,
) -> SignalObservation:
    signal_date = date.fromisoformat(day)
    return SignalObservation(
        ticker=ticker,
        timeframe=timeframe,
        signal_date=signal_date,
        available_date=date.fromisoformat(available) if available else signal_date,
        close=100.0,
        bar_date=date.fromisoformat(bar_date) if bar_date else signal_date,
        trend_filter_pass=trend_filter_pass,
        weekly_id=f"{signal_date.isocalendar().year}-W{signal_date.isocalendar().week:02d}" if timeframe == "weekly" else "",
    )


class ResonanceEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_resonance_config(theme_ids=["semiconductor", "bitcoin_crypto"])
        cls.engine = ResonanceEngine(cls.config)

    def events(self, observations):
        return self.engine.evaluate(observations)

    def test_single_ticker_is_watch_not_resonance(self):
        events = self.events([observation("AMD", "2026-09-15")])
        self.assertEqual(events[0].state, "WATCH")

    def test_two_independent_subgroups_same_day_make_daily_resonance(self):
        events = self.events([observation("AMD", "2026-09-15"), observation("MU", "2026-09-15")])
        self.assertEqual(events[0].state, "DAILY_RESONANCE")

    def test_one_trading_day_apart_is_one_cluster(self):
        events = self.events([observation("AMD", "2026-09-15"), observation("MU", "2026-09-16")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state, "DAILY_RESONANCE")
        self.assertEqual(events[0].cluster_end_date, date(2026, 9, 16))

    def test_two_trading_days_apart_are_separate_clusters(self):
        events = self.events([observation("AMD", "2026-09-15"), observation("MU", "2026-09-17")])
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item.state == "WATCH" for item in events))

    def test_scattered_month_signals_do_not_form_resonance(self):
        events = self.events([
            observation("AMD", "2026-09-01"), observation("MU", "2026-09-08"),
            observation("AMAT", "2026-09-15"), observation("QCOM", "2026-09-22"),
        ])
        self.assertTrue(all(item.state == "WATCH" for item in events))

    def test_weekly_same_natural_week_forms_resonance(self):
        events = self.events([
            observation("BTC-USD", "2026-09-20", "weekly"),
            observation("MSTR", "2026-09-20", "weekly"),
        ])
        self.assertEqual(events[0].state, "WEEKLY_RESONANCE")

    def test_weekly_one_week_apart_is_not_same_cluster(self):
        events = self.events([
            observation("BTC-USD", "2026-09-13", "weekly"),
            observation("MSTR", "2026-09-20", "weekly"),
        ])
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item.state == "WATCH" for item in events))

    def test_four_spot_etfs_are_four_tickers_but_one_subgroup(self):
        events = self.events([observation(ticker, "2026-09-15") for ticker in ("IBIT", "FBTC", "ARKB", "BITB")])
        self.assertEqual(events[0].tickers, ("ARKB", "BITB", "FBTC", "IBIT"))
        self.assertEqual(len(events[0].subgroups), 1)
        self.assertEqual(events[0].state, "WATCH")

    def test_raw_dxdx_with_failed_trend_still_enters_collective_provider(self):
        index = pd.date_range("2026-01-02", "2026-09-17", freq="B", tz="America/New_York")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}, index=index)
        provider = TimeframeSignalProvider(self.config)

        def daily_indicator(source):
            result = source.copy()
            result["DXDX"] = False
            result["BLUE_ABOVE_YELLOW"] = True
            result.iloc[-1, result.columns.get_loc("DXDX")] = True
            result.iloc[-1, result.columns.get_loc("BLUE_ABOVE_YELLOW")] = False
            return result

        def no_weekly_signal(source):
            result = source.copy()
            result["DXDX"] = False
            return result

        with patch("resonance.signal_provider.add_all_indicators", side_effect=daily_indicator), patch("resonance.signal_provider.compute_macd_divergence", side_effect=no_weekly_signal):
            values = provider.build({"SOXQ": frame}, as_of=date(2026, 9, 18), start=date(2026, 9, 1))
        daily = [item for item in values if item.timeframe == "daily"]
        self.assertEqual(len(daily), 1)
        self.assertFalse(daily[0].trend_filter_pass)

    def test_trend_flags_cannot_change_collective_ticker_count(self):
        values = [
            observation("SOXX", "2026-09-17", trend_filter_pass=True),
            observation("SOXQ", "2026-09-17", trend_filter_pass=False),
            observation("SMH", "2026-09-17", trend_filter_pass=True),
        ]
        event = self.events(values)[0]
        self.assertEqual(event.state, "DAILY_RESONANCE")
        self.assertEqual(event.synchronous_tickers, ("SMH", "SOXQ", "SOXX"))
        self.assertEqual(len(event.synchronous_subgroups), 2)

    def test_btc_anchor_and_four_spot_etfs_form_weekly_collective_behavior(self):
        values = [observation("BTC-USD", "2026-07-12", "weekly")]
        values.extend(observation(ticker, "2026-07-12", "weekly") for ticker in ("IBIT", "FBTC", "ARKB", "BITB"))
        event = self.events(values)[0]
        self.assertEqual(event.state, "WEEKLY_RESONANCE")
        self.assertEqual(event.weekly_id, "2026-W28")
        self.assertEqual(len(event.synchronous_tickers), 5)
        self.assertEqual(len(event.synchronous_subgroups), 2)
        self.assertEqual((event.etf_signaled, event.etf_total), (4, 4))

    def test_mstr_w32_is_follow_up_not_w28_synchronous_member(self):
        values = [
            observation("BTC-USD", "2026-07-12", "weekly"),
            observation("IBIT", "2026-07-12", "weekly"),
            observation("FBTC", "2026-07-12", "weekly"),
            observation("ARKB", "2026-07-12", "weekly"),
            observation("BITB", "2026-07-12", "weekly"),
            observation("MSTR", "2026-08-09", "weekly"),
        ]
        event = next(item for item in self.events(values) if item.weekly_id == "2026-W28")
        self.assertEqual(event.cluster_start_date, date(2026, 7, 12))
        self.assertEqual(event.cluster_end_date, date(2026, 7, 12))
        self.assertNotIn("MSTR", event.synchronous_tickers)
        self.assertNotIn("related_companies", event.synchronous_subgroups)
        self.assertEqual([(item.ticker, item.weekly_id, item.relation) for item in event.follow_up_signals], [("MSTR", "2026-W32", "follow_up")])

    def test_collective_event_survives_without_later_stock_diffusion(self):
        values = [
            observation("BTC-USD", "2026-07-12", "weekly"),
            observation("IBIT", "2026-07-12", "weekly"),
        ]
        event = self.events(values)[0]
        self.assertEqual(event.state, "WEEKLY_RESONANCE")
        self.assertEqual(event.follow_up_signals, ())

    def test_soxq_failed_trend_is_synchronous_semiconductor_member(self):
        event = self.events([
            observation("SOXX", "2026-09-17", trend_filter_pass=True),
            observation("SOXQ", "2026-09-17", trend_filter_pass=False),
            observation("SMH", "2026-09-17", trend_filter_pass=True),
        ])[0]
        soxq = next(item for item in event.evidence if item.ticker == "SOXQ")
        self.assertFalse(soxq.observation.trend_filter_pass)
        self.assertIn("SOXQ", event.synchronous_tickers)

    def test_reconstruction_adds_follow_up_without_rewriting_original_week(self):
        values = [
            observation("BTC-USD", "2026-07-12", "weekly", available="2026-07-13"),
            observation("IBIT", "2026-07-12", "weekly", available="2026-07-13"),
            observation("MSTR", "2026-08-09", "weekly", available="2026-08-10"),
        ]
        events = HistoricalReconstructor(self.engine).reconstruct(
            values, {}, start=date(2026, 7, 1), end=date(2026, 8, 15),
        )
        event = next(item for item in events if item.weekly_id == "2026-W28")
        self.assertEqual((event.cluster_start_date, event.cluster_end_date), (date(2026, 7, 12), date(2026, 7, 12)))
        self.assertEqual(event.synchronous_tickers, ("BTC-USD", "IBIT"))
        self.assertEqual([item.ticker for item in event.follow_up_signals], ["MSTR"])

    def test_new_follow_up_generates_follow_up_state_change(self):
        initial = self.events([
            observation("BTC-USD", "2026-07-12", "weekly"),
            observation("IBIT", "2026-07-12", "weekly"),
        ])[0]
        expanded = self.events([
            observation("BTC-USD", "2026-07-12", "weekly"),
            observation("IBIT", "2026-07-12", "weekly"),
            observation("MSTR", "2026-08-09", "weekly"),
        ])[0]
        with tempfile.TemporaryDirectory() as directory:
            state = ResonanceStateStore(Path(directory) / "state.json")
            state.apply([initial])
            changes = state.apply([expanded])
        self.assertEqual(changes[0].change_type, "FOLLOW_UP")
        self.assertEqual([item.ticker for item in changes[0].added_follow_ups], ["MSTR"])

    def test_user_visible_email_uses_collective_behavior_language(self):
        event = self.events([
            observation("BTC-USD", "2026-07-12", "weekly"),
            observation("IBIT", "2026-07-12", "weekly"),
            observation("MSTR", "2026-08-09", "weekly"),
        ])[0]
        _, text, html = format_resonance_email(
            [ResonanceChange("NEW", "历史重建", event)], [], self.config,
        )
        self.assertIn("比特币与加密资产｜周线集体抄底", text)
        self.assertIn("后续接力", text)
        self.assertNotIn("共振", text + html)

    def test_daily_and_weekly_same_low_region_make_multi_timeframe(self):
        events = self.events([
            observation("BTC-USD", "2026-09-15"), observation("MSTR", "2026-09-15"),
            observation("BTC-USD", "2026-09-20", "weekly"), observation("MSTR", "2026-09-20", "weekly"),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].state, "MULTI_TIMEFRAME_RESONANCE")

    def test_distant_daily_and_weekly_do_not_force_multi_timeframe(self):
        events = self.events([
            observation("BTC-USD", "2026-08-03"), observation("MSTR", "2026-08-03"),
            observation("BTC-USD", "2026-09-20", "weekly"), observation("MSTR", "2026-09-20", "weekly"),
        ])
        self.assertEqual({item.state for item in events}, {"DAILY_RESONANCE", "WEEKLY_RESONANCE"})

    def test_incomplete_week_is_not_emitted(self):
        index = pd.date_range("2026-01-01", "2026-09-16", freq="D", tz="America/New_York")
        frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}, index=index)
        provider = TimeframeSignalProvider(self.config)

        def mark_last(source):
            result = source.copy()
            result["DXDX"] = False
            result.iloc[-1, result.columns.get_loc("DXDX")] = True
            return result

        with patch("resonance.signal_provider.add_all_indicators", side_effect=mark_last), patch("resonance.signal_provider.compute_macd_divergence", side_effect=mark_last):
            values = provider.build({"BTC-USD": frame}, as_of=date(2026, 9, 16), start=date(2026, 9, 1))
        self.assertFalse(any(item.timeframe == "weekly" and item.signal_date >= date(2026, 9, 14) for item in values))

    def test_identical_cluster_second_run_has_no_notification(self):
        event = self.events([observation("AMD", "2026-09-15"), observation("MU", "2026-09-15")])[0]
        with tempfile.TemporaryDirectory() as directory:
            state = ResonanceStateStore(Path(directory) / "state.json")
            self.assertEqual(len(state.apply([event])), 1)
            self.assertEqual(state.apply([event]), [])

    def test_cluster_expansion_generates_enhancement(self):
        initial = self.events([observation("AMD", "2026-09-15"), observation("MU", "2026-09-15")])[0]
        expanded = self.events([
            observation("AMD", "2026-09-15"), observation("MU", "2026-09-15"),
            observation("NVDA", "2026-09-15"), observation("WDC", "2026-09-15"),
        ])[0]
        with tempfile.TemporaryDirectory() as directory:
            state = ResonanceStateStore(Path(directory) / "state.json")
            state.apply([initial])
            changes = state.apply([expanded])
        self.assertEqual(changes[0].change_type, "ENHANCED")
        self.assertEqual(set(changes[0].added_tickers), {"NVDA", "WDC"})

    def test_later_cluster_is_a_new_independent_event(self):
        observations = [
            observation("AMD", "2026-09-01"), observation("MU", "2026-09-01"),
            observation("AMD", "2026-09-15"), observation("MU", "2026-09-15"),
        ]
        events = [item for item in self.events(observations) if item.state != "WATCH"]
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].event_id, events[1].event_id)

    def test_missing_market_data_does_not_crash_provider(self):
        self.assertEqual(TimeframeSignalProvider(self.config).build({}, as_of=date(2026, 9, 22)), [])

    def test_reconstruction_uses_first_knowable_date(self):
        values = [
            observation("AMD", "2026-09-15", available="2026-09-16"),
            observation("MU", "2026-09-15", available="2026-09-16"),
        ]
        events = HistoricalReconstructor(self.engine).reconstruct(
            values, {}, start=date(2026, 9, 1), end=date(2026, 9, 22),
        )
        self.assertEqual(events[0].first_known_date, date(2026, 9, 16))

    def test_semiconductor_etf_methodologies_are_not_all_counted_as_duplicates(self):
        events = self.events([
            observation("SOXX", "2026-09-17"), observation("SOXQ", "2026-09-17"),
            observation("SMH", "2026-09-17"),
        ])
        self.assertEqual(events[0].state, "DAILY_RESONANCE")
        self.assertEqual(len(events[0].subgroups), 2)
        self.assertEqual(events[0].etf_signaled, 3)
        self.assertEqual(events[0].etf_total, 4)

    def test_friday_signal_and_saturday_workflow_keep_distinct_dates(self):
        detected = datetime(2026, 9, 19, 8, 5, tzinfo=timezone(timedelta(hours=8)))
        event = self.engine.evaluate([
            observation("AMD", "2026-09-18", available="2026-09-19"),
            observation("MU", "2026-09-18", available="2026-09-19"),
        ], now=detected)[0]
        self.assertEqual({item.signal_date for item in event.evidence}, {date(2026, 9, 18)})
        self.assertEqual(event.effective_market_date, date(2026, 9, 18))
        self.assertEqual(datetime.fromisoformat(event.detected_at).date(), date(2026, 9, 19))
        _, text, _ = format_resonance_email([ResonanceChange("NEW", "新增", event)], [], self.config)
        self.assertIn("市场正式成立：2026-09-18", text)
        self.assertIn("系统发现：2026-09-19 08:05 +0800", text)

    def test_broad_condition_effective_market_date_is_market_friday(self):
        event = self.events([
            observation("SOXX", "2026-09-17", available="2026-09-18"),
            observation("SMH", "2026-09-17", available="2026-09-18"),
            observation("AMAT", "2026-09-18", available="2026-09-19"),
        ])[0]
        self.assertEqual(event.state, "BROAD_RESONANCE")
        self.assertEqual(event.cluster_end_date, date(2026, 9, 18))
        self.assertEqual(event.effective_market_date, date(2026, 9, 18))
        self.assertEqual(event.first_known_date, date(2026, 9, 19))

    def test_forward_t1_starts_at_next_actual_market_session(self):
        event = self.events([
            observation("AMD", "2026-09-18"),
            observation("MU", "2026-09-18"),
        ])[0]
        index = pd.to_datetime(["2026-09-18", "2026-09-21", "2026-09-22"])
        frame = pd.DataFrame({"close": [100.0, 110.0, 120.0]}, index=index)
        result = calculate_event_performance(event, {"AMD": frame, "MU": frame}, (1,))
        self.assertEqual(result["baseline_date"], "2026-09-18")
        self.assertEqual(result["baseline_field"], "effective_market_date")
        self.assertEqual(result["return_t1"], 10.0)

    def test_weekly_signal_week_bar_end_and_detection_are_separate(self):
        detected = datetime(2026, 7, 13, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        event = self.engine.evaluate([
            observation("BTC-USD", "2026-07-12", "weekly", available="2026-07-13", bar_date="2026-07-12"),
            observation("IBIT", "2026-07-12", "weekly", available="2026-07-13", bar_date="2026-07-10"),
        ], now=detected)[0]
        self.assertEqual(event.weekly_signal_week, "2026-W28")
        self.assertEqual(event.weekly_bar_end, date(2026, 7, 12))
        self.assertEqual(event.effective_market_date, date(2026, 7, 12))
        self.assertEqual(datetime.fromisoformat(event.detected_at).date(), date(2026, 7, 13))

    def test_weekly_provider_preserves_crypto_and_equity_source_bar_dates(self):
        btc_index = pd.date_range("2026-01-01", "2026-07-12", freq="D", tz="America/New_York")
        ibit_index = pd.date_range("2026-01-01", "2026-07-10", freq="B", tz="America/New_York")

        def frame(index):
            return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0}, index=index)

        def no_daily(source):
            result = source.copy()
            result["DXDX"] = False
            result["BLUE_ABOVE_YELLOW"] = False
            return result

        def last_weekly(source):
            result = source.copy()
            result["DXDX"] = False
            result.iloc[-1, result.columns.get_loc("DXDX")] = True
            return result

        provider = TimeframeSignalProvider(self.config)
        with patch("resonance.signal_provider.add_all_indicators", side_effect=no_daily), patch("resonance.signal_provider.compute_macd_divergence", side_effect=last_weekly):
            values = provider.build({"BTC-USD": frame(btc_index), "IBIT": frame(ibit_index)}, as_of=date(2026, 7, 13), start=date(2026, 7, 1))
        weekly = {item.ticker: item for item in values if item.timeframe == "weekly"}
        self.assertEqual(weekly["BTC-USD"].signal_date, date(2026, 7, 12))
        self.assertEqual(weekly["IBIT"].signal_date, date(2026, 7, 12))
        self.assertEqual(weekly["BTC-USD"].bar_date, date(2026, 7, 12))
        self.assertEqual(weekly["IBIT"].bar_date, date(2026, 7, 10))

    def test_reconstruction_detection_is_timezone_independent(self):
        values = [
            observation("AMD", "2026-09-18", available="2026-09-19"),
            observation("MU", "2026-09-18", available="2026-09-19"),
        ]
        first = HistoricalReconstructor(self.engine).reconstruct(values, {}, start=date(2026, 9, 1), end=date(2026, 9, 22))[0]
        second = HistoricalReconstructor(self.engine).reconstruct(values, {}, start=date(2026, 9, 1), end=date(2026, 9, 22))[0]
        self.assertEqual(first.detected_at, second.detected_at)
        self.assertEqual(first.detected_at, "2026-09-19T00:00:00+00:00")
        self.assertEqual(first.effective_market_date, date(2026, 9, 18))

    def test_notified_at_is_only_set_after_successful_notification(self):
        event = self.events([observation("AMD", "2026-09-18"), observation("MU", "2026-09-18")])[0]
        detected = datetime(2026, 9, 19, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        notified = datetime(2026, 9, 19, 8, 1, tzinfo=timezone(timedelta(hours=8)))
        with tempfile.TemporaryDirectory() as directory:
            state = ResonanceStateStore(Path(directory) / "state.json")
            changes = state.apply([event], detected_at=detected)
            self.assertEqual(event.notified_at, "")
            state.mark_notified(changes, notified_at=notified)
        self.assertEqual(event.detected_at, detected.isoformat())
        self.assertEqual(event.notified_at, notified.isoformat())


if __name__ == "__main__":
    unittest.main()
