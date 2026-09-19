"""Independent US ETF official-daily DXDX radar entry point."""
from __future__ import annotations

import argparse
import csv
import logging
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from etf_notifier import format_etf_signals_email, sort_etf_signals
from etf_screener import run_etf_confirmation_screener
from etf_universe import ETF_NAMES, ETF_UNIVERSE, etf_name
from main import confirmation_session_times, load_config, require_smtp_config
from notifier import send_email
from screener import DailyDiagnostic, ScanStats, Signal
from state import AlertState


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("etf-daily-dxdx-radar")
OUTPUT_DIR = Path("output")
ETF_STATE_PATH = Path("data/etf_daily_alert_state.json")
CONFIRM_LOOKBACK_SESSIONS = 5

SIGNAL_FIELDS = [
    "symbol", "name", "signal_level", "daily_dxdx", "h4_dxdx",
    "daily_signal_time", "signal_date", "close", "blue_above_yellow",
    "detected_at", "scan_mode", "daily_data_source", "h4_context_source",
    "source_radar", "source_timeframe",
]
DIAGNOSTIC_FIELDS = [
    "symbol", "session", "official_daily_fresh", "official_daily_source",
    "official_history_bars", "open", "high", "low", "close", "DIFF", "DEA",
    "MACD", "N1", "MM1", "CC1", "CC2", "CC3", "DIFL1", "DIFL2", "DIFL3",
    "AAA", "BBB", "CCC", "JJJ_prev", "JJJ", "DXDX", "BLUE_UP", "BLUE_DW",
    "YELLOW_UP", "YELLOW_DW", "BLUE_ABOVE_YELLOW", "final_signal",
]


def _signal_row(signal: Signal) -> dict:
    values = signal.to_dict()
    values["name"] = etf_name(signal.symbol)
    return {field: values[field] for field in SIGNAL_FIELDS}


def _diagnostic_row(diagnostic: DailyDiagnostic) -> dict:
    values = diagnostic.to_dict()
    return {field: values[field] for field in DIAGNOSTIC_FIELDS}


