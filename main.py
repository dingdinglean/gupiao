"""Main entry point.

Usage:
    python main.py --once                 # scan once, exit
    python main.py --once --dry-run       # scan once, print results, DON'T email
    python main.py                        # loop forever, scan every 30 min
    python main.py --interval-min 15      # custom interval
    python main.py --symbols KO AAPL TSLA # ad-hoc symbol list

First time setup:
    1. cp config.example.env .env
    2. fill in SMTP_PASSWORD with your QQ Mail authorization code
    3. python main.py --once --dry-run    # sanity test
    4. python main.py --once              # send a real email
    5. python main.py                     # leave it running
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime

from dotenv import load_dotenv

from notifier import format_hits_email, send_email
from screener import run_screener
from state import AlertState
from universe import get_nasdaq100, get_sp500, get_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def load_config() -> dict:
    load_dotenv()
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {missing}. Copy config.example.env to .env and fill it in.")

    return {
        "smtp_host": os.environ["SMTP_HOST"],
        "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
        "smtp_user": os.environ["SMTP_USER"],
        "smtp_password": os.environ["SMTP_PASSWORD"],
        "from_addr":  os.environ.get("EMAIL_FROM", os.environ["SMTP_USER"]),
        "to_addrs":   [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()],
        "strict_separation": os.environ.get("STRICT_BLUE_ABOVE", "false").lower() == "true",
        "daily_lookback":    int(os.environ.get("DAILY_LOOKBACK_BARS", "3")),
        "h4_lookback":       int(os.environ.get("H4_LOOKBACK_BARS", "2")),
        "max_workers":       int(os.environ.get("MAX_WORKERS", "8")),
    }


def run_once(cfg: dict, symbols: list[str], state: AlertState, dry_run: bool = False) -> None:
    log.info(f"==== scan start ({len(symbols)} symbols) ====")
    started = datetime.now()
    hits = run_screener(
        symbols,
        h4_lookback_bars=cfg["h4_lookback"],
        daily_lookback_bars=cfg["daily_lookback"],
        require_strict_separation=cfg["strict_separation"],
        max_workers=cfg["max_workers"],
    )
    elapsed = (datetime.now() - started).total_seconds()
    log.info(f"==== scan done in {elapsed:.0f}s, {len(hits)} total hits ====")

    new_hits = state.filter_new(hits)
    log.info(f"{len(new_hits)} new hits after dedup")

    if not new_hits:
        return

    if dry_run:
        log.info("DRY RUN - would have sent email with these hits:")
        for h in new_hits:
            log.info(f"  {h.to_text()}")
        return

    subject, text, html = format_hits_email(new_hits, scan_time=started)
    try:
        send_email(
            cfg["smtp_host"], cfg["smtp_port"],
            cfg["smtp_user"], cfg["smtp_password"],
            cfg["from_addr"], cfg["to_addrs"],
            subject, text, html,
        )
        for h in new_hits:
            state.mark_sent(h)
        state.save()
    except Exception as e:
        log.error(f"send_email failed: {e}", exc_info=True)


def parse_args():
    p = argparse.ArgumentParser(description="US-stock screener: blue>yellow + daily/4H 抄底 resonance")
    p.add_argument("--once", action="store_true", help="Run a single scan and exit")
    p.add_argument("--dry-run", action="store_true", help="Print results, do not send email")
    p.add_argument("--symbols", nargs="*", help="Ad-hoc symbols to scan (override universe)")
    p.add_argument("--universe", choices=["all", "sp500", "ndx"], default="all",
                   help="Universe (default: S&P 500 + NASDAQ 100)")
    p.add_argument("--interval-min", type=int, default=30,
                   help="Loop interval in minutes (default: 30)")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = load_config()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
        log.info(f"Using ad-hoc list of {len(symbols)} symbols")
    elif args.universe == "sp500":
        symbols = get_sp500()
    elif args.universe == "ndx":
        symbols = get_nasdaq100()
    else:
        symbols = get_universe()
    log.info(f"Universe size: {len(symbols)}")

    state = AlertState()

    if args.once:
        run_once(cfg, symbols, state, dry_run=args.dry_run)
        return

    log.info(f"Loop mode: scanning every {args.interval_min} minutes. Ctrl-C to stop.")
    while True:
        try:
            run_once(cfg, symbols, state, dry_run=args.dry_run)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as e:
            log.error(f"Scan crashed: {e}", exc_info=True)
        time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    main()
