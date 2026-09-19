"""Chinese email formatting for the independent ETF Daily radar."""
from __future__ import annotations

from datetime import datetime

from etf_universe import ETF_UNIVERSE, etf_name


_ETF_ORDER = {symbol: index for index, symbol in enumerate(ETF_UNIVERSE)}


def sort_etf_signals(signals: list) -> list:
    return sorted(
        signals,
        key=lambda signal: (
            _ETF_ORDER[signal.symbol],
            signal.daily_signal_time or datetime.min,
        ),
    )


def format_etf_signals_email(signals: list, *, scan_time: datetime | None = None) -> tuple[str, str]:
    """Format ETF signals separately from every individual-stock message."""
    scan_time = scan_time or datetime.now()
    ordered = sort_etf_signals(signals)
    lead = ordered[0].symbol if ordered else "ETF"
    subject = f"【美股ETF日线抄底】{lead} 等 {len(ordered)} 只"
    lines = ["【美股ETF日线抄底雷达】", ""]
    for signal in ordered:
        lines.extend(
            [
                f"{signal.symbol}｜{etf_name(signal.symbol)}",
                f"信号日：{signal.daily_signal_time:%Y-%m-%d}",
                f"收盘：{signal.close:.2f}",
                "DXDX：是",
                "蓝梯>黄梯：是",
                "",
            ]
        )
    lines.extend(
        [
            f"扫描时间：{scan_time:%Y-%m-%d %H:%M:%S}",
            "数据源：Yahoo official daily（auto_adjust=True, period=max）",
            "仅供研究参考，不构成投资建议。",
        ]
    )
    return subject, "\n".join(lines) + "\n"
