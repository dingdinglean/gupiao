"""Daily entry point for the independent US dual-timeframe DXDX pullback radar."""
from __future__ import annotations

import argparse
import csv
import logging
import os
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # Test/minimal environments can still use process env.
    def load_dotenv() -> bool:
        return False

from notifier import format_signals_email, send_email
from screener import ScanStats, Signal, run_screener
from state import AlertState
from universe import get_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dxdx-radar")
OUTPUT_DIR = Path("output")


def load_config() -> dict:
    load_dotenv()
    required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"Missing required SMTP configuration: {', '.join(missing)}")
    return {
        "smtp_host": os.environ["SMTP_HOST"],
        "smtp_port": int(os.environ["SMTP_PORT"]),
        "smtp_user": os.environ["SMTP_USER"],
        "smtp_password": os.environ["SMTP_PASSWORD"],
        "to_addrs": [value.strip() for value in os.environ["EMAIL_TO"].replace(";", ",").split(",") if value.strip()],
        "strict_separation": os.getenv("STRICT_BLUE_ABOVE", "false").lower() == "true",
        "max_workers": int(os.getenv("MAX_WORKERS", "6")),
    }


def write_reports(signals: list[Signal], stats: ScanStats, *, email_sent: bool, detected_at: datetime) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = ["symbol", "signal_level", "daily_dxdx", "h4_dxdx", "daily_signal_time", "h4_signal_time", "close", "blue_above_yellow", "detected_at"]
    with (OUTPUT_DIR / "dxdx_signals.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(signal.to_dict() for signal in signals)
    counts = {level: sum(signal.signal_level == level for signal in signals) for level in ("S", "A", "B")}
    lines = [
        "【美股双周期抄底雷达】",
        f"股票池数量：{stats.pool_count}",
        f"成功取数数量：{stats.fetched_count}",
        f"数据失败数量：{stats.failed_count}",
        f"S级数量：{counts['S']}",
        f"A级数量：{counts['A']}",
        f"B级数量：{counts['B']}",
        f"邮件是否发送：{'是' if email_sent else '否'}",
        f"检测时间：{detected_at.isoformat()}",
    ]
    (OUTPUT_DIR / "dxdx_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_once(cfg: dict, symbols: list[str], state: AlertState, *, dry_run: bool = False) -> tuple[list[Signal], ScanStats, bool]:
    started = datetime.now()
    signals, stats = run_screener(symbols, require_strict_separation=cfg["strict_separation"], max_workers=cfg["max_workers"])
    new_signals = state.filter_new(signals)
    if not new_signals:
        log.info("No new DXDX signals; email skipped.")
        write_reports(signals, stats, email_sent=False, detected_at=started)
        state.save()
        return signals, stats, False
    if dry_run:
        log.info("Dry run: %s new DXDX signal(s); email skipped.", len(new_signals))
        write_reports(new_signals, stats, email_sent=False, detected_at=started)
        return new_signals, stats, False
    subject, body = format_signals_email(new_signals, pool_count=stats.pool_count, scan_time=started)
    send_email(cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"], cfg["smtp_password"], cfg["to_addrs"], subject, body)
    for signal in new_signals:
        state.mark_sent(signal)
    state.save()
    write_reports(new_signals, stats, email_sent=True, detected_at=started)
    return new_signals, stats, True


def send_test_email(cfg: dict) -> None:
    now = datetime.now()
    send_email(cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_user"], cfg["smtp_password"], cfg["to_addrs"], "【美股双周期抄底雷达】Gmail 连通性测试", f"Gmail SMTP 连通性测试成功。\n时间：{now:%Y-%m-%d %H:%M:%S}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US daily/4H DXDX pullback radar")
    parser.add_argument("--dry-run", action="store_true", help="scan without emailing or updating alert state")
    parser.add_argument("--test-email", action="store_true", help="send one Gmail SMTP connectivity test")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()
    if args.test_email:
        send_test_email(config)
        return
    run_once(config, get_universe(), AlertState(), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
