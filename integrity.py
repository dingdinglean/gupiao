"""Data-integrity tiers for a finished scan.

SUCCESS keeps the original report and email rules.
PARTIAL keeps those rules and adds a warning banner that names the missing symbols.
CRITICAL withholds the normal signal report, sends one failure notice, and fails the job.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from enum import Enum


class DataTier(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class DataIntegrity:
    tier: DataTier
    missing_symbols: tuple[str, ...]
    missing_count: int
    critical_reasons: tuple[str, ...]


def classify_integrity(
    *,
    pool_count: int,
    fetched_count: int,
    missing_symbols: list[str] | None = None,
    critical_reasons: list[str] | None = None,
) -> DataIntegrity:
    """Classify one finished scan without looking at indicator results."""
    known = tuple(sorted({str(symbol).upper() for symbol in (missing_symbols or []) if symbol}))
    reasons = tuple(reason for reason in (critical_reasons or []) if reason)
    pool = max(int(pool_count), 0)
    fetched = max(int(fetched_count), 0)
    missing_count = max(len(known), pool - fetched)
    if reasons or pool <= 0 or fetched <= 0 or (pool > 0 and missing_count >= pool):
        if not reasons and pool <= 0:
            reasons = ("扫描池为空",)
        elif not reasons:
            reasons = ("全部标的取数失败",)
        return DataIntegrity(DataTier.CRITICAL, known, missing_count, reasons)
    if missing_count > 0:
        return DataIntegrity(DataTier.PARTIAL, known, missing_count, ())
    return DataIntegrity(DataTier.SUCCESS, (), 0, ())


def warning_banner(integrity: DataIntegrity) -> str:
    """Explicit banner for PARTIAL and CRITICAL. SUCCESS has no banner."""
    if integrity.tier == DataTier.SUCCESS:
        return ""
    label = "部分缺失" if integrity.tier == DataTier.PARTIAL else "严重失败"
    symbols = ", ".join(integrity.missing_symbols) if integrity.missing_symbols else "（无逐项代码）"
    lines = [
        f"【数据完整性：{label}】",
        f"缺失数量：{integrity.missing_count}",
        f"缺失标的：{symbols}",
    ]
    if integrity.critical_reasons:
        lines.append("原因：" + "；".join(integrity.critical_reasons))
    return "\n".join(lines) + "\n"


def prepend_banner(text: str, integrity: DataIntegrity) -> str:
    banner = warning_banner(integrity)
    if not banner:
        return text
    return banner + "\n" + text


def prepend_html_banner(html_text: str, integrity: DataIntegrity) -> str:
    banner = warning_banner(integrity)
    if not banner:
        return html_text
    return "<pre>" + html.escape(banner) + "</pre>\n" + html_text


def failure_notification(job_name: str, integrity: DataIntegrity) -> tuple[str, str]:
    """Separate failure mail. This is not the signal report."""
    subject = f"【数据完整性失败】{job_name}"
    body = (
        warning_banner(integrity)
        + f"\n本次未发送{job_name}的常规信号报告。\n"
        + "请检查行情源与成分股缓存。\n"
    )
    return subject, body


def finish_integrity(logger: logging.Logger, integrity: DataIntegrity) -> int:
    """Return 1 only for CRITICAL. PARTIAL stays visible in the report and exits 0."""
    if integrity.tier == DataTier.CRITICAL:
        logger.error("数据完整性 CRITICAL：%s", warning_banner(integrity).replace("\n", " | "))
        return 1
    if integrity.tier == DataTier.PARTIAL:
        logger.warning(
            "数据完整性 PARTIAL 缺失数量=%s 缺失标的=%s",
            integrity.missing_count,
            ",".join(integrity.missing_symbols),
        )
    return 0


def attach_integrity(stats: object, integrity: DataIntegrity) -> DataIntegrity:
    setattr(stats, "integrity", integrity)
    return integrity
