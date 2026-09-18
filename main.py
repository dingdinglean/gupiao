"""Official-daily DXDX pullback radar production entry point."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:  # Test/minimal environments can still use process env.
    def load_dotenv() -> bool:
        return False

from notifier import format_signals_email, send_email
from data_fetcher import configure_yfinance_runtime
from indicators import compute_macd_divergence
from screener import DailyDiagnostic, ScanStats, Signal, run_confirmation_screener
from session_calendar import nyse_session, previous_nyse_trading_day
from state import AlertState
from universe import get_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dxdx-radar")
OUTPUT_DIR = Path("output")
CONFIRM_LOOKBACK_SESSIONS = 5


def load_config(*, require_smtp: bool = False) -> dict:
    load_dotenv()
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
    missing = [name for name in required if not os.getenv(name)]
    if require_smtp and missing:
        raise SystemExit(f"Missing required SMTP configuration: {', '.join(missing)}")
    port_raw = os.getenv("SMTP_PORT") or "0"
    try:
        smtp_port = int(port_raw)
    except ValueError:
        smtp_port = 0
    return {
        "smtp_host": os.getenv("SMTP_HOST"),
        "smtp_port": smtp_port,
        "smtp_user": os.getenv("SMTP_USER"),
        "smtp_password": os.getenv("SMTP_PASSWORD"),
        "to_addrs": [value.strip() for value in os.getenv("EMAIL_TO", "").replace(";", ",").split(",") if value.strip()],
        "strict_separation": os.getenv("STRICT_BLUE_ABOVE", "false").lower() == "true",
        "max_workers": int(os.getenv("MAX_WORKERS", "6")),
        "daily_batch_size": int(os.getenv("DAILY_BATCH_SIZE", "50")),
    }


def require_smtp_config(cfg: dict) -> None:
    missing = [name for name, value in {"SMTP_HOST": cfg["smtp_host"], "SMTP_PORT": cfg["smtp_port"], "SMTP_USER": cfg["smtp_user"], "SMTP_PASSWORD": cfg["smtp_password"], "EMAIL_TO": cfg["to_addrs"]}.items() if not value]
    if missing:
        raise RuntimeError("Missing required SMTP configuration: " + ", ".join(missing))


def write_reports(
    signals: list[Signal],
    stats: ScanStats,
    *,
    email_sent: bool,
    detected_at: datetime,
    daily_diagnostics: list[DailyDiagnostic] | None = None,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "signal_level", "daily_dxdx", "h4_dxdx", "daily_signal_time", "h4_signal_time", "signal_time", "signal_date", "close", "blue_above_yellow", "detected_at", "scan_mode", "daily_data_source", "h4_context_source", "source_radar", "source_timeframe"]
    with (OUTPUT_DIR / "dxdx_signals.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(signal.to_dict() for signal in signals)
    daily_fields = ["symbol", "session", "scan_mode", "official_daily_fresh", "official_daily_source", "official_history_bars", "open", "high", "low", "close", "DIFF", "DEA", "MACD", "N1", "MM1", "CC1", "CC2", "CC3", "DIFL1", "DIFL2", "DIFL3", "AAA", "BBB", "CCC", "JJJ_prev", "JJJ", "DXDX", "BLUE_UP", "BLUE_DW", "YELLOW_UP", "YELLOW_DW", "BLUE_ABOVE_YELLOW", "final_signal", "matched_h4_same_session", "final_level"]
    diagnostics_started = time.perf_counter()
    with (OUTPUT_DIR / "daily_dxdx_diagnostics.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=daily_fields); writer.writeheader()
        writer.writerows(item.to_dict() for item in daily_diagnostics or [])
    stats.diagnostics_write_seconds = time.perf_counter() - diagnostics_started
    daily_count = sum(signal.signal_level == "DAILY" for signal in signals)
    lines = [
        "【美股日线抄底雷达】",
        f"股票池数量：{stats.pool_count}",
        f"成功取数数量：{stats.fetched_count}",
        f"数据失败数量：{stats.failed_count}",
        f"日线DXDX信号数量：{daily_count}",
        f"批量数量：{stats.batch_count}",
        f"缓存预热秒数：{stats.cache_warmup_seconds:.3f}",
        f"批量取数秒数：{stats.batch_download_seconds:.3f}",
        f"小批重试数量：{stats.mini_batch_retry_count}",
        f"小批重试股票数：{stats.mini_batch_retry_symbols}",
        f"小批重试成功数：{stats.mini_batch_success_count}",
        f"单股重试数量：{stats.single_retry_count}",
        f"单股重试成功数：{stats.single_retry_success_count}",
        f"最终失败数量：{stats.final_failed_count}",
        f"标准化秒数：{stats.normalization_seconds:.3f}",
        f"指标计算秒数：{stats.indicator_compute_seconds:.3f}",
        f"诊断写入秒数：{stats.diagnostics_write_seconds:.3f}",
        f"邮件是否发送：{'是' if email_sent else '否'}",
        f"检测时间：{detected_at.isoformat()}",
    ]
    (OUTPUT_DIR / "dxdx_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_once(cfg: dict, symbols: list[str], state: AlertState, *, dry_run: bool = False) -> tuple[list[Signal], ScanStats, bool]:
    """Backward-compatible production entry point, now daily confirmation only."""
    return run_confirmation(cfg, symbols, state, dry_run=dry_run)


def send_test_email(cfg: dict) -> None:
    require_smtp_config(cfg)
    now = datetime.now()
    send_email(cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"], cfg["smtp_password"], cfg["to_addrs"], "【美股双周期抄底雷达】Gmail 连通性测试", f"Gmail SMTP 连通性测试成功。\n时间：{now:%Y-%m-%d %H:%M:%S}\n")


def confirmation_session_times(now: datetime, count: int = CONFIRM_LOOKBACK_SESSIONS) -> list[datetime]:
    moment = pd.Timestamp(now)
    moment = moment.tz_convert("America/New_York") if moment.tzinfo else moment.tz_localize("America/New_York")
    day = moment.normalize()
    bounds = nyse_session(day)
    if bounds is None or moment < bounds.close + pd.Timedelta(minutes=20):
        day = previous_nyse_trading_day(day - pd.Timedelta(days=1))
    result = []
    for _ in range(count):
        result.append((nyse_session(day).close + pd.Timedelta(minutes=20)).to_pydatetime())
        day = previous_nyse_trading_day(day - pd.Timedelta(days=1))
    return result


def run_confirmation(cfg: dict, symbols: list[str], state: AlertState, *, dry_run: bool = False, now: datetime | None = None) -> tuple[list[Signal], ScanStats, bool]:
    """Official-1D catch-up for five completed XNYS sessions.

    Only Yahoo's official 1D bars participate in signal production.
    """
    runtime_started = time.perf_counter()
    sessions = [pd.Timestamp(item) for item in confirmation_session_times(now or datetime.now().astimezone())]
    all_signals, total, daily_rows = run_confirmation_screener(
        symbols, sessions,
        require_strict_separation=cfg["strict_separation"],
        max_workers=cfg["max_workers"],
        batch_size=cfg.get("daily_batch_size", 50),
    )
    new_signals = state.filter_new(all_signals)
    started = now or datetime.now()
    if dry_run:
        write_reports(new_signals, total, email_sent=False, detected_at=started, daily_diagnostics=daily_rows)
        total.total_runtime_seconds = time.perf_counter() - runtime_started
        _log_confirmation_timing(total)
        return new_signals, total, False
    if not new_signals:
        write_reports([], total, email_sent=False, detected_at=started, daily_diagnostics=daily_rows)
        state.save()
        total.total_runtime_seconds = time.perf_counter() - runtime_started
        _log_confirmation_timing(total)
        return [], total, False
    require_smtp_config(cfg)
    subject, body = format_signals_email(new_signals, pool_count=total.pool_count, scan_time=started)
    send_email(cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"], cfg["smtp_password"], cfg["to_addrs"], subject, body)
    for signal in new_signals:
        state.mark_sent(signal)
    state.save()
    write_reports(new_signals, total, email_sent=True, detected_at=started, daily_diagnostics=daily_rows)
    total.total_runtime_seconds = time.perf_counter() - runtime_started
    _log_confirmation_timing(total)
    return new_signals, total, True


def _log_confirmation_timing(stats: ScanStats) -> None:
    log.info(
        "daily confirmation timing universe_count=%s batch_size=%s batch_count=%s "
        "cache_warmup_seconds=%.3f batch_download_seconds=%.3f "
        "mini_batch_retry_seconds=%.3f single_retry_seconds=%.3f "
        "normalization_seconds=%.3f indicator_compute_seconds=%.3f "
        "diagnostics_write_seconds=%.3f total_runtime_seconds=%.3f "
        "batch_success_count=%s mini_batch_retry_count=%s mini_batch_retry_symbols=%s "
        "mini_batch_success_count=%s single_retry_count=%s single_retry_success_count=%s "
        "final_failed_count=%s",
        stats.universe_count, stats.batch_size, stats.batch_count,
        stats.cache_warmup_seconds, stats.batch_download_seconds,
        stats.mini_batch_retry_seconds, stats.single_retry_seconds,
        stats.normalization_seconds, stats.indicator_compute_seconds,
        stats.diagnostics_write_seconds, stats.total_runtime_seconds,
        stats.batch_success_count, stats.mini_batch_retry_count,
        stats.mini_batch_retry_symbols, stats.mini_batch_success_count,
        stats.single_retry_count, stats.single_retry_success_count,
        stats.final_failed_count,
    )


def replay_daily(symbol: str, date: str) -> int:
    """Read-only Yahoo adjusted/raw history matrix for deferred regressions."""
    import yfinance as yf
    configure_yfinance_runtime()
    target = pd.Timestamp(date, tz="America/New_York").normalize()
    rows = []
    for adjusted in (True, False):
        for period in ("3y", "5y", "max"):
            daily = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=adjusted, prepost=False).rename(columns=str.lower)
            daily = daily.dropna(subset=["open", "high", "low", "close"])
            match = daily.loc[pd.DatetimeIndex(daily.index).tz_convert("America/New_York").normalize() == target] if not daily.empty else daily
            if match.empty:
                rows.append({"symbol": symbol, "date": date, "auto_adjust": adjusted, "period": period, "status": "pending_official_daily_data"})
            else:
                row = compute_macd_divergence(daily).loc[match.index[-1]]
                rows.append({"symbol": symbol, "date": date, "auto_adjust": adjusted, "period": period, "status": "ok", **{key: row.get(key) for key in ("open", "high", "low", "close", "DIF", "DEA", "MACD_bar", "N1", "MM1", "CC1", "CC2", "CC3", "DIFL1", "DIFL2", "DIFL3", "AAA", "BBB", "CCC", "JJJ", "DXDX")}})
    for row in rows: print(row)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US official-daily DXDX pullback radar")
    parser.add_argument("--dry-run", action="store_true", help="scan without emailing or updating alert state")
    parser.add_argument("--test-email", action="store_true", help="send one Gmail SMTP connectivity test")
    parser.add_argument("--confirm-daily", action="store_true", help="confirm official daily DXDX over five XNYS sessions")
    parser.add_argument("--replay-daily", nargs=2, metavar=("SYMBOL", "YYYY-MM-DD"), help="read-only official Yahoo daily matrix")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(require_smtp=args.test_email)
    if args.test_email:
        send_test_email(config)
        return
    if args.replay_daily:
        replay_daily(*args.replay_daily)
        return
    if args.confirm_daily:
        run_confirmation(config, get_universe(), AlertState(), dry_run=args.dry_run)
        return
    run_confirmation(config, get_universe(), AlertState(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
