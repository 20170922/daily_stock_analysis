# -*- coding: utf-8 -*-
"""深度涨停复盘 builder：数据章节纯规则渲染，判断章节走 LLM。

失败策略：取数失败直接抛异常，由调用方降级回老链路（fail-open）；
LLM 不可用时保留数据章节，判断章节留空提示。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_SH = ZoneInfo("Asia/Shanghai")

_INDEX_NAMES = {
    "000001.SH": "上证",
    "399001.SZ": "深证",
    "399006.SZ": "创业板",
    "000688.SH": "科创50",
    "000300.SH": "沪深300",
    "000016.SH": "上证50",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}

_YI = 1e8


def _yi(value: Any) -> float:
    try:
        return round(float(value or 0) / _YI, 2)
    except (TypeError, ValueError):
        return 0.0


def _pct(today: float, prev: float) -> str:
    if not prev:
        return "—"
    v = (today - prev) / abs(prev) * 100
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _fmt_pct(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _bar_date_yyyymmdd(date_ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(date_ms) / 1000, tz=_SH).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return ""


@dataclass
class DeepReviewPacket:
    """builder 与 LLM 之间的数据包（全部是当日可核对事实）。"""

    date: str = ""  # YYYYMMDD
    prev_date: str = ""
    indices: List[Dict[str, Any]] = field(default_factory=list)
    yesterday_limit_chg: str = "—"
    market_amount: float = 0.0
    market_amount_prev: float = 0.0
    market_volume_hands: float = 0.0
    breadth: Dict[str, Any] = field(default_factory=dict)
    first_board_total: int = 0
    failed_ladder: List[Dict[str, Any]] = field(default_factory=list)
    limit_up: int = 0
    limit_down: int = 0
    limit_break: int = 0
    seal_rate: float = 0.0
    limit_up_prev: int = 0
    limit_down_prev: int = 0
    seal_rate_prev: float = 0.0
    ladder: List[Dict[str, Any]] = field(default_factory=list)
    first_boards: List[Dict[str, Any]] = field(default_factory=list)
    reason_ranking: List[Dict[str, Any]] = field(default_factory=list)
    hot: List[Dict[str, Any]] = field(default_factory=list)


class DeepReviewUnavailable(RuntimeError):
    """取数失败，调用方应降级回老链路。"""


class DeepReviewBuilder:
    """按 review_structures 配置生成深度复盘 Markdown。"""

    def __init__(
        self,
        fetcher: Any,
        structure: Dict[str, Any],
        analyzer: Any = None,
        news: Optional[List[Any]] = None,
    ) -> None:
        if fetcher is None:
            raise DeepReviewUnavailable("缺少行情 fetcher")
        self.fetcher = fetcher
        self.structure = structure
        self.analyzer = analyzer
        self.news = news or []
        data_sources = structure.get("data_sources") or {}
        self.index_codes: List[str] = list(data_sources.get("index_codes") or list(_INDEX_NAMES))
        self.ti_code: str = str(data_sources.get("yesterday_limit_index") or "883900.TI")
        self.hot_n: int = int(data_sources.get("hot_n") or 30)
        self.first_board_top_n: int = int(data_sources.get("first_board_top_n") or 8)
        self.reason_rank_top_n: int = int(data_sources.get("reason_rank_top_n") or 4)
        conventions = structure.get("conventions") or {}
        self.flat_tol: float = float(conventions.get("flat_tolerance_pct") or 0.2)
        self.reason_groups: List[Dict[str, Any]] = list(
            conventions.get("reason_groups") or data_sources.get("reason_groups") or []
        )
        self.not_advice: str = str(conventions.get("not_advice") or "")

    # -- 取数 ----------------------------------------------------------
    def _index_bars(self, code: str, start: str, end: str) -> List[Dict[str, Any]]:
        df = self.fetcher._fetch_raw_data(code, start, end)
        if df is None or getattr(df, "empty", True):
            return []
        return [dict(row) for _, row in df.iterrows()]

    @staticmethod
    def _to_ymd(date_str: str) -> str:
        return date_str.replace("-", "")

    def _collect_packet(self, today: str) -> DeepReviewPacket:
        packet = DeepReviewPacket(date=today)
        today_dash = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
        # 对比日：用上证近 10 天 K 的最后一根之前那根，休市日不算昨日。
        start_dash = (datetime.strptime(today, "%Y%m%d") - timedelta(days=10)).strftime("%Y-%m-%d")
        sh_bars = self._index_bars("000001.SH", start_dash, today_dash)[-5:]
        dated = [(b, _bar_date_yyyymmdd(b.get("date_ms"))) for b in sh_bars]
        dated = [(b, d) for b, d in dated if d and d <= today]
        if len(dated) < 2:
            raise DeepReviewUnavailable(f"指数 K 不足，无法确定对比日: {today}")
        if dated[-1][1] != today:
            # 当日未收盘（盘中/节假日）：指数最后一根不是今日，拒绝半成品复盘
            raise DeepReviewUnavailable(f"当日({today})指数 K 缺失，可能未收盘，降级回老链路")
        prev = dated[-2][1]
        packet.prev_date = prev
        prev_dash = f"{prev[:4]}-{prev[4:6]}-{prev[6:8]}"

        indices = []
        sh_turnover = sh_prev = sz_turnover = sz_prev = 0.0
        sh_vol = sz_vol = 0.0
        for code in self.index_codes:
            bars = self._index_bars(code, prev_dash, today_dash)
            pick = [b for b in bars if _bar_date_yyyymmdd(b.get("date_ms")) in (prev, today)]
            if len(pick) < 2:
                logger.warning("[DeepReview] 指数缺 K 已跳过: %s", code)
                continue
            old, cur = pick[-2], pick[-1]
            turnover = float(cur.get("turnover") or 0)
            turnover_prev = float(old.get("turnover") or 0)
            indices.append(
                {
                    "name": _INDEX_NAMES.get(code, code),
                    "close": float(cur.get("close_price") or 0),
                    "change_pct": _fmt_pct(
                        (float(cur.get("close_price") or 0) - float(old.get("close_price") or 0))
                        / float(old.get("close_price") or 1)
                        * 100
                    ),
                    "amount": _yi(turnover),
                    "amount_chg": _pct(turnover, turnover_prev),
                }
            )
            if code == "000001.SH":
                sh_turnover, sh_prev = turnover, turnover_prev
                sh_vol = float(cur.get("volume") or 0)
            if code == "399001.SZ":
                sz_turnover, sz_prev = turnover, turnover_prev
                sz_vol = float(cur.get("volume") or 0)
        if not indices:
            raise DeepReviewUnavailable("指数行情全缺")
        packet.indices = indices
        packet.market_amount = round((sh_turnover + sz_turnover) / _YI, 2)
        packet.market_amount_prev = round((sh_prev + sz_prev) / _YI, 2)
        packet.market_volume_hands = round((sh_vol + sz_vol) / 1e10, 2)

        ti_bars = self._index_bars(self.ti_code, prev_dash, today_dash)
        ti_pick = [b for b in ti_bars if _bar_date_yyyymmdd(b.get("date_ms")) in (prev, today)]
        if len(ti_pick) >= 2:
            old_c = float(ti_pick[-2].get("close_price") or 0)
            cur_c = float(ti_pick[-1].get("close_price") or 0)
            packet.yesterday_limit_chg = _fmt_pct((cur_c - old_c) / old_c * 100 if old_c else 0)

        breadth = None
        try:
            breadth = self.fetcher.get_market_breadth(today)
        except Exception as exc:  # noqa: BLE001 - 方法缺失（其它 fetcher）时走快照
            logger.debug("[DeepReview] Parquet 宽度不可用，走快照: %s", exc)
        if not breadth:
            breadth = self.fetcher.get_market_stats() or {}
        packet.breadth = breadth

        up = self.fetcher.get_limit_up_pool(date=today, n=300) or []
        down = self.fetcher.get_limit_down_pool(date=today) or []
        brk = self.fetcher.get_limit_break_pool(date=today) or []
        packet.limit_up, packet.limit_down, packet.limit_break = len(up), len(down), len(brk)
        packet.seal_rate = round(len(up) / (len(up) + len(brk)) * 100, 2) if (up or brk) else 0.0

        up_prev = self.fetcher.get_limit_up_pool(date=prev, n=300) or []
        down_prev = self.fetcher.get_limit_down_pool(date=prev) or []
        brk_prev = self.fetcher.get_limit_break_pool(date=prev) or []
        packet.limit_up_prev, packet.limit_down_prev = len(up_prev), len(down_prev)
        packet.seal_rate_prev = (
            round(len(up_prev) / (len(up_prev) + len(brk_prev)) * 100, 2) if (up_prev or brk_prev) else 0.0
        )
        # 昨日连板今日未晋级（断板/掉队）：昨日池连板数≥2 且今日不在涨停池
        today_codes = {str(x.get("code") or "") for x in up}
        packet.failed_ladder = [
            {"name": x.get("name", ""), "code": x.get("code", ""),
             "prev_boards": x.get("continue_boards", 0)}
            for x in up_prev
            if (x.get("continue_boards") or 0) >= 2 and str(x.get("code") or "") not in today_codes
        ]

        # 连板天梯与首板
        ladder_raw = sorted(
            [x for x in up if (x.get("continue_boards") or 0) >= 2],
            key=lambda x: (-(x.get("continue_boards") or 0), -(x.get("seal_amount") or 0)),
        )
        caps = {}
        try:
            caps = self.fetcher.get_auction_snapshot([x.get("thscode", "") for x in ladder_raw if x.get("thscode")])
        except Exception as exc:  # noqa: BLE001 - 换手率缺失不阻断，置空
            logger.warning("[DeepReview] 集合竞价快照失败，换手率置空: %s", exc)
        for item in ladder_raw:
            kline = self._stock_day_shape(item.get("code", ""), prev_dash, today_dash)
            cap = caps.get(item.get("thscode", ""), 0)
            turnover_rate = (
                round(float(kline.get("volume") or 0) * 100 / (cap / float(kline.get("close") or 1)), 2)
                if cap and kline.get("close")
                else 0.0
            )
            packet.ladder.append({**item, **kline, "turnover_rate": turnover_rate})
        first_raw = [x for x in up if (x.get("continue_boards") or 0) <= 1]
        packet.first_board_total = len(first_raw)
        packet.first_boards = sorted(first_raw, key=lambda x: -(x.get("seal_amount") or 0))[
            : self.first_board_top_n
        ]

        # 热点排名：先按 YAML reason_groups 归组，未命中按首关键词单列
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in up:
            reason = str(item.get("reason") or "未标注").strip() or "未标注"
            group_name = ""
            for group_cfg in self.reason_groups:
                keywords = group_cfg.get("keywords") or []
                if any(kw and kw in reason for kw in keywords):
                    group_name = str(group_cfg.get("name") or "").strip()
                    break
            if not group_name:
                group_name = re.split(r"[+＋、/／|｜]", reason, maxsplit=1)[0].strip() or "未标注"
            groups.setdefault(group_name, []).append(item)
        ranking = sorted(groups.items(), key=lambda kv: -len(kv[1]))[: self.reason_rank_top_n]
        for reason, members in ranking:
            top = sorted(members, key=lambda x: -(x.get("seal_amount") or 0))[:3]
            packet.reason_ranking.append(
                {
                    "reason": reason,
                    "count": len(members),
                    "members": [f"{m.get('name')}" for m in top],
                }
            )

        packet.hot = self.fetcher.get_hot_stocks(n=self.hot_n) or []
        return packet

    def _stock_day_shape(self, code: str, prev_dash: str, today_dash: str) -> Dict[str, Any]:
        """未复权日 K 判断一字/T字/换手，返回 turnover(亿)/shape/close/volume。"""
        out = {"turnover": 0.0, "shape": "换手板", "close": 0.0, "volume": 0.0}
        try:
            df = self.fetcher._fetch_raw_data(code, prev_dash, today_dash, adjust="none")
        except Exception as exc:  # noqa: BLE001 - 单只失败不阻断
            logger.debug("[DeepReview] 个股日 K 失败已跳过 %s: %s", code, exc)
            return out
        if df is None or getattr(df, "empty", True):
            return out
        row = dict(df.iloc[-1])
        o, h, l, c = (float(row.get(k) or 0) for k in ("open_price", "high_price", "low_price", "close_price"))
        out["turnover"] = _yi(row.get("turnover"))
        out["close"] = round(c, 2)
        out["volume"] = float(row.get("volume") or 0)
        if not c:
            return out
        rng = (h - l) / c * 100
        open_high_gap = abs((o or h) - h) / c * 100
        close_high_gap = abs(c - h) / c * 100
        if rng <= self.flat_tol:
            out["shape"] = "一字板"
        elif open_high_gap <= self.flat_tol and close_high_gap <= self.flat_tol:
            out["shape"] = "T字板"
        return out

    # -- 数据章节渲染 ------------------------------------------------------
    def _render_market_status(self, packet: DeepReviewPacket) -> str:
        lines = []
        for idx in packet.indices:
            lines.append(
                f"| {idx['name']} | {idx['close']:.2f} | {idx['change_pct']} | {idx['amount']:.2f}亿 | {idx['amount_chg']} |"
            )
        table = "| 指数 | 收盘 | 涨跌幅 | 成交额 | 较昨日 |\n| --- | --- | --- | --- | --- |\n" + "\n".join(lines)
        b = packet.breadth
        up_n = b.get("up_count", "—")
        flat_n = b.get("flat_count", "—")
        down_n = b.get("down_count", "—")
        prev_nums = getattr(self, "_history_numbers", {})
        amount_chg = _pct(packet.market_amount, packet.market_amount_prev)
        judgment = (
            f"整体判断：两市成交额 {packet.market_amount:.2f} 亿，较 {packet.prev_date[4:6]}/{packet.prev_date[6:]} "
            f"增加 {packet.market_amount - packet.market_amount_prev:.2f} 亿（{amount_chg}）；"
            f"市场宽度 {up_n} 家上涨、{flat_n} 家平盘、{down_n} 家下跌。"
            f"情绪：涨停 {packet.limit_up} 家、跌停 {packet.limit_down} 家、炸板 {packet.limit_break} 家、"
            f"封板率 {packet.seal_rate:.2f}%；昨日涨停表现 {self.ti_code} 收 {packet.yesterday_limit_chg}。"
        )
        if prev_nums:
            judgment += (
                f"对比昨日：涨停 {prev_nums.get('limit_up', '—')}→{packet.limit_up} 家、"
                f"跌停 {prev_nums.get('limit_down', '—')}→{packet.limit_down} 家、"
                f"封板率 {prev_nums.get('seal_rate', '—')}→{packet.seal_rate:.2f}%。"
            )
        return f"{table}\n\n{judgment}"

    def _render_limitup(self, packet: DeepReviewPacket) -> str:
        head = (
            f"| 维度 | 数据 |\n| --- | --- |\n"
            f"| 涨停家数（沪深口径） | {packet.limit_up}家 |\n"
            f"| 跌停家数 | {packet.limit_down}家 |\n"
            f"| 炸板家数 | {packet.limit_break}家 |\n"
        )
        if not packet.ladder:
            ladder_text = "没有连续连板。"
            max_board = "无"
        else:
            max_n = max(x.get("continue_boards", 0) for x in packet.ladder)
            top_names = "、".join(x.get("name", "") for x in packet.ladder if x.get("continue_boards") == max_n)
            max_board = f"{max_n}板（{top_names}）"
            parts = []
            for x in packet.ladder:
                parts.append(
                    f"- {x.get('continue_boards')}板：{x.get('name')}（{x.get('reason') or '未标注'}）"
                    f"{x.get('shape', '')}，成交 {x.get('turnover', 0):.2f} 亿，"
                    f"封单 {float(x.get('seal_amount') or 0) / _YI:.2f} 亿，换手 {x.get('turnover_rate', 0):.2f}%"
                )
            ladder_text = "连板天梯（仅连续连板）\n" + "\n".join(parts)
        first_text = "首板代表（按封单）：{}".format(
            "、".join(f"{x.get('name')} {float(x.get('seal_amount') or 0) / _YI:.2f} 亿" for x in packet.first_boards)
            or "无"
        )
        failed_text = ""
        if packet.failed_ladder:
            failed_text = "昨日连板今日未晋级：{}".format(
                "、".join(f"{x['name']}(昨日{x['prev_boards']}板)" for x in packet.failed_ladder)
            )
        reason_lines = ["热点涨停板块排名", "按涨停原因关键词归类。"]
        for i, group in enumerate(packet.reason_ranking, 1):
            reason_lines.append(
                f"{i}. {group['reason']}：{group['count']}家（{'/'.join(group['members'])}）"
            )
        return (
            f"{head}"
            f"| 连续连板最高板 | {max_board} |\n"
            f"| 连续连板股数量 | {len(packet.ladder)}只 |\n"
            f"| 首板数量 | {packet.first_board_total}只 |\n"
            f"| 封板率(今日) | {packet.seal_rate:.2f}% |\n"
            f"| 封板率(昨日，{packet.prev_date[4:6]}/{packet.prev_date[6:]}) | {packet.seal_rate_prev:.2f}% |\n"
            f"| 昨日涨停表现（{self.ti_code}） | {packet.yesterday_limit_chg} |\n\n"
            f"{ladder_text}\n{first_text}\n"
            f"{failed_text + chr(10) if failed_text else ''}\n" + "\n".join(reason_lines)
        )

    def _render_hotlist(self, packet: DeepReviewPacket) -> str:
        lines = ["热度榜", "同花顺热股当天榜，`period=day`。排名变化和趋势是接口原值。"]
        lines.append("| 排名 | 个股 | 代码 | 热度 | 排名变化 | 趋势 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in packet.hot:
            change = item.get("rank_change")
            change_text = f"{'+' if isinstance(change, int) and change > 0 else ''}{change}" if change is not None else "—"
            code_text = item.get("thscode") or f"{item.get('code')}.SH"
            lines.append(
                f"| {item.get('rank')} | {item.get('name')} | {code_text} "
                f"| {item.get('heat')} | {change_text} | {item.get('trend_text')} |"
            )
        return "\n".join(lines)

    def _render_notes(self, packet: DeepReviewPacket) -> str:
        return (
            "数据说明\n"
            f"- 数据源：同花顺金融数据服务公开接口。对比日为 {packet.prev_date} 与 {packet.date}。"
            "个股复权口径为 `none`。涨停、跌停、炸板池查询日为当日。\n"
            f"- 市场宽度来自全市场近 10 个交易日日 K（未复权收盘对比前一日，沪深股票，不含北交所）。"
            f"{packet.date} 为 {packet.breadth.get('up_count', '—')} 涨、"
            f"{packet.breadth.get('flat_count', '—')} 平、{packet.breadth.get('down_count', '—')} 跌。\n"
            "- 两市成交额为上证与深证成交额之和；成交量按指数日 K 的股数换算成手后相加。\n"
            "- 首板代表按涨停池封单金额取前列（省掉全量日 K 请求的口径）。\n"
            "- 连板换手率 = 当日成交量 / 流通股本。流通股本用集合竞价接口的流通市值除以当日最新价。\n"
            f"- 昨日涨停表现是 `{self.ti_code}` 的日涨跌幅。\n"
            "- 热度榜用当天榜。热度、排名变化、趋势用接口原值。\n"
            f"- {self.not_advice}"
        )

    # -- LLM 章节 ----------------------------------------------------------
    def _packet_text(self, packet: DeepReviewPacket) -> str:
        parts = [
            f"交易日：{packet.date}，对比日：{packet.prev_date}",
            "指数：" + "；".join(
                f"{i['name']}收{i['close']:.2f}（{i['change_pct']}），成交{i['amount']:.2f}亿（{i['amount_chg']}）"
                for i in packet.indices
            ),
            f"两市成交{packet.market_amount:.2f}亿；宽度：涨{packet.breadth.get('up_count')} 平{packet.breadth.get('flat_count')} 跌{packet.breadth.get('down_count')}",
            f"涨停{packet.limit_up} 跌停{packet.limit_down} 炸板{packet.limit_break} 封板率{packet.seal_rate:.2f}%（昨日{packet.seal_rate_prev:.2f}%）",
            f"昨日涨停表现{packet.yesterday_limit_chg}",
            "连板：" + "；".join(
                f"{x.get('name')}{x.get('continue_boards')}板{x.get('shape')}成交{x.get('turnover', 0):.2f}亿封单{float(x.get('seal_amount') or 0) / _YI:.2f}亿换手{x.get('turnover_rate', 0):.2f}%"
                for x in packet.ladder
            ),
            "昨日连板今日未晋级：" + "、".join(
                f"{x['name']}(昨日{x['prev_boards']}板)" for x in packet.failed_ladder
            ) or "无",
            "热点：" + "；".join(f"{g['reason']}{g['count']}家" for g in packet.reason_ranking),
            "热榜前5：" + "、".join(str(x.get("name")) for x in packet.hot[:5]),
        ]
        if self.news:
            titles = []
            for item in self.news[:10]:
                if isinstance(item, dict):
                    titles.append(str(item.get("title") or ""))
                else:
                    titles.append(str(getattr(item, "title", "") or ""))
            titles = [t for t in titles if t]
            if titles:
                parts.append("相关新闻标题：" + "；".join(titles))
        return "\n".join(parts)

    def _llm_sections(self, packet: DeepReviewPacket, history_numbers: Dict[str, str],
                      prev_expectations: List[str]) -> Dict[str, str]:
        sections = {s.get("key"): s for s in (self.structure.get("sections") or []) if isinstance(s, dict)}
        llm_cfg = self.structure.get("llm") or {}
        groups: List[List[str]] = llm_cfg.get("groups") or []
        temperature = float(llm_cfg.get("temperature") or 0.3)
        max_tokens = int(llm_cfg.get("max_tokens") or 4000)
        results: Dict[str, str] = {}
        if self.analyzer is None:
            return results
        history_block = ""
        if prev_expectations:
            history_block = "【昨日预期】（只验证这些条目）\n" + "\n".join(f"- {e}" for e in prev_expectations)
        for group in groups:
            prompts = []
            for key in group:
                section = sections.get(key)
                if not section or not section.get("prompt"):
                    continue
                prompts.append(f"## {section.get('title', key)}\n{section['prompt']}")
            if not prompts:
                continue
            prompt = (
                "你是 A 股短线复盘助手，只基于【数据包】写复盘，不编造数据。\n\n"
                f"【数据包】\n{self._packet_text(packet)}\n\n"
                f"{history_block}\n\n"
                + "\n\n".join(prompts)
                + "\n\n直接输出各章节正文（三级标题开头），不要输出说明文字。"
            )
            try:
                text = self.analyzer.generate_text(prompt, max_tokens=max_tokens, temperature=temperature)
            except Exception as exc:  # noqa: BLE001 - 单组失败不阻断整篇
                logger.warning("[DeepReview] LLM 分组失败已跳过: %s", exc)
                text = None
            if text:
                results[tuple(group).__str__()] = text
        return results

    # -- 组装 --------------------------------------------------------------
    def build(self, today: str) -> str:
        """生成完整八章 Markdown（取数失败抛 DeepReviewUnavailable）。"""
        packet = self._collect_packet(today)
        from .history import build_history_context, render_lifemap

        history = build_history_context(today, packet.prev_date)
        self._history_numbers = history.prev_numbers
        sections_cfg = self.structure.get("sections") or []
        data_rendered = {
            "market_status": self._render_market_status(packet),
            "limitup": self._render_limitup(packet),
            "hotlist": self._render_hotlist(packet),
            "notes": self._render_notes(packet),
        }
        llm_results = self._llm_sections(packet, history.prev_numbers, history.prev_expectations)

        titles = {s.get("key"): s.get("title", "") for s in sections_cfg if isinstance(s, dict)}
        group_texts = list(llm_results.values())
        # 按 groups 顺序把 LLM 文本拆回各 key：第一组→mainline/core_stocks/mindmap，第二组→expectation/plan
        llm_cfg = self.structure.get("llm") or {}
        groups: List[List[str]] = llm_cfg.get("groups") or []
        llm_by_key: Dict[str, str] = {}
        for group, text in zip(groups, group_texts):
            if len(group) == 1:
                llm_by_key[group[0]] = text
            else:
                # 多节合调时模型按三级标题分节输出，整块挂到首节，其余节给空提示避免丢章
                llm_by_key[group[0]] = text
                for extra in group[1:]:
                    llm_by_key.setdefault(extra, "")

        order = [s.get("key") for s in sections_cfg if isinstance(s, dict)]
        chunks = []
        for key in order:
            title = titles.get(key, "")
            if key in data_rendered:
                chunks.append(f"### {title}\n{data_rendered[key]}")
            elif key == "lifemap":
                chunks.append(f"### {title}\n{render_lifemap(history, today)}")
            elif key in llm_by_key and llm_by_key[key]:
                chunks.append(llm_by_key[key])
            elif key in llm_by_key:
                continue  # 合调组的非首节（内容已在首节整块输出）
            else:
                chunks.append(
                    f"### {title}\n_本节需 LLM 生成，当前模型不可用，仅保留数据章节。_"
                )
        return "\n".join(chunks)
