from __future__ import annotations

import html
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .config import ResonanceConfig
from .models import Evidence, ResonanceChange, ResonanceEvent, SignalObservation


ROLE_NAMES = {"anchor": "锚定资产", "etf": "ETF", "stock": "个股"}
DISPLAY_TIMEZONE = timezone(timedelta(hours=8))


def _date_range(event: ResonanceEvent) -> str:
    start = event.cluster_start_date.isoformat()
    end = event.cluster_end_date.isoformat()
    return start if start == end else f"{start}～{end}"


def _group_evidence(event: ResonanceEvent) -> list[tuple[str, list[Evidence]]]:
    grouped: dict[str, list[Evidence]] = defaultdict(list)
    names: dict[str, str] = {}
    for item in event.evidence:
        grouped[item.subgroup_id].append(item)
        names[item.subgroup_id] = item.subgroup_display_name
    role_order = {"anchor": 0, "etf": 1, "stock": 2}
    result = [(names[key], sorted(values, key=lambda item: (item.signal_date, item.ticker, item.observation.timeframe))) for key, values in grouped.items()]
    return sorted(result, key=lambda pair: (role_order.get(pair[1][0].role, 9), pair[0]))


def _breadth(event: ResonanceEvent) -> str:
    return "广泛集体行为" if len(event.subgroups) >= 3 else "基础层集体行为"


def _detected_label(event: ResonanceEvent) -> str:
    if not event.detected_at:
        return ""
    try:
        value = datetime.fromisoformat(event.detected_at).astimezone(DISPLAY_TIMEZONE)
        return value.strftime("%Y-%m-%d %H:%M %z")
    except ValueError:
        return event.detected_at


def _event_text(change: ResonanceChange, config: ResonanceConfig) -> list[str]:
    event = change.event
    state = config.status_display_names[event.state]
    lines = [f"🔥 {event.display_name}｜{state}"]
    if event.timeframe == "weekly":
        lines.append(f"集体行为周：{event.weekly_id}")
        if event.weekly_bar_end:
            lines.append(f"周线K线结束：{event.weekly_bar_end.isoformat()}")
    else:
        lines.append(f"集体行为日期：{_date_range(event)}")
    lines.append(f"市场正式成立：{event.effective_market_date.isoformat()}")
    if event.detected_at:
        lines.append(f"系统发现：{_detected_label(event)}")
    if event.aligned_weekly_id:
        lines.append(f"对齐周线：{event.aligned_weekly_id}")
    for name, items in _group_evidence(event):
        values = []
        for item in items:
            period = "周线" if item.observation.timeframe == "weekly" else "日线"
            values.append(f"{item.ticker} ✅ {item.signal_date:%m/%d} {period}")
        lines.extend([name, *values])
    if event.etf_total:
        lines.append(f"ETF同步：{event.etf_signaled}/{event.etf_total}")
        if event.etf_signaled == event.etf_total:
            lines.append("ETF全体同步")
    layers = " + ".join(name for name, _ in _group_evidence(event))
    lines.extend([f"独立子组：{len(event.subgroups)}", f"参与层级：{layers}", f"当前广度：{_breadth(event)}"])
    if event.follow_up_signals:
        lines.append("后续接力：")
        lines.extend(
            f"{item.ticker}｜{item.weekly_id or item.signal_date.isoformat()}｜{item.subgroup_display_name}"
            for item in event.follow_up_signals
        )
    lines.extend([f"本次状态：{state}", f"变化：{change.change_display_name}", ""])
    return lines