def write_etf_reports(
    signals: list[Signal],
    stats: ScanStats,
    *,
    email_sent: bool,
    detected_at: datetime,
    diagnostics: list[DailyDiagnostic],
    dry_run: bool,
    state_restored: bool,
    state_saved: bool,
    marked_sent: int,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ordered = sort_etf_signals(signals)
    with (OUTPUT_DIR / "etf_daily_signals.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=SIGNAL_FIELDS)
        writer.writeheader()
        writer.writerows(_signal_row(signal) for signal in ordered)
    with (OUTPUT_DIR / "etf_daily_diagnostics.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=DIAGNOSTIC_FIELDS)
        writer.writeheader()
        writer.writerows(_diagnostic_row(item) for item in diagnostics)
    lines = [
        "【美股ETF日线抄底雷达】",
        f"ETF universe count：{len(ETF_UNIVERSE)}",
        f"ETF universe symbols：{','.join(ETF_UNIVERSE)}",
        f"completed sessions：{CONFIRM_LOOKBACK_SESSIONS}",
        f"成功取数数量：{stats.fetched_count}",
        f"数据失败数量：{stats.failed_count}",
        f"信号数量：{len(ordered)}",
        f"信号ETF：{','.join(signal.symbol for signal in ordered)}",
        f"diagnostics rows：{len(diagnostics)}",
        f"batch size：{stats.batch_size}",
        f"batch count：{stats.batch_count}",
        f"fetch_hourly calls：0",
        f"dry-run：{'是' if dry_run else '否'}",
        f"email：{1 if email_sent else 0}",
        f"ETF state restore：{1 if state_restored else 0}",
        f"ETF state save：{1 if state_saved else 0}",
        f"mark_sent：{marked_sent}",
        f"cache warmup seconds：{stats.cache_warmup_seconds:.3f}",
        f"batch download seconds：{stats.batch_download_seconds:.3f}",
        f"indicator compute seconds：{stats.indicator_compute_seconds:.3f}",
        f"total runtime seconds：{stats.total_runtime_seconds:.3f}",
        f"检测时间：{detected_at.isoformat()}",
    ]
    (OUTPUT_DIR / "etf_daily_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_etf_confirmation(
    cfg: dict,
    state: AlertState | None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> tuple[list[Signal], ScanStats, bool]:
    """Run five-session ETF catch-up without touching stock state or email."""
    runtime_started = time.perf_counter()
    started = now or datetime.now().astimezone()
    sessions = [pd.Timestamp(item) for item in confirmation_session_times(started, CONFIRM_LOOKBACK_SESSIONS)]
    all_signals, stats, diagnostics = run_etf_confirmation_screener(
        sessions,
        require_strict_separation=cfg["strict_separation"],
        batch_size=cfg.get("daily_batch_size", 50),
    )
    state_restored = state is not None and not dry_run
    signals = all_signals if dry_run else state.filter_new(all_signals) if state is not None else []
    email_sent = False
    state_saved = False
    marked_sent = 0
    if not dry_run and signals:
        require_smtp_config(cfg)
        subject, body = format_etf_signals_email(signals, scan_time=started)
        send_email(
            cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"],
            cfg["smtp_password"], cfg["to_addrs"], subject, body,
        )
        email_sent = True
        for signal in signals:
            state.mark_sent(signal)
            marked_sent += 1
    if not dry_run and state is not None:
        state.save()
        state_saved = True
    stats.total_runtime_seconds = time.perf_counter() - runtime_started
    write_etf_reports(
        signals,
        stats,
        email_sent=email_sent,
        detected_at=started,
        diagnostics=diagnostics,
        dry_run=dry_run,
        state_restored=state_restored,
        state_saved=state_saved,
        marked_sent=marked_sent,
    )
    log.info(
        "ETF_DAILY_SUMMARY universe=%s sessions=%s fetched=%s failed=%s "
        "signals=%s diagnostics=%s fetch_hourly=0 email=%s state_restore=%s "
        "state_save=%s mark_sent=%s runtime_seconds=%.3f",
        len(ETF_UNIVERSE), len(sessions), stats.fetched_count, stats.failed_count,
        len(signals), len(diagnostics), int(email_sent), int(state_restored),
        int(state_saved), marked_sent, stats.total_runtime_seconds,
    )
    return signals, stats, email_sent


def replay_etf_daily(symbol: str, date: str) -> int:
    """Print one official-daily ETF diagnostic through the production path."""
    normalized = symbol.upper()
    if normalized not in ETF_NAMES:
        raise SystemExit(f"ETF is not in ETF_UNIVERSE: {symbol}")
    session = pd.Timestamp(date, tz="America/New_York")
    from data_fetcher import fetch_daily_batch
    from screener import check_symbol_confirmation
    from etf_universe import is_etf_symbol

    frame = fetch_daily_batch([normalized], period="max", batch_size=50).get(normalized)
    if frame is None or frame.empty:
        raise SystemExit(f"No official daily data for {normalized}")
    _, diagnostics = check_symbol_confirmation(
        normalized,
        [session],
        frame,
        symbol_validator=is_etf_symbol,
        source_radar="etf_daily",
    )
    row = diagnostics[0].to_dict()
    for field in DIAGNOSTIC_FIELDS:
        replay_name = "DIF" if field == "DIFF" else field
        print(f"{replay_name}={row[field]}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US ETF official-daily DXDX radar")
    parser.add_argument("--dry-run", action="store_true", help="scan without email or formal ETF state access")
    parser.add_argument("--replay", nargs=2, metavar=("SYMBOL", "YYYY-MM-DD"), help="print one official-daily ETF diagnostic")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.replay:
        replay_etf_daily(*args.replay)
        return
    cfg = load_config()
    state = None
    if not args.dry_run:
        ETF_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = AlertState(ETF_STATE_PATH)
    run_etf_confirmation(cfg, state, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
