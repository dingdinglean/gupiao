"""Email notifier (QQ Mail / Gmail compatible SMTP).

For QQ Mail you need an 'authorization code' (授权码), not your QQ password.
See README.md for instructions.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_email(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    from_addr: str,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    body_html: str | None = None,
):
    """Send via SMTPS (SSL). Works for QQ (smtp.qq.com:465) and Gmail (smtp.gmail.com:465)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=30) as srv:
        srv.login(smtp_user, smtp_password)
        srv.sendmail(from_addr, to_addrs, msg.as_string())
    log.info(f"Email sent to {to_addrs}")


def format_hits_email(hits: list, scan_time: datetime | None = None) -> tuple[str, str, str]:
    """Build subject + text + html body for the email."""
    if scan_time is None:
        scan_time = datetime.now()
    n = len(hits)
    subject = f"[选股] {n} 只美股触发抄底信号 - {scan_time:%Y-%m-%d %H:%M}"

    text_lines = [
        f"扫描时间: {scan_time:%Y-%m-%d %H:%M:%S}",
        f"触发条件:",
        f"  · 蓝梯(EMA23) > 黄梯(EMA89)",
        f"  · 日线 DXDX 抄底首现 或 4H DXDX 抄底首现",
        "",
        f"命中 {n} 只:",
        "",
    ]
    for h in hits:
        text_lines.append("  " + h.to_text())
    body_text = "\n".join(text_lines)

    html_rows = []
    for h in hits:
        daily = f"{h.daily_signal_at:%Y-%m-%d}" if h.daily_signal_at else "-"
        h4 = f"{h.h4_signal_at:%Y-%m-%d %H:%M}" if h.h4_signal_at else "-"
        strict_tag = ""
        if h.blue_strict_daily and h.blue_strict_h4:
            strict_tag = "<span style='color:#16a34a;font-size:12px'>· 双周期完全分离</span>"
        elif h.blue_strict_daily:
            strict_tag = "<span style='color:#16a34a;font-size:12px'>· 日线完全分离</span>"
        elif h.blue_strict_h4:
            strict_tag = "<span style='color:#16a34a;font-size:12px'>· 4H完全分离</span>"
        html_rows.append(
            f"<tr>"
            f"<td style='font-weight:600'><a href='https://finance.yahoo.com/quote/{h.symbol}' "
            f"style='color:#0066cc;text-decoration:none'>{h.symbol}</a></td>"
            f"<td>${h.daily_close:.2f}</td>"
            f"<td>{daily}</td>"
            f"<td>{h4}</td>"
            f"<td>{strict_tag}</td>"
            f"</tr>"
        )

    body_html = f"""\
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#222">
<h2 style="margin-bottom:4px">{subject}</h2>
<p style="color:#666;margin-top:0">
扫描时间 {scan_time:%Y-%m-%d %H:%M:%S}<br>
条件: 蓝梯&gt;黄梯 + (日线抄底 或 4H抄底)
</p>
<table style="border-collapse:collapse;font-size:14px">
  <thead>
    <tr style="background:#f3f4f6">
      <th style="padding:8px 12px;text-align:left;border:1px solid #ddd">代码</th>
      <th style="padding:8px 12px;text-align:left;border:1px solid #ddd">收盘价</th>
      <th style="padding:8px 12px;text-align:left;border:1px solid #ddd">日抄底</th>
      <th style="padding:8px 12px;text-align:left;border:1px solid #ddd">4H抄底</th>
      <th style="padding:8px 12px;text-align:left;border:1px solid #ddd">标签</th>
    </tr>
  </thead>
  <tbody>
    {''.join(html_rows)}
  </tbody>
</table>
<p style="color:#999;font-size:12px;margin-top:20px">
本邮件由 stock_screener 自动发送 · 仅供研究参考,不构成投资建议
</p>
</body></html>"""
    return subject, body_text, body_html