def _event_html(change: ResonanceChange, config: ResonanceConfig) -> str:
    event = change.event
    state = config.status_display_names[event.state]
    timing = f"集体行为周：{event.weekly_id}" if event.timeframe == "weekly" else f"集体行为日期：{_date_range(event)}"
    if event.timeframe == "weekly" and event.weekly_bar_end:
        timing += f"<br>周线K线结束：{event.weekly_bar_end.isoformat()}"
    timing += f"<br>市场正式成立：{event.effective_market_date.isoformat()}"
    if event.detected_at:
        timing += f"<br>系统发现：{html.escape(_detected_label(event))}"
    if event.aligned_weekly_id:
        timing += f"<br>对齐周线：{html.escape(event.aligned_weekly_id)}"
    sections = []
    for name, items in _group_evidence(event):
        rows = "".join(
            f"<tr><td><strong>{html.escape(item.ticker)}</strong></td><td>✅ {item.signal_date:%m/%d}</td><td>{'周线' if item.observation.timeframe == 'weekly' else '日线'}</td></tr>"
            for item in items
        )
        sections.append(f"<h3>{html.escape(name)}</h3><table>{rows}</table>")
    etf = f"<span>ETF同步：{event.etf_signaled}/{event.etf_total}</span>" if event.etf_total else ""
    full = "<span class='pill'>ETF全体同步</span>" if event.etf_total and event.etf_signaled == event.etf_total else ""
    layers = " + ".join(name for name, _ in _group_evidence(event))
    follow_ups = ""
    if event.follow_up_signals:
        rows = "".join(
            f"<tr><td><strong>{html.escape(item.ticker)}</strong></td><td>{html.escape(item.weekly_id or item.signal_date.isoformat())}</td><td>{html.escape(item.subgroup_display_name)}</td></tr>"
            for item in event.follow_up_signals
        )
        follow_ups = f"<h3>后续接力 / 扩散</h3><table>{rows}</table>"
    return (
        "<section class='card'>"
        f"<h2>🔥 {html.escape(event.display_name)}｜{html.escape(state)}</h2>"
        f"<p>{timing}</p>{''.join(sections)}{follow_ups}"
        f"<div class='facts'>{etf}<span>独立子组：{len(event.subgroups)}</span><span>参与层级：{html.escape(layers)}</span><span>当前广度：{_breadth(event)}</span><span>变化：{html.escape(change.change_display_name)}</span>{full}</div>"
        "</section>"
    )


def format_resonance_email(
    changes: list[ResonanceChange],
    individual_signals: list[SignalObservation],
    config: ResonanceConfig,
    *,
    scan_time: datetime | None = None,
) -> tuple[str, str, str]:
    scan_time = scan_time or datetime.now()
    subject = f"【今日板块/主题集体行为】{len(changes)} 个变化｜{scan_time:%Y-%m-%d}"
    text_lines = ["【今日板块 / 主题集体行为】", ""]
    if changes:
        for change in changes:
            text_lines.extend(_event_text(change, config))
    else:
        text_lines.extend(["今日无新增板块/主题集体行为。", ""])
    text_lines.extend(["【今日个体抄底信号】", ""])
    if individual_signals:
        for item in sorted(individual_signals, key=lambda value: (value.timeframe, value.signal_date, value.ticker)):
            timeframe = "周线" if item.timeframe == "weekly" else "日线"
            trend = "；趋势附加信息：蓝梯>黄梯" if item.trend_filter_pass else ("；趋势附加信息：未通过蓝黄过滤" if item.trend_filter_pass is False else "")
            text_lines.append(f"{item.ticker}｜{timeframe} DXDX｜{item.signal_date.isoformat()}{trend}")
    else:
        text_lines.append("今日无新增个体抄底信号。")
    text_lines.extend(["", "仅供研究参考，不构成投资建议。"])

    cards = "".join(_event_html(change, config) for change in changes) if changes else "<section class='empty'>今日无新增板块/主题集体行为。</section>"
    individual_rows = "".join(
        f"<tr><td><strong>{html.escape(item.ticker)}</strong></td><td>{'周线' if item.timeframe == 'weekly' else '日线'}</td><td>{item.signal_date.isoformat()}</td><td>{'通过蓝黄趋势' if item.trend_filter_pass else ('未通过蓝黄趋势' if item.trend_filter_pass is False else '—')}</td></tr>"
        for item in sorted(individual_signals, key=lambda value: (value.timeframe, value.signal_date, value.ticker))
    ) or "<tr><td colspan='4'>今日无新增个体抄底信号。</td></tr>"
    html_body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
body{{margin:0;background:#f4f6f8;color:#17202a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif}}main{{max-width:720px;margin:auto;padding:18px}}h1{{font-size:24px}}h2{{font-size:20px;margin-top:0}}h3{{font-size:15px;color:#566573;margin-bottom:5px}}.card,.empty{{background:white;border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 2px 10px #00000010}}table{{border-collapse:collapse;width:100%}}td,th{{padding:8px 6px;border-bottom:1px solid #edf0f2;text-align:left;font-size:14px}}.facts{{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}}.facts span,.pill{{background:#eef3f8;border-radius:999px;padding:6px 10px;font-size:13px}}.pill{{background:#fff1d6;color:#8a5500}}.muted{{color:#718096;font-size:13px}}
</style></head><body><main><h1>今日板块 / 主题集体行为</h1>{cards}<section class="card"><h2>今日个体抄底信号</h2><table><tr><th>标的</th><th>周期</th><th>信号日期</th><th>趋势附加信息</th></tr>{individual_rows}</table></section><p class="muted">扫描时间：{scan_time:%Y-%m-%d %H:%M:%S}<br>仅供研究参考，不构成投资建议。</p></main></body></html>"""
    return subject, "\n".join(text_lines) + "\n", html_body
