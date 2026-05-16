"""Email notifier (QQ Mail / Gmail SMTP)."""
from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


def send_email(smtp_host, smtp_port, smtp_user, smtp_password,
               from_addr, to_addrs, subject, body_text, body_html=None):
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


def format_hits_email(hits, scan_time=None):
    if scan_time is None:
        scan_time = datetime.now()
    n = len(hits)
    subject = f"[选股] {n} 只美股触发抄底 - {scan_time:%Y-%m-%d %H:%M}"

    text_lines = [
        f"扫描时间: {scan_time:%Y-%m-%d %H:%M:%S}",
        "触发条件: 蓝梯>黄梯 + (日线抄底 或 4H抄底)",
        "",
        f"命中 {n} 只:",
        "",
    ]
    for h in hits:
        text_lines.append("  " + h.to_text())
    body_text = "\n".join(text_lines)

    rows = []
    for h in hits:
        rows.append(
            f"<tr>"
            f"<td style='font-weight:600'>"
            f"<a href=' ' style='color:#0066cc'>{h.symbol}</a ></td>"
            f"<td>${h.daily_close:.2f}</td>"
            f"<td>{h.daily_str()}</td>"
            f"<td>{h.h4_str()}</td>"
            f"</tr>"
        )

    body_html = f"""<html><body style="font-family:Helvetica,Arial,sans-serif;color:#222">
<h2>{subject}</h2>
<p style="color:#666">扫描时间 {scan_time:%Y-%m-%d %H:%M:%S}<br>条件: 蓝梯&gt;黄梯 + (日抄底 或 4H抄底)</p >
<table style="border-collapse:collapse;font-size:14px">
  <thead><tr style="background:#f3f4f6">
    <th style="padding:8px 12px;border:1px solid #ddd">代码</th>
    <th style="padding:8px 12px;border:1px solid #ddd">收盘价</th>
    <th style="padding:8px 12px;border:1px solid #ddd">日抄底</th>
    <th style="padding:8px 12px;border:1px solid #ddd">4H抄底</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
<p style="color:#999;font-size:12px;margin-top:20px">仅供研究参考,不构成投资建议</p >
</body></html>"""
    return subject, body_text, body_html
