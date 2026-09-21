from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from resonance.cluster_builder import SignalClusterBuilder
from resonance.config import load_resonance_config
from resonance.engine import ResonanceEngine
from resonance.mapper import ThemeMapper
from resonance.models import SignalObservation
from resonance.reconstruction import HistoricalReconstructor
from resonance.signal_provider import TimeframeSignalProvider
from resonance.state_store import ResonanceStateStore


def observation(ticker: str, day: str, timeframe: str = "daily", available: str | None = None) -> SignalObservation:
    signal_date = date.fromisoformat(day)
    return SignalObservation(
        ticker=ticker,
        timeframe=timeframe,
        signal_date=signal_date,
        available_date=date.fromisoformat(available) if available else signal_date,
        close=100.0,
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


if __name__ == "__main__":
    unittest.main()
