"""Entry point for the independent US weekly/monthly DXDX radar."""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> bool:
        return False

from long_screener import LongScanStats, LongSignal, run_long_screener
from notifier import format_long_signals_email, send_email
from state import AlertState
from universe import get_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("long-dxdx-radar")
OUTPUT_DIR = Path("output")
STATE_PATH = Path("long_alert_state.json")
STATE_TTL_DAYS = 400


def load_config() -> dict:
    load_dotenv()
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
        "max_workers": int(os.getenv("MAX_WORKERS", "6")),
    }


def require_smtp_config(cfg: dict) -> None:
    values = {"SMTP_HOST": cfg["smtp_host"], "SMTP_PORT": cfg["smtp_port"], "SMTP_USER": cfg["smtp_user"], "SMTP_PASSWORD": cfg["smtp_password"], "EMAIL_TO": cfg["to_addrs"]}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("Missing required SMTP configuration: " + ", ".join(missing))


def write_reports(signals: list[LongSignal], stats: LongScanStats, *, email_sent: bool, detected_at: datetime) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "timeframe", "signal_time", "close", "detected_at"]
    with (OUTPUT_DIR / "long_dxdx_signals.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(signal.to_dict() for signal in signals)
    monthly_count = sum(signal.timeframe == "monthly" for signal in signals)
    weekly_count = sum(signal.timeframe == "weekly" for signal in signals)
    lines = [
        "【美股周/月抄底雷达】",
        f"股票池数量：{stats.pool_count}",
        f"成功取数数量：{stats.fetched_count}",
        f"数据失败数量：{stats.failed_count}",
        f"历史不足数量：{stats.insufficient_count}",
        f"周线 DXDX 数量：{weekly_count}",
        f"月线 DXDX 数量：{monthly_count}",
        f"邮件是否发送：{'是' if email_sent else '否'}",
        f"检测时间：{detected_at.isoformat()}",
    ]
    (OUTPUT_DIR / "long_dxdx_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_once(cfg: dict, symbols: list[str], state: AlertState, *, dry_run: bool = False) -> tuple[list[LongSignal], LongScanStats, bool]:
    started = datetime.now()
    signals, stats = run_long_screener(symbols, max_workers=cfg["max_workers"])
    new_signals = state.filter_new(signals)
    if dry_run:
        log.info("Dry run: %s new weekly/monthly DXDX signal(s); email and state update skipped.", len(new_signals))
        write_reports(signals, stats, email_sent=False, detected_at=started)
        return signals, stats, False
    if not new_signals:
        log.info("No new weekly/monthly DXDX signals; email skipped.")
        write_reports(signals, stats, email_sent=False, detected_at=started)
        state.save()
        return signals, stats, False
    require_smtp_config(cfg)
    subject, body = format_long_signals_email(new_signals, pool_count=stats.pool_count, scan_time=started)
    send_email(cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"], cfg["smtp_password"], cfg["to_addrs"], subject, body)
    for signal in new_signals:
        state.mark_sent(signal)
    state.save()
    write_reports(signals, stats, email_sent=True, detected_at=started)
    return signals, stats, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US weekly/monthly DXDX pullback radar")
    parser.add_argument("--dry-run", action="store_true", help="scan without emailing or updating alert state")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_once(load_config(), get_universe(), AlertState(STATE_PATH, ttl_days=STATE_TTL_DAYS), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
