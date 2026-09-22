from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from data_fetcher import BatchDailyFetchStats, fetch_daily_batch
from main import load_config, require_smtp_config
from notifier import send_email
from resonance.config import ResonanceConfig, load_resonance_config
from resonance.engine import ResonanceEngine
from resonance.mapper import ThemeMapper
from resonance.reconstruction import HistoricalReconstructor
from resonance.renderer import format_resonance_email
from resonance.models import ResonanceChange, SignalObservation
from resonance.signal_provider import TimeframeSignalProvider
from resonance.state_store import ResonanceStateStore


log = logging.getLogger("resonance-engine")
OUTPUT_DIR = Path("output")
STATE_PATH = Path("data/resonance_state.json")
ENGINE_VERSION = "Collective Behavior v1"


def _individual_recommendations(
    observations: list[SignalObservation],
    config: ResonanceConfig,
) -> list[SignalObservation]:
    """Return user-facing Daily v1 stock recommendations only.

    Raw observations remain untouched for collective-behaviour evaluation and
    diagnostics.  Role comes from the existing theme mapper; no second ticker
    universe is maintained here.
    """
    eligible = [
        item for item in observations
        if item.timeframe == "daily"
        and item.dxdx is True
        and item.trend_filter_pass is True
    ]
    stock_keys = {
        (item.ticker, item.observation.timeframe, item.signal_date)
        for item in ThemeMapper(config).map_observations(eligible)
        if item.role == "stock"
    }
    return [
        item for item in eligible
        if (item.ticker, item.timeframe, item.signal_date) in stock_keys
    ]


def _flat_event(event, config) -> dict:
    return {
        "event_id": event.event_id,
        "theme": event.theme_id,
        "display_name": event.display_name,
        "timeframe": event.timeframe,
        "cluster_center_date": event.cluster_center_date.isoformat(),
        "cluster_start_date": event.cluster_start_date.isoformat(),
        "cluster_end_date": event.cluster_end_date.isoformat(),
        "effective_market_date": event.effective_market_date.isoformat(),
        "first_known_date": event.first_known_date.isoformat(),
        "detected_at": event.detected_at,
        "notified_at": event.notified_at,
        "state": event.state,
        "state_display_name": config.status_display_names[event.state],
        "synchronous_tickers": ",".join(event.synchronous_tickers),
        "synchronous_ticker_count": len(event.synchronous_tickers),
        "synchronous_subgroups": ",".join(event.synchronous_subgroups),
        "independent_subgroup_count": len(event.synchronous_subgroups),
        "weekly_id": event.weekly_id,
        "weekly_signal_week": event.weekly_signal_week,
        "weekly_bar_end": event.weekly_bar_end.isoformat() if event.weekly_bar_end else "",
        "aligned_weekly_id": event.aligned_weekly_id,
        "etf_sync": f"{event.etf_signaled}/{event.etf_total}",
        "follow_up_signals": json.dumps([item.to_dict() for item in event.follow_up_signals], ensure_ascii=False),
        **event.performance,
    }


