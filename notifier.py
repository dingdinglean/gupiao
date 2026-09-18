"""Chinese-first Gmail-compatible SMTP delivery for DXDX alerts."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send_email(smtp_host: str, smtp_port: int, smtp_user: str, smtp_password: str, to_addrs: list[str], subject: str, body_text: str, *, retries: int = 1) -> None:
    """Use STARTTLS on 587 (Gmail) and implicit TLS on 465; retry once."""
    message = EmailMessage()
    message["From"] = smtp_user
    message["To"] = ", ".join(to_addrs)
    message["Subject"] = subject
    message.set_content(body_text)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if smtp_port == 587:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                    server.ehlo()
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                    server.login(smtp_user, smtp_password)
                    server.send_message(message)
            else:
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context(), timeout=30) as server:
                    server.login(smtp_user, smtp_password)
                    server.send_message(message)
            log.info("Email sent successfully to configured recipient(s)")
            return
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                log.warning("SMTP attempt %s failed; retrying once: %r", attempt + 1, exc)
                time.sleep(2)
    raise last_error or RuntimeError("SMTP delivery failed")


def format_signals_email(signals: list, *, pool_count: int, scan_time: datetime | None = None) -> tuple[str, str]:
    """Format official-daily-only DXDX alerts."""
    scan_time = scan_time or datetime.now()
    daily = [signal for signal in signals if signal.daily_dxdx]
    subject = f"【美股日线抄底雷达】日{len(daily)}｜{scan_time:%Y-%m-%d}"
    lines = ["【美股日线抄底雷达】", "", "🔥 新的 official daily DXDX 信号", ""]
    for signal in daily:
        lines.extend(["🔵 日线抄底", signal.symbol, "日线：DXDX", "趋势：蓝梯 > 黄梯", f"收盘价：${signal.close:.2f}", ""])
    lines.extend([
        f"一句话：本次扫描 {pool_count} 只美股，发现日线 DXDX {len(daily)} 只。",
        f"数据时间：{scan_time:%Y-%m-%d} official daily 确认后",
        "仅供研究参考，不构成投资建议。",
    ])
    return subject, "\n".join(lines) + "\n"


def format_long_signals_email(signals: list, *, pool_count: int, scan_time: datetime | None = None) -> tuple[str, str]:
    """Format independent weekly/monthly DXDX alerts without trend filtering."""
    scan_time = scan_time or datetime.now()
    monthly = [signal for signal in signals if signal.timeframe == "monthly"]
    weekly = [signal for signal in signals if signal.timeframe == "weekly"]
    subject = f"【美股周/月抄底雷达】月{len(monthly)} 周{len(weekly)}｜{scan_time:%Y-%m-%d}"
    lines = ["【美股周/月抄底雷达】", "", "🔥 新的大周期抄底信号", ""]
    for label, items, timeframe_label in (("🟣 月线抄底", monthly, "月线"), ("🟠 周线抄底", weekly, "周线")):
        for signal in items:
            lines.extend([
                label,
                signal.symbol,
                f"{timeframe_label}：DXDX",
                f"信号K：{signal.signal_time:%Y-%m-%d}",
                f"信号收盘价：${signal.close:.2f}",
                "",
            ])
    lines.extend([
        "规则说明：DXDX 大周期底背离，不附加日线蓝梯 > 黄梯过滤。",
        f"本次扫描 {pool_count} 只美股。",
        f"月线信号 {len(monthly)} 个，周线信号 {len(weekly)} 个。",
        "仅供研究参考，不构成投资建议。",
    ])
    return subject, "\n".join(lines) + "\n"
