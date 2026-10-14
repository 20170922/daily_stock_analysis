# -*- coding: utf-8 -*-
"""复盘历史链：读已成稿复盘文件，提供昨日预判验证与本周生命图谱素材。

全部 fail-open：文件缺失或解析失败时返回空结构，调用方跳过对应章节。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORTS_DIR = REPO_ROOT / "reports"
REPORT_NAME_RE = re.compile(r"^market_review_(\d{8})\.md$")


@dataclass
class HistoryContext:
    """昨日预判与本周已成稿复盘摘要。"""

    prev_date: str = ""  # YYYYMMDD
    prev_expectations: List[str] = field(default_factory=list)
    prev_numbers: Dict[str, str] = field(default_factory=dict)
    week_files: List[str] = field(default_factory=list)  # YYYYMMDD ascending
    week_summaries: List[Dict[str, str]] = field(default_factory=list)


def _read_report(date_yyyymmdd: str) -> str:
    try:
        return (REPORTS_DIR / f"market_review_{date_yyyymmdd}.md").read_text(encoding="utf-8")
    except OSError:
        return ""


def _extract_expectations(text: str) -> List[str]:
    """提取"明日只核对 N 条"下面的条目行。"""
    lines = text.splitlines()
    out: List[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip()
        if not capturing:
            if "只核对" in stripped:
                capturing = True
            continue
        if stripped.startswith(("- ", "* ", "+ ")):
            item = stripped[2:].strip()
            if item:
                out.append(item)
            continue
        if stripped == "" or stripped.startswith("#"):
            if out:
                break
            continue
        if out:
            break
    return out[:8]


def _extract_numbers(text: str) -> Dict[str, str]:
    """从昨日复盘提取可对比数字：成交额/涨跌家数/涨跌停/封板率。"""
    found: Dict[str, str] = {}
    patterns = {
        "amount": r"两市成交[额约]*\s*([0-9,.]+)\s*亿",
        "up": r"上涨\s*([0-9,]+)",
        "down": r"下跌\s*([0-9,]+)",
        "limit_up": r"涨停\s*([0-9,]+)",
        "limit_down": r"跌停\s*([0-9,]+)",
        "seal_rate": r"封板率\s*([0-9.]+%)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            found[key] = match.group(1)
    return found


def _week_dates(today: datetime) -> List[str]:
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(days=i)).strftime("%Y%m%d") for i in range(5)]


def build_history_context(today_yyyymmdd: str, prev_yyyymmdd: str = "") -> HistoryContext:
    """组装历史上下文（昨日 + 本周一到今日的已成稿文件）。"""
    ctx = HistoryContext(prev_date=prev_yyyymmdd)
    if prev_yyyymmdd:
        prev_text = _read_report(prev_yyyymmdd)
        if prev_text:
            ctx.prev_expectations = _extract_expectations(prev_text)
            ctx.prev_numbers = _extract_numbers(prev_text)
        else:
            logger.info("[ReviewHistory] 未找到昨日复盘，跳过预判验证: %s", prev_yyyymmdd)
    try:
        today = datetime.strptime(today_yyyymmdd, "%Y%m%d")
    except ValueError:
        return ctx
    for day in _week_dates(today):
        if day > today_yyyymmdd:
            continue
        text = _read_report(day)
        if not text:
            continue
        ctx.week_files.append(day)
        numbers = _extract_numbers(text)
        first_line = ""
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("主线") or s.startswith("整体判断"):
                first_line = s[:60]
                break
        ctx.week_summaries.append({"date": day, **numbers, "headline": first_line})
    return ctx


def render_lifemap(ctx: HistoryContext, today_yyyymmdd: str) -> str:
    """渲染第八章：本周生命图谱（纯规则，无 LLM）。"""
    try:
        week_no = datetime.strptime(today_yyyymmdd, "%Y%m%d").isocalendar()[1]
    except ValueError:
        week_no = "-"
    lines = [f"当前进度：第 {week_no} 周 | 本周已成稿复盘：{'、'.join(ctx.week_files) or '无'}"]
    if ctx.week_summaries:
        lines.append("主线演变")
        for row in ctx.week_summaries:
            parts = [row["date"]]
            if row.get("amount"):
                parts.append(f"两市成交约 {row['amount']} 亿")
            up, down = row.get("up"), row.get("down")
            if up or down:
                parts.append(f"上涨 {up or '?'} / 下跌 {down or '?'}")
            lu, ld = row.get("limit_up"), row.get("limit_down")
            if lu or ld:
                parts.append(f"涨停 {lu or '?'}、跌停 {ld or '?'}")
            if row.get("headline"):
                parts.append(row["headline"])
            lines.append("- " + "；".join(parts) + "。")
    else:
        lines.append("本周暂无已成稿复盘可聚合。")
    return "\n".join(lines)