def write_outputs(events, changes, observations, report: list[str], html_preview: str, config) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = []
    for event in events:
        if event.state == "WATCH":
            continue
        row = event.to_dict()
        row["state_display_name"] = config.status_display_names[event.state]
        payload.append(row)
    (OUTPUT_DIR / "resonance_history.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "event_id", "theme", "display_name", "timeframe", "cluster_center_date",
        "cluster_start_date", "cluster_end_date", "effective_market_date", "first_known_date",
        "detected_at", "notified_at", "state", "state_display_name",
        "synchronous_tickers", "synchronous_ticker_count", "synchronous_subgroups",
        "independent_subgroup_count", "weekly_id", "weekly_signal_week", "weekly_bar_end",
        "aligned_weekly_id", "etf_sync", "follow_up_signals",
        "method", "baseline_date", "baseline_field", "participant_count", "return_t1", "return_t3",
        "return_t5", "return_t10", "return_t20",
    ]
    with (OUTPUT_DIR / "resonance_history.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_flat_event(event, config) for event in events if event.state != "WATCH")
    change_payload = []
    for item in changes:
        row = item.to_dict()
        row["event"]["state_display_name"] = config.status_display_names[item.event.state]
        change_payload.append(row)
    (OUTPUT_DIR / "resonance_changes.json").write_text(json.dumps(change_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUTPUT_DIR / "resonance_individual_signals.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = ["ticker", "timeframe", "signal_date", "bar_date", "available_date", "close", "dxdx", "trend_filter_pass", "weekly_id"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(item.to_dict() for item in observations)
    (OUTPUT_DIR / "resonance_email_preview.html").write_text(html_preview, encoding="utf-8")
    (OUTPUT_DIR / "resonance_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def run(
    *,
    dry_run: bool,
    theme_ids: list[str] | None,
    reconstruct: tuple[date, date] | None,
    as_of: date | None = None,
) -> tuple[list, list]:
    started = datetime.now().astimezone()
    cutoff = as_of or started.date()
    config = load_resonance_config(theme_ids=theme_ids)
    stats = BatchDailyFetchStats()
    batch_size = int(os.getenv("RESONANCE_BATCH_SIZE", "50"))
    daily_map = fetch_daily_batch(config.tickers, period="max", batch_size=batch_size, stats=stats)
    provider = TimeframeSignalProvider(config)
    observation_start = reconstruct[0] - timedelta(days=14) if reconstruct else cutoff - timedelta(days=120)
    observations = provider.build(daily_map, as_of=cutoff, start=observation_start)
    engine = ResonanceEngine(config)
    if reconstruct:
        events = HistoricalReconstructor(engine).reconstruct(
            observations, daily_map, start=reconstruct[0], end=reconstruct[1],
        )
        changes = []
    else:
        events = engine.evaluate(observations, now=started)
        state = ResonanceStateStore(STATE_PATH)
        changes = state.apply(events, notify_after=cutoff - timedelta(days=10), detected_at=started)
        if not dry_run:
            state.save()
    recent = [item for item in observations if item.available_date == cutoff]
    individual_recommendations = _individual_recommendations(recent, config)
    preview_changes = changes if not reconstruct else [ResonanceChange("NEW", "历史重建", event) for event in events if event.state != "WATCH"]
    subject, body, html_preview = format_resonance_email(
        preview_changes, individual_recommendations, config, scan_time=started,
    )
    sent = False
    if changes and not dry_run and not reconstruct:
        smtp = load_config()
        require_smtp_config(smtp)
        send_email(
            smtp["smtp_host"], smtp["smtp_port"], smtp["smtp_user"], smtp["smtp_password"],
            smtp["to_addrs"], subject, body, html_body=html_preview,
        )
        sent = True
        state.mark_notified(changes, notified_at=datetime.now().astimezone())
        state.save()
    report = [
        "【全市场板块 / 主题集体行为引擎】",
        f"版本：{ENGINE_VERSION}",
        f"主题数量：{len(config.themes)}",
        f"标的数量：{len(config.tickers)}",
        f"成功取数：{len(daily_map)}",
        f"失败取数：{len(config.tickers) - len(daily_map)}",
        f"日线聚类容差（交易日）：{config.daily_tolerance}",
        f"周线聚类容差（周）：{config.weekly_tolerance}",
        f"集体行为事件：{sum(event.state != 'WATCH' for event in events)}",
        f"通知变化：{len(changes)}",
        f"邮件发送：{'是' if sent else '否'}",
        f"历史逐日重建：{'是' if reconstruct else '否'}",
        f"截至日期：{cutoff.isoformat()}",
    ]
    write_outputs(events, changes, observations, report, html_preview, config)
    return events, changes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="全市场板块/主题集体行为引擎")
    parser.add_argument("--dry-run", action="store_true", help="不发邮件、不保存正式状态")
    parser.add_argument("--themes", help="逗号分隔的内部主题 ID；默认全部")
    parser.add_argument("--as-of", help="历史截至日期 YYYY-MM-DD")
    parser.add_argument("--reconstruct", nargs=2, metavar=("开始日期", "结束日期"), help="逐日历史重建")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    args = parse_args()
    theme_ids = [item.strip() for item in args.themes.split(",") if item.strip()] if args.themes else None
    reconstruction = tuple(date.fromisoformat(item) for item in args.reconstruct) if args.reconstruct else None
    run(
        dry_run=args.dry_run,
        theme_ids=theme_ids,
        reconstruct=reconstruction,
        as_of=date.fromisoformat(args.as_of) if args.as_of else (reconstruction[1] if reconstruction else None),
    )


if __name__ == "__main__":
    main()
