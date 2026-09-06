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
    """Keep the alert short and Chinese-first; code symbols remain English."""
    scan_time = scan_time or datetime.now()
    grouped = {level: [signal for signal in signals if signal.signal_level == level] for level in ("S", "A", "B")}
    subject = f"【美股双周期抄底雷达】S{len(grouped['S'])} A{len(grouped['A'])} B{len(grouped['B'])}｜{scan_time:%Y-%m-%d}"
    labels = {"S": "🥇 S级｜双周期共振", "A": "🟢 A级｜4H抄底", "B": "🔵 B级｜日线抄底"}
    lines = ["【美股双周期抄底雷达】", "", "🔥 今日新信号", ""]
    for level in ("S", "A", "B"):
        for signal in grouped[level]:
            lines.extend([labels[level], signal.symbol])
            if signal.daily_dxdx:
                lines.append("日线：DXDX")
            if signal.h4_dxdx:
                lines.append("4H：DXDX")
            lines.extend(["趋势：蓝梯 > 黄梯", f"收盘价：${signal.close:.2f}", ""])
    lines.extend([
        f"一句话：今日扫描 {pool_count} 只美股，发现 S级 {len(grouped['S'])} 只、A级 {len(grouped['A'])} 只、B级 {len(grouped['B'])} 只。",
        f"数据时间：{scan_time:%Y-%m-%d} 美股收盘后",
        "仅供研究参考，不构成投资建议。",
    ])
    return subject, "\n".join(lines) + "\n"
