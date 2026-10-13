# -*- coding: utf-8 -*-
"""
HithinkFetcher - 同花顺金融数据服务行情适配器（可选数据源，fail-open）

数据来源：同花顺官方 Financial-API（fuyao.aicubes.cn），REST + X-api-key 鉴权。
契约见隔壁仓库 D:/stock/Financial-API docs/api（a-share/prices、a-share-index、
a-share/special-data）。无 Key 时不在 DataFetcherManager 中注册。

实现范围（v1）：
- get_main_indices: 指数行情快照（上证/深成/创业板/科创50/上证50/沪深300）
- get_realtime_quote: 单只 A 股实时快照
- _fetch_raw_data/_normalize_data: 单只 A 股历史日线（默认前复权）
- get_limit_up_pool: 涨停池/连板梯队
- get_hot_stocks: 热股榜单
- get_market_stats: 全市场快照分页统计涨跌家数 + 涨跌停池计数
- get_sector_rankings/get_concept_rankings: 未实现（待 TI 板块目录对接），返回 None
  由管理器继续降级到其它数据源。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
)
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int


logger = logging.getLogger(__name__)


_DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
_AUTH_MISSING_CODE = 2003
# 全市场快照翻页保护：500 条/页时约 11 页可覆盖全市场，30 页为异常兜底。
_MAX_SNAPSHOT_PAGES = 30

# （thscode，项目内 code，中文名）
_CN_MAIN_INDICES = (
    ("000001.SH", "sh000001", "上证指数"),
    ("399001.SZ", "sz399001", "深证成指"),
    ("399006.SZ", "sz399006", "创业板指"),
    ("000688.SH", "sh000688", "科创50"),
    ("000016.SH", "sh000016", "上证50"),
    ("000300.SH", "sh000300", "沪深300"),
)


def to_thscode(stock_code: str) -> str:
    """将项目内股票代码转换为同花顺 thscode（600519.SH / 300364.SZ / 430047.BJ）。"""
    raw = (stock_code or "").strip().upper()
    for sep in (".SH", ".SZ", ".BJ"):
        if raw.endswith(sep) and len(raw) > len(sep):
            return raw
    # 显式交易所前缀优先（sh600519 / SH000001 / ss600519），避免裸 000001 被误判。
    prefix_map = {"SH": "SH", "SS": "SH", "SZ": "SZ", "BJ": "BJ"}
    for prefix, suffix in prefix_map.items():
        if raw.startswith(prefix):
            digits = "".join(ch for ch in raw[len(prefix):] if ch.isdigit())
            if len(digits) == 6:
                return f"{digits}.{suffix}"
            break
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 6:
        raise DataFetchError(f"[Hithink] 无法解析股票代码: {stock_code!r}")
    if digits.startswith(("60", "68")):
        return f"{digits}.SH"
    if digits.startswith(("00", "30")):
        return f"{digits}.SZ"
    if digits.startswith(("43", "83", "87", "92")):
        return f"{digits}.BJ"
    raise DataFetchError(f"[Hithink] 无法判断交易所后缀: {stock_code!r}")


def _project_index_code(thscode: str) -> str:
    digits = thscode.split(".")[0]
    suffix = thscode.split(".")[1].lower() if "." in thscode else ""
    prefix = {"sh": "sh", "sz": "sz"}.get(suffix, "")
    return f"{prefix}{digits}"


def _is_index_thscode(thscode: str) -> bool:
    """判断是否为指数 thscode（指数历史走 /api/a-share-index，且无复权语义）。"""
    bare, _, suffix = thscode.partition(".")
    suffix = suffix.upper()
    if suffix == "TI":
        return True
    if suffix == "SH" and bare.startswith("000"):
        return True
    if suffix == "SZ" and bare.startswith("399"):
        return True
    return False


def _shanghai_midnight_ms(date_yyyymmdd: str) -> int:
    """YYYYMMDD -> 当日 Asia/Shanghai 00:00 的毫秒时间戳（涨跌停池 date_ms 口径）。"""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
    except Exception:  # pragma: no cover - 回退到东八区固定偏移
        tz = timezone(timedelta(hours=8))
    day = datetime.strptime(date_yyyymmdd, "%Y%m%d").replace(tzinfo=tz)
    return int(day.timestamp() * 1000)


class HithinkFetcher(BaseFetcher):
    """同花顺金融数据服务（fuyao）行情适配器。"""

    name = "HithinkFetcher"

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 15.0,
        priority: int = 0,
        snapshot_page_size: int = 500,
        session_factory: Optional[Callable[[], requests.Session]] = None,
    ) -> None:
        if not (api_key or "").strip():
            raise DataFetchError("[Hithink] 缺少 API Key（HITHINK_FINANCE_API_KEY）")
        self.api_key = api_key.strip()
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.priority = priority
        self.snapshot_page_size = snapshot_page_size
        self._session_factory = session_factory or requests.Session
        self._shared_session: Optional[requests.Session] = None

    def _session(self) -> requests.Session:
        # 管理器侧已按 fetcher 实例加锁串行调用，这里复用单会话避免连接泄漏。
        if self._shared_session is None:
            self._shared_session = self._session_factory()
        return self._shared_session

    def close(self) -> None:
        session, self._shared_session = self._shared_session, None
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    # -- 基础请求 ----------------------------------------------------------
    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session().get(
                url, params=params, headers={"X-api-key": self.api_key}, timeout=self.timeout
            )
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as exc:
            raise DataFetchError(f"[Hithink] 请求失败 {path}: {exc}") from exc
        except ValueError as exc:
            raise DataFetchError(f"[Hithink] 响应非 JSON {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else payload
            hint = (
                "（请检查 HITHINK_FINANCE_API_KEY 是否有效）"
                if code == _AUTH_MISSING_CODE
                else ""
            )
            raise DataFetchError(f"[Hithink] 业务错误 {path} code={code}: {message}{hint}")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise DataFetchError(f"[Hithink] 响应结构异常 {path}: data 非对象")
        return data

    # -- 指数 --------------------------------------------------------------
    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        if region != "cn":
            return None
        thscodes = ",".join(item[0] for item in _CN_MAIN_INDICES)
        data = self._request("/api/a-share-index/prices/snapshot", {"thscodes": thscodes})
        items = {item.get("thscode"): item for item in (data.get("item") or []) if isinstance(item, dict)}
        results = []
        for thscode, code, name in _CN_MAIN_INDICES:
            item = items.get(thscode)
            if not item:
                continue
            current = safe_float(item.get("last_price"))
            change_pct = safe_float(item.get("price_change_ratio_pct"))
            if current is None:
                continue
            prev = safe_float(item.get("prev_price"))
            high = safe_float(item.get("high_price"))
            low = safe_float(item.get("low_price"))
            amplitude = (
                round((high - low) / prev * 100, 2)
                if prev and high is not None and low is not None
                else 0.0
            )
            results.append(
                {
                    "code": code,
                    "name": name,
                    "current": current,
                    "change": safe_float(item.get("price_change"), 0.0),
                    "change_pct": change_pct if change_pct is not None else 0.0,
                    "open": safe_float(item.get("open_price"), current),
                    "high": high if high is not None else current,
                    "low": low if low is not None else current,
                    "prev_close": prev if prev is not None else current,
                    "volume": safe_float(item.get("volume"), 0.0),
                    "amount": safe_float(item.get("turnover"), 0.0),
                    "amplitude": amplitude,
                }
            )
        return results or None

    # -- 实时行情 ----------------------------------------------------------
    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        thscode = to_thscode(stock_code)
        # 指数快照走指数端点（A 股快照端点不覆盖指数 thscode）。
        path = (
            "/api/a-share-index/prices/snapshot"
            if _is_index_thscode(thscode)
            else "/api/a-share/prices/snapshot"
        )
        data = self._request(path, {"thscodes": thscode})
        items = data.get("item") or []
        item = items[0] if items else None
        if not isinstance(item, dict):
            return None
        price = safe_float(item.get("last_price"))
        if price is None:
            return None
        prev = safe_float(item.get("prev_price"))
        amplitude = None
        if prev:
            high = safe_float(item.get("high_price"))
            low = safe_float(item.get("low_price"))
            if high is not None and low is not None:
                amplitude = round((high - low) / prev * 100, 2)
        return UnifiedRealtimeQuote(
            code=thscode.split(".")[0],
            name="",
            source=RealtimeSource.HITHINK,
            price=price,
            change_pct=safe_float(item.get("price_change_ratio_pct")),
            change_amount=safe_float(item.get("price_change")),
            volume=safe_int(item.get("volume")),
            amount=safe_float(item.get("turnover")),
            open_price=safe_float(item.get("open_price")),
            amplitude=amplitude,
        )

    # -- 日线 --------------------------------------------------------------
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        thscode = to_thscode(stock_code)
        tz = timezone(timedelta(hours=8))
        start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz).timestamp() * 1000)
        end_ms = int(
            (datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)).timestamp()
            * 1000
            - 1
        )
        if _is_index_thscode(thscode):
            # 指数走指数历史端点，且无复权语义、不接受 adjust 参数。
            data = self._request(
                "/api/a-share-index/prices/historical",
                {"thscode": thscode, "interval": "1d", "start": start_ms, "end": end_ms},
            )
        else:
            data = self._request(
                "/api/a-share/prices/historical",
                {"thscode": thscode, "interval": "1d", "start": start_ms, "end": end_ms, "adjust": "forward"},
            )
        return pd.DataFrame(data.get("item") or [])

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=STANDARD_COLUMNS)
        tz = timezone(timedelta(hours=8))
        rows = []
        prev_close = None
        for _, row in df.iterrows():
            try:
                date = datetime.fromtimestamp(float(row.get("date_ms", 0)) / 1000, tz=tz).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            close = safe_float(row.get("close_price"))
            if close is None:
                continue
            pct_chg = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
            rows.append(
                {
                    "date": date,
                    "open": safe_float(row.get("open_price"), close),
                    "high": safe_float(row.get("high_price"), close),
                    "low": safe_float(row.get("low_price"), close),
                    "close": close,
                    "volume": safe_float(row.get("volume"), 0.0),
                    "amount": safe_float(row.get("turnover"), 0.0),
                    "pct_chg": pct_chg,
                }
            )
            prev_close = close
        return pd.DataFrame(rows, columns=STANDARD_COLUMNS)

    # -- 涨停池 / 热榜 -----------------------------------------------------
    def get_limit_up_pool(
        self, date: Optional[str] = None, n: int = 20
    ) -> Optional[List[Dict[str, Any]]]:
        params: Dict[str, Any] = {"page": 1, "size": max(1, min(int(n or 20), 200))}
        if date:
            try:
                params["date_ms"] = _shanghai_midnight_ms(date)
            except ValueError as exc:
                raise DataFetchError(f"[Hithink] 非法日期格式 {date!r}，期望 YYYYMMDD") from exc
        data = self._request("/api/a-share/special-data/limit-up-pool", params)
        results = []
        for item in data.get("item") or []:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "")
            if not ticker:
                continue
            results.append(
                {
                    "code": ticker,
                    "name": item.get("name") or "",
                    "price": safe_float(item.get("last_price"), 0.0),
                    "change_pct": safe_float(item.get("price_change_ratio_pct"), 0.0),
                    "continue_boards": safe_int(item.get("continue_day_cnt"), 0),
                    "limit_up_time": item.get("limit_up_time") or "",
                    "reason": item.get("limit_up_reason") or "",
                    "seal_amount": safe_float(item.get("seal_money"), 0.0),
                }
            )
        return results or None

    def get_hot_stocks(self, n: int = 10) -> Optional[List[Dict[str, Any]]]:
        data = self._request("/api/a-share/special-data/hot-stock-list", {})
        results = []
        for item in (data.get("item") or [])[: max(1, int(n or 10))]:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "")
            if not ticker:
                continue
            results.append(
                {
                    "rank": safe_int(item.get("rank"), 0),
                    "code": ticker,
                    "name": item.get("name") or "",
                    "source": "同花顺热股榜",
                }
            )
        return results or None

    # -- 市场统计 ----------------------------------------------------------
    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        limit = max(1, int(self.snapshot_page_size or 500))
        offset = 0
        up = down = flat = 0
        total_amount = 0.0
        seen = 0
        pages = 0
        while True:
            if pages >= _MAX_SNAPSHOT_PAGES:
                logger.warning("[Hithink] 快照分页达到上限 %d，停止翻页", _MAX_SNAPSHOT_PAGES)
                break
            pages += 1
            data = self._request(
                "/api/a-share/prices/snapshot", {"limit": limit, "offset": offset}
            )
            items = data.get("item") or []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                pct = safe_float(item.get("price_change_ratio_pct"))
                if pct is None:
                    continue
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
                else:
                    flat += 1
                total_amount += safe_float(item.get("turnover"), 0.0) or 0.0
            seen += len(items)
            total = safe_int(data.get("total"), 0) or 0
            offset += len(items)
            if (total and offset >= total) or len(items) < limit:
                break
        if seen == 0:
            return None
        limit_up = self._count_pool("/api/a-share/special-data/limit-up-pool")
        limit_down = self._count_pool("/api/a-share/special-data/limit-down-pool")
        return {
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "total_amount": round(total_amount / 1e8, 2),
        }

    def _count_pool(self, path: str) -> int:
        try:
            data = self._request(path, {"page": 1, "size": 1})
            pagination = data.get("pagination") or {}
            return safe_int(pagination.get("total"), 0) or 0
        except DataFetchError:
            logger.warning("[Hithink] 获取涨跌停池计数失败（fail-open）: %s", path)
            return 0

    # -- 板块/概念（v1 未实现，交由管理器降级） ------------------------------
    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        logger.debug("[Hithink] 板块排行待 TI 目录对接，跳过")
        return None

    def get_concept_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        logger.debug("[Hithink] 概念排行待 TI 目录对接，跳过")
        return None
